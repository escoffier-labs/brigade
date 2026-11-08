//! S17 ranking over a single authorized snapshot. Relations never cross scopes.
use super::{index::Snapshot, text};
use serde_json::{Value as Json, json};
use std::collections::{HashMap, HashSet};

pub const SCORER_VERSION: &str = "bm25-1.2-0.75-rrf60-lineage-source-v3";

#[derive(Clone, Copy, Debug)]
pub struct Weights {
    pub lexical: f64,
    pub dense: f64,
}
impl Default for Weights {
    fn default() -> Self {
        Self {
            lexical: 1.0,
            dense: 1.0,
        }
    }
}
impl Weights {
    pub fn parse(value: Option<&Json>) -> Result<Self, String> {
        let Some(value) = value else {
            return Ok(Self::default());
        };
        let map = value.as_object().ok_or("memory.rrf must be a table")?;
        if map
            .keys()
            .any(|k| !["lexical", "dense"].contains(&k.as_str()))
        {
            return Err("unknown memory.rrf weight".into());
        }
        let weight = |key| -> Result<f64, String> {
            match map.get(key) {
                None => Ok(1.0),
                Some(v) => v
                    .as_f64()
                    .filter(|v| v.is_finite() && *v > 0.0 && *v <= 100.0)
                    .ok_or_else(|| {
                        "memory.rrf weights must be finite, positive and at most 100".into()
                    }),
            }
        };
        Ok(Self {
            lexical: weight("lexical")?,
            dense: weight("dense")?,
        })
    }
    pub fn json(self) -> Json {
        json!({"lexical": self.lexical, "dense": self.dense})
    }
}

#[derive(Clone, Debug, Default)]
pub struct Links {
    /// The direction names the successor. Aliases name predecessors.
    pub(super) successor: Option<String>,
    pub(super) predecessors: Vec<String>,
}
impl Links {
    pub fn parse(body: &str, scope: &str) -> Self {
        let mut out = Self::default();
        for (key, value) in text::front_matter(body) {
            let values = match value {
                text::Value::Str(s) => {
                    match s.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
                        Some(items) => items
                            .split(',')
                            .map(|s| s.trim().trim_matches(['\'', '"']).to_string())
                            .collect(),
                        None => vec![s],
                    }
                }
                text::Value::Lit(text::Lit::List(items)) => {
                    items.iter().map(text::Lit::py_str).collect()
                }
                _ => continue,
            };
            for value in values.into_iter().take(256) {
                let path = value
                    .strip_prefix("memory/cards/")
                    .or_else(|| value.strip_prefix("cards/"));
                let id = match path {
                    Some(path) => path.rsplit_once('.').map(|(stem, _)| stem).unwrap_or(path),
                    None => value.strip_prefix(&format!("{scope}/")).unwrap_or(&value),
                };
                if !super::ids::valid_card_id(id) {
                    continue;
                }
                match key.as_str() {
                    "superseded_by" => out.successor = Some(id.into()),
                    "supersedes" | "merged_from" | "aliases" => out.predecessors.push(id.into()),
                    _ => (),
                }
            }
        }
        out
    }
}

/// Only reviewed relations may change another card's search identity.
pub fn trusted(trust: &str) -> bool {
    matches!(trust, "reviewed" | "policy-promoted" | "imported")
}

#[derive(Clone, Copy, Debug)]
pub struct Neighbour {
    pub doc: u32,
    pub owner: u32,
}

/// 200 initial candidates plus connected history, with at most 512 lineage
/// eligibility probes. Overflow preserves input order instead of losing hits.
pub const NODE_CAP: usize = 512;

pub struct Collapsed {
    pub hits: Vec<(u32, f64)>,
    pub chains: HashMap<u32, Vec<Json>>,
    pub sources: HashMap<u32, u32>,
    pub visited: usize,
    pub overflow: bool,
    pub uncollapsed: usize,
}
impl Collapsed {
    fn empty() -> Self {
        Self {
            hits: Vec::new(),
            chains: HashMap::new(),
            sources: HashMap::new(),
            visited: 0,
            overflow: false,
            uncollapsed: 0,
        }
    }
}

