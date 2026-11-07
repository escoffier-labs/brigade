//! S17 ranking over a single authorized snapshot. Relations never cross scopes.
use super::{index::Snapshot, text};
use serde_json::{Value as Json, json};
use std::collections::{HashMap, HashSet};

pub const SCORER_VERSION: &str = "bm25-1.2-0.75-rrf60-lineage-source-v2";

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
                let id = value
                    .strip_prefix("memory/cards/")
                    .or_else(|| value.strip_prefix("cards/"))
                    .unwrap_or(&value);
                let id = id.strip_prefix(&format!("{scope}/")).unwrap_or(id);
                let id = id.strip_suffix(".md").unwrap_or(id);
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

pub struct Collapsed {
    pub hits: Vec<(u32, f64)>,
    pub chains: HashMap<u32, Vec<Json>>,
    pub sources: HashMap<u32, u32>,
    pub visited: usize,
}

#[cfg(test)]
pub fn collapse(snap: &Snapshot, allowed: &[bool], hits: Vec<(u32, f64)>) -> Collapsed {
    collapse_eligible(snap, allowed, hits, |_| Ok(true)).unwrap()
}

/// Walk only components reached from hits. Eligibility applies to each end and
/// the trusted owner of each assertion. Separate terminal heads stay separate.
pub fn collapse_eligible(
    snap: &Snapshot,
    allowed: &[bool],
    hits: Vec<(u32, f64)>,
    mut eligible: impl FnMut(u32) -> rusqlite::Result<bool>,
) -> rusqlite::Result<Collapsed> {
    let mut out = Collapsed {
        hits: Vec::new(),
        chains: HashMap::new(),
        sources: HashMap::new(),
        visited: 0,
    };
    if hits.is_empty() {
        return Ok(out);
    }
    let mut eligibility = HashMap::new();
    let mut permit = |n: u32| -> rusqlite::Result<bool> {
        if let Some(ok) = eligibility.get(&n) {
            return Ok(*ok);
        }
        let d = snap.doc(n);
        let ok = d.live && allowed.get(d.scope as usize).copied().unwrap_or(false) && eligible(n)?;
        eligibility.insert(n, ok);
        Ok(ok)
    };
    let hit_ranks: HashMap<_, _> = hits
        .iter()
        .enumerate()
        .map(|(rank, (n, score))| (*n, (rank, *score)))
        .collect();
    let mut seen = HashSet::new();
    for (source, _) in hits {
        if seen.contains(&source) || !permit(source)? {
            continue;
        }
        let mut members = Vec::new();
        let mut forward: HashMap<u32, Vec<u32>> = HashMap::new();
        let mut reverse: HashMap<u32, Vec<u32>> = HashMap::new();
        let mut todo = vec![source];
        while let Some(n) = todo.pop() {
            if !seen.insert(n) {
                continue;
            }
            members.push(n);
            for (edges, outgoing) in [(snap.forward(n), true), (snap.reverse(n), false)] {
                for edge in edges {
                    if !permit(edge.doc)? || !permit(edge.owner)? {
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
        out.visited += members.len();
        let mut heads: Vec<_> = members
            .iter()
            .copied()
            .filter(|n| {
                forward.get(n).is_none_or(Vec::is_empty) && &*snap.doc(*n).trust != "flagged"
            })
            .collect();
        let recency = |a: &u32, b: &u32| {
            snap.doc(*b)
                .head_seq
                .cmp(&snap.doc(*a).head_seq)
                .then_with(|| snap.doc(*a).card_id.cmp(&snap.doc(*b).card_id))
        };
        heads.sort_by(recency);
        if heads.is_empty() {
            // A cycle keeps its newest eligible event. A standalone flagged
            // hit remains an ordinary result, never a replacement head.
            let mut candidates: Vec<_> = members
                .iter()
                .copied()
                .filter(|n| &*snap.doc(*n).trust != "flagged")
                .collect();
            candidates.sort_by(recency);
            if let Some(head) = candidates.first() {
                heads.push(*head);
            } else {
                // Flagged explicit hits stay searchable without becoming a
                // lineage head or acquiring a chain.
                out.hits.push((source, hit_ranks[&source].1));
                out.sources.insert(source, source);
                continue;
            }
        }
        for head in heads {
            let mut ancestors = HashSet::new();
            let mut todo = vec![head];
            while let Some(n) = todo.pop() {
                if ancestors.insert(n) {
                    todo.extend(reverse.get(&n).into_iter().flatten())
                }
            }
            let donor = ancestors
                .iter()
                .filter_map(|n| hit_ranks.get(n).map(|(rank, score)| (*rank, *n, *score)))
                .min_by_key(|v| v.0);
            let Some((_, donor, score)) = donor else {
                continue;
            };
            let mut chain: Vec<_> = ancestors.into_iter().collect();
            chain.sort_by(recency);
            out.chains.insert(head, chain.into_iter().map(|n| {
                let d = snap.doc(n);
                json!({"scope":snap.scopes()[d.scope as usize],"card_id":&*d.card_id,"revision":d.revision})
            }).collect());
            out.sources.insert(head, donor);
            out.hits.push((head, score));
        }
    }
    out.hits.sort_by(|(a, _), (b, _)| {
        hit_ranks[&out.sources[a]]
            .0
            .cmp(&hit_ranks[&out.sources[b]].0)
            .then_with(|| snap.doc(*b).head_seq.cmp(&snap.doc(*a).head_seq))
            .then_with(|| snap.doc(*a).card_id.cmp(&snap.doc(*b).card_id))
    });
    Ok(out)
}
