//! S17 ranking over a single authorized snapshot. Relations never cross scopes.
use super::{index::Snapshot, text};
use serde_json::{Value as Json, json};
use std::collections::{HashMap, HashSet};

pub const SCORER_VERSION: &str = "bm25-1.2-0.75-rrf60-lineage-source-v1";

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
    successor: Option<String>,
    predecessors: Vec<String>,
}
impl Links {
    pub fn parse(body: &str) -> Self {
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

/// Connected visible cards collapse to a terminal head. Multiple terminals and
/// cycles use newest revision, then answer order. Unmatched successors can be
/// returned with the predecessor's score. No hidden card key enters the chain.
pub struct Collapsed {
    pub hits: Vec<(u32, f64)>,
    pub chains: HashMap<u32, Vec<Json>>,
    pub sources: HashMap<u32, u32>,
}
pub fn collapse(snap: &Snapshot, allowed: &[bool], hits: Vec<(u32, f64)>) -> Collapsed {
    let live = snap.live(allowed);
    let keys: HashMap<_, _> = live
        .iter()
        .map(|&n| {
            let d = snap.doc(n);
            ((d.scope, d.card_id.to_string()), n)
        })
        .collect();
    let mut edges: HashMap<u32, Vec<u32>> = HashMap::new();
    let mut outgoing = HashSet::new();
    for &n in &live {
        let d = snap.doc(n);
        // Unreviewed automation cannot absorb interactive cards by claiming
        // their ids as aliases. Reviewed proposal relations remain effective.
        if &*d.trust == "flagged" || (d.automated && &*d.trust == "agent-unreviewed") {
            continue;
        }
        let mut link = |from, to| {
            if from != to {
                edges.entry(from).or_default().push(to);
                edges.entry(to).or_default().push(from);
                outgoing.insert(from);
            }
        };
        if let Some(to) = d
            .links
            .successor
            .as_ref()
            .and_then(|id| keys.get(&(d.scope, id.clone())))
        {
            link(n, *to);
        }
        for from in &d.links.predecessors {
            if let Some(from) = keys.get(&(d.scope, from.clone())) {
                link(*from, n)
            }
        }
    }
    let mut heads = HashMap::new();
    let mut chains = HashMap::new();
    let mut out = Vec::new();
    let mut emitted = HashSet::new();
    let mut sources = HashMap::new();
    for (n, score) in hits {
        let head = if let Some(&head) = heads.get(&n) {
            head
        } else {
            let mut component = Vec::new();
            let mut seen = HashSet::new();
            let mut todo = vec![n];
            while let Some(next) = todo.pop() {
                if seen.insert(next) {
                    component.push(next);
                    todo.extend(edges.get(&next).into_iter().flatten());
                }
            }
            component.sort_by(|a, b| {
                let da = snap.doc(*a);
                let db = snap.doc(*b);
                outgoing
                    .contains(a)
                    .cmp(&outgoing.contains(b))
                    .then_with(|| db.revision.cmp(&da.revision))
                    .then_with(|| da.card_id.cmp(&db.card_id))
            });
            let head = component[0];
            let chain = component.iter().map(|&member| {
                heads.insert(member, head);
                let d = snap.doc(member);
                json!({"scope": snap.scopes()[d.scope as usize], "card_id": &*d.card_id, "revision": d.revision})
            }).collect();
            chains.insert(head, chain);
            head
        };
        if emitted.insert(head) {
            sources.insert(head, n);
            out.push((head, score))
        }
    }
    Collapsed {
        hits: out,
        chains,
        sources,
    }
}
