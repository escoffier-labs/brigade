//! Recheck S17 thresholds from admin-attested results, never a caller pass bit.
use super::ids;
use serde_json::{Value as Json, json};

const METRICS: [&str; 4] = ["hit_rate", "mrr", "precision_at_k", "recall_at_k"];
fn stats(value: &Json) -> Option<(u64, [f64; 4])> {
    let count = value["query_count"].as_u64().filter(|n| *n > 0)?;
    let mut scores = [0.0; 4];
    for (i, key) in METRICS.iter().enumerate() {
        let score = value[*key].as_f64()?;
        if !score.is_finite() || !(0.0..=1.0).contains(&score) {
            return None;
        }
        scores[i] = score;
    }
    Some((count, scores))
}
fn compare(before: &Json, after: &Json, lexical: bool) -> bool {
    let (Some((n, b)), Some((m, a))) = (stats(before), stats(after)) else {
        return false;
    };
    n == m
        && a.iter()
            .zip(b)
            .all(|(a, b)| if lexical { *a == b } else { *a >= b })
}
fn ranking(adapter: &Json) -> Option<&str> {
    let digest = adapter["ranking_sha256"].as_str().filter(|s| {
        s.len() == 64
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })?;
    if let Some(rows) = adapter.get("queries") {
        let rows = rows.as_array()?;
        if Some(rows.len() as u64) != adapter["overall"]["query_count"].as_u64() {
            return None;
        }
        let mut rankings = Vec::new();
        for row in rows {
            let ids = row["ranked_candidates"].as_array()?;
            if !ids.iter().all(Json::is_string) {
                return None;
            }
            rankings.push(ids);
        }
        // Python's stable_digest ASCII-escapes strings, including UTF-16 pairs.
        // Rankings contain strings only, so floating-point formatting is absent.
        let mut ascii = String::new();
        for c in ids::canonical(&json!(rankings)).chars() {
            if u32::from(c) >= 127 {
                use std::fmt::Write;
                for unit in c.encode_utf16(&mut [0; 2]) {
                    write!(ascii, "\\u{unit:04x}").unwrap();
                }
            } else {
                ascii.push(c);
            }
        }
        if ids::sha256_hex(ascii.as_bytes()) != digest {
            return None;
        }
    }
    Some(digest)
}
pub(super) fn passed(body: &Json, mode: &str) -> bool {
    let lexical = mode == "lexical";
    let before = &body["adapters"]["bm25"];
    let after = &body["adapters"][if lexical { "hub-lexical" } else { "hub-hybrid" }];
    if before["available"] != true
        || after["available"] != true
        || after["binding"]["ranking"] != if lexical { "raw" } else { "default" }
        || (!lexical && body["k"].as_u64() != Some(5))
        || !compare(&before["overall"], &after["overall"], lexical)
    {
        return false;
    }
    let (Some(b), Some(a)) = (
        before["by_category"].as_object(),
        after["by_category"].as_object(),
    ) else {
        return false;
    };
    if b.is_empty()
        || b.len() != a.len()
        || b.iter()
            .any(|(k, v)| !compare(v, a.get(k).unwrap_or(&Json::Null), lexical))
    {
        return false;
    }
    // Every category in the baseline must be present in the candidate. Counts
    // must cover the overall metric, not silently omit an inconvenient slice.
    for adapter in [before, after] {
        let total = adapter["by_category"]
            .as_object()
            .unwrap()
            .values()
            .try_fold(0u64, |n, v| n.checked_add(v["query_count"].as_u64()?));
        if total != adapter["overall"]["query_count"].as_u64() {
            return false;
        }
    }
    if lexical {
        return ranking(before).is_some_and(|digest| ranking(after) == Some(digest));
    }
    ["paraphrase", "abbreviation"].iter().any(|key| {
        let (Some((_, before)), Some((_, after))) = (
            stats(b.get(*key).unwrap_or(&Json::Null)),
            stats(a.get(*key).unwrap_or(&Json::Null)),
        ) else {
            return false;
        };
        after.iter().zip(before).any(|(a, b)| *a > b)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn full_ranking_hash_matches_python_ascii_escaping() {
        let digest = "b8c3dcad5bdd690150f219c35aa173a8dcbc059f6f15bccc81aa6882147bd4e3";
        let adapter = json!({"overall":{"query_count":1},"queries":[{"ranked_candidates":["café","face😀","\u{7f}"]}],"ranking_sha256":digest});
        assert_eq!(ranking(&adapter), Some(digest));
    }
}