struct Visit<'a, F> {
    snap: &'a Snapshot,
    allowed: &'a [bool],
    eligible: F,
    mask: HashMap<u32, bool>,
    overflow: bool,
}
impl<F: FnMut(u32) -> rusqlite::Result<bool>> Visit<'_, F> {
    fn permit(&mut self, n: u32) -> rusqlite::Result<bool> {
        if let Some(ok) = self.mask.get(&n) {
            return Ok(*ok);
        }
        if self.mask.len() == NODE_CAP {
            self.overflow = true;
            return Ok(false);
        }
        let d = self.snap.doc(n);
        let ok = d.live
            && self.allowed.get(d.scope as usize).copied().unwrap_or(false)
            && (self.eligible)(n)?;
        self.mask.insert(n, ok);
        Ok(ok)
    }
}

#[cfg(test)]
pub fn collapse(snap: &Snapshot, allowed: &[bool], hits: Vec<(u32, f64)>) -> Collapsed {
    collapse_eligible(snap, allowed, hits, |_| Ok(true)).unwrap()
}

type Ranks = HashMap<u32, (usize, f64)>;
type Edges = HashMap<u32, Vec<u32>>;

fn recency(snap: &Snapshot, a: u32, b: u32) -> std::cmp::Ordering {
    snap.doc(b)
        .head_seq
        .cmp(&snap.doc(a).head_seq)
        .then_with(|| snap.doc(a).card_id.cmp(&snap.doc(b).card_id))
}

fn reachable(start: u32, edges: &Edges) -> HashSet<u32> {
    let mut seen = HashSet::new();
    let mut todo = vec![start];
    while let Some(n) = todo.pop() {
        if seen.insert(n) {
            todo.extend(edges.get(&n).into_iter().flatten());
        }
    }
    seen
}

/// Iterative Kosaraju on the bounded query graph, independent of other sinks.
fn components(
    members: &[u32],
    forward: &Edges,
    reverse: &Edges,
) -> (Vec<Vec<u32>>, HashMap<u32, usize>) {
    let mut seen = HashSet::new();
    let mut order = Vec::new();
    for &n in members {
        let mut todo = vec![(n, false)];
        while let Some((n, leaving)) = todo.pop() {
            if leaving {
                order.push(n);
            } else if seen.insert(n) {
                todo.push((n, true));
                todo.extend(
                    forward
                        .get(&n)
                        .into_iter()
                        .flatten()
                        .map(|next| (*next, false)),
                );
            }
        }
    }
    let mut ids = HashMap::new();
    let mut groups = Vec::new();
    for n in order.into_iter().rev() {
        if ids.contains_key(&n) {
            continue;
        }
        let mut group = Vec::new();
        let mut todo = vec![n];
        while let Some(n) = todo.pop() {
            if ids.contains_key(&n) {
                continue;
            }
            ids.insert(n, groups.len());
            group.push(n);
            todo.extend(reverse.get(&n).into_iter().flatten());
        }
        groups.push(group);
    }
    (groups, ids)
}

fn emit(
    out: &mut Collapsed,
    snap: &Snapshot,
    head: u32,
    members: &HashSet<u32>,
    ranks: &Ranks,
) -> bool {
    let donor = members
        .iter()
        .filter_map(|n| ranks.get(n).map(|(rank, score)| (*rank, *n, *score)))
        .min_by_key(|v| v.0);
    let Some((_, donor, score)) = donor else {
        return false;
    };
    if out.sources.contains_key(&head) {
        return true;
    }
    let mut chain: Vec<_> = members.iter().copied().collect();
    chain.sort_by(|a, b| recency(snap, *a, *b));
    if &*snap.doc(head).trust != "flagged" {
        out.chains.insert(head,chain.into_iter().map(|n| {
            let d=snap.doc(n);
            json!({"scope":snap.scopes()[d.scope as usize],"card_id":&*d.card_id,"revision":d.revision})
        }).collect());
    }
    out.sources.insert(head, donor);
    out.hits.push((head, score));
    true
}

