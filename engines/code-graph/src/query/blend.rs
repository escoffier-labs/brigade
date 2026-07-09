//! Blend external semantic-search hits with GraphTrail call-graph centrality.
//!
//! This module is pure (no network): it takes embedding hits keyed by file and combines them with
//! each symbol's call-edge degree. The HTTP fetch lives in the feature-gated `adapters::codesearch`.

use anyhow::Result;
use rusqlite::{Connection, params};
use serde::Serialize;

use crate::model::SearchRow;

/// An embedding hit from an external semantic search (e.g. Code Search), keyed by file.
#[derive(Debug, Clone, Serialize)]
pub struct ExternalHit {
    pub file_path: String,
    pub score: f64,
}

#[derive(Debug, Serialize)]
pub struct BlendedRow {
    pub symbol: SearchRow,
    pub embedding_score: f64,
    pub graph_score: f64,
    pub blended_score: f64,
}

/// For each hit file, score every symbol in it by `weight_embed * embedding + weight_graph * degree`,
/// where degree is the symbol's total call-edge count normalized across the candidate set.
pub fn blend(
    conn: &Connection,
    hits: &[ExternalHit],
    weight_embed: f64,
    weight_graph: f64,
    limit: usize,
) -> Result<Vec<BlendedRow>> {
    let mut candidates: Vec<(SearchRow, f64, f64)> = Vec::new();
    for hit in hits {
        let mut stmt = conn.prepare(
            r#"
            SELECT s.id, s.kind, s.name, s.qualified_name, s.file_path, s.start_line, s.end_line,
                   s.signature,
                   (SELECT COUNT(*) FROM edges e WHERE e.source = s.id OR e.target = s.id) AS degree
            FROM symbols s
            WHERE s.file_path = ?1
            "#,
        )?;
        let mapped = stmt.query_map(params![hit.file_path], |row| {
            let degree: i64 = row.get(8)?;
            Ok((
                SearchRow {
                    id: row.get(0)?,
                    kind: row.get(1)?,
                    name: row.get(2)?,
                    qualified_name: row.get(3)?,
                    file_path: row.get(4)?,
                    start_line: row.get::<_, i64>(5)? as usize,
                    end_line: row.get::<_, i64>(6)? as usize,
                    signature: row.get(7)?,
                    score: hit.score,
                },
                hit.score,
                degree as f64,
            ))
        })?;
        for row in mapped {
            candidates.push(row?);
        }
    }

    let max_degree = candidates
        .iter()
        .map(|(_, _, degree)| *degree)
        .fold(0.0_f64, f64::max);

    let mut rows: Vec<BlendedRow> = candidates
        .into_iter()
        .map(|(symbol, embed, degree)| {
            let graph_score = if max_degree > 0.0 {
                degree / max_degree
            } else {
                0.0
            };
            BlendedRow {
                blended_score: weight_embed * embed + weight_graph * graph_score,
                embedding_score: embed,
                graph_score,
                symbol,
            }
        })
        .collect();

    rows.sort_by(|a, b| {
        b.blended_score
            .partial_cmp(&a.blended_score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    rows.truncate(limit);
    Ok(rows)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{init_schema, open_db, sync_repo};
    use std::fs;

    #[test]
    fn graph_weight_promotes_central_symbol() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        // hub.py: `hub` is called by many -> high degree. leaf.py: `leaf` called by none.
        fs::write(
            root.join("hub.py"),
            "def hub():\n    return 1\n\ndef a():\n    return hub()\n\ndef b():\n    return hub()\n",
        )
        .unwrap();
        fs::write(root.join("leaf.py"), "def leaf():\n    return 1\n").unwrap();
        let conn = open_db(&root.join("g.db")).unwrap();
        init_schema(&conn).unwrap();
        sync_repo(&conn, root).unwrap();

        // Equal embedding scores; pure graph weighting should rank hub.py's symbols on top.
        let hits = vec![
            ExternalHit {
                file_path: "hub.py".to_string(),
                score: 0.5,
            },
            ExternalHit {
                file_path: "leaf.py".to_string(),
                score: 0.5,
            },
        ];
        let rows = blend(&conn, &hits, 0.0, 1.0, 10).unwrap();
        assert!(!rows.is_empty());
        assert_eq!(rows[0].symbol.name, "hub");
        assert!(rows[0].graph_score >= rows.last().unwrap().graph_score);
    }
}