/// Input candidates are the search's top 200. On budget overflow return all
/// eligible candidates unchanged, including earlier components, with no chain.
pub fn collapse_eligible(
    snap: &Snapshot,
    allowed: &[bool],
    hits: Vec<(u32, f64)>,
    eligible: impl FnMut(u32) -> rusqlite::Result<bool>,
) -> rusqlite::Result<Collapsed> {
    let mut out = Collapsed::empty();
    if hits.is_empty() {
        return Ok(out);
    }
    debug_assert!(hits.len() <= super::index::TOP);
    let mut visit = Visit {
        snap,
        allowed,
        eligible,
        mask: HashMap::new(),
        overflow: false,
    };
    let mut input = Vec::new();
    for (n, score) in hits {
        if visit.permit(n)? {
            input.push((n, score));
        }
    }
    let ranks: Ranks = input
        .iter()
        .enumerate()
        .map(|(rank, (n, score))| (*n, (rank, *score)))
        .collect();
    let mut seen = HashSet::new();
    for &(source, _) in &input {
        if seen.contains(&source) {
            continue;
        }
        let mut members = Vec::new();
        let mut forward: Edges = HashMap::new();
        let mut reverse: Edges = HashMap::new();
        let mut todo = vec![source];
        'walk: while let Some(n) = todo.pop() {
            if !seen.insert(n) {
                continue;
            }
            members.push(n);
            for (edges, outgoing) in [(snap.forward(n), true), (snap.reverse(n), false)] {
                for edge in edges {
                    let permitted = visit.permit(edge.doc)? && visit.permit(edge.owner)?;
                    if visit.overflow {
                        break 'walk;
                    }
                    if !permitted {
                        continue;
                    }
                    todo.push(edge.doc);
                    let (from, to) = if outgoing {
                        (n, edge.doc)
                    } else {
                        (edge.doc, n)
                    };
                    forward.entry(from).or_default().push(to);
                    reverse.entry(to).or_default().push(from);
                }
            }
        }
        if visit.overflow {
            out = Collapsed::empty();
            out.visited = visit.mask.len();
            out.overflow = true;
            out.uncollapsed = input.len();
            out.hits = input;
            return Ok(out);
        }
        let (groups, ids) = components(&members, &forward, &reverse);
        let mut claimed = HashSet::new();
        for (id, group) in groups.iter().enumerate() {
            if group.iter().any(|n| {
                forward
                    .get(n)
                    .into_iter()
                    .flatten()
                    .any(|next| ids[next] != id)
            }) {
                continue;
            }
            let head = group
                .iter()
                .copied()
                .filter(|n| &*snap.doc(*n).trust != "flagged")
                .min_by(|a, b| recency(snap, *a, *b));
            if let Some(head) = head {
                let ancestors = reachable(head, &reverse);
                if emit(&mut out, snap, head, &ancestors, &ranks) {
                    claimed.extend(ancestors);
                }
            }
        }
        // Never lose an eligible explicit hit, including a cycle beside a
        // separate terminal head or an ordinary flagged result.
        for &n in &members {
            if !ranks.contains_key(&n) || claimed.contains(&n) {
                continue;
            }
            let group = &groups[ids[&n]];
            let head = group
                .iter()
                .copied()
                .filter(|n| &*snap.doc(*n).trust != "flagged")
                .min_by(|a, b| recency(snap, *a, *b))
                .unwrap_or(n);
            let group: HashSet<_> = group.iter().copied().collect();
            emit(&mut out, snap, head, &group, &ranks);
            claimed.extend(group);
        }
    }
    out.visited = visit.mask.len();
    out.hits.sort_by(|(a, _), (b, _)| {
        ranks[&out.sources[a]]
            .0
            .cmp(&ranks[&out.sources[b]].0)
            .then_with(|| recency(snap, *a, *b))
    });
    Ok(out)
}
