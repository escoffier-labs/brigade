//! Immutable row-major matrices with shared append segments. Hash-to-document mapping
//! uses the lexical snapshot, so retired revisions never become dense hits.
use super::{embedder::normalize_vector, index::Snapshot};
use rusqlite::Connection;
use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex, RwLock},
};

#[derive(Debug)]
pub struct Segment {
    pub hashes: Vec<String>,
    pub matrix: Vec<f32>,
}
#[derive(Clone, Default, Debug)]
pub struct Matrix {
    pub dimensions: usize,
    pub segments: Vec<Arc<Segment>>,
    pub rows: usize,
}
impl Matrix {
    pub fn hashes(&self) -> HashSet<&str> {
        self.segments
            .iter()
            .flat_map(|s| s.hashes.iter().map(String::as_str))
            .collect()
    }
    pub fn score(&self, snapshot: &Snapshot, allowed: &[bool], query: &[f32]) -> Vec<(u32, f64)> {
        if query.len() != self.dimensions {
            return Vec::new();
        }
        let mut scores = HashMap::new();
        for segment in &self.segments {
            for (hash, row) in segment
                .hashes
                .iter()
                .zip(segment.matrix.chunks_exact(self.dimensions))
            {
                scores.insert(
                    hash.as_str(),
                    row.iter()
                        .zip(query)
                        .map(|(a, b)| *a as f64 * *b as f64)
                        .sum::<f64>(),
                );
            }
        }
        snapshot
            .live(allowed)
            .into_iter()
            .filter_map(|n| scores.get(&*snapshot.doc(n).content_hash).map(|s| (n, *s)))
            .collect()
    }
    /// Rebuild from the captured immutable segments. A reader may retain the old copy.
    pub fn compact(&self) -> Self {
        let mut segment = Segment {
            hashes: Vec::with_capacity(self.rows),
            matrix: Vec::with_capacity(self.rows * self.dimensions),
        };
        for old in &self.segments {
            segment.hashes.extend(old.hashes.iter().cloned());
            segment.matrix.extend_from_slice(&old.matrix);
        }
        Self {
            dimensions: self.dimensions,
            segments: vec![Arc::new(segment)],
            rows: self.rows,
        }
    }
}
#[derive(Default)]
pub struct Index {
    publication: Mutex<()>,
    matrices: RwLock<HashMap<String, Arc<Matrix>>>,
}
impl Index {
    pub fn current(&self, id: &str) -> Option<Arc<Matrix>> {
        self.matrices.read().unwrap().get(id).cloned()
    }
    pub fn drop_id(&self, id: &str) {
        let _publication = self.publication.lock().unwrap();
        self.matrices.write().unwrap().remove(id);
    }
    /// Called on a CPU/reader pool, never under either write lock. Existing
    /// matrices remain shared and a new immutable delta holds only new rows.
    pub fn refresh(
        &self,
        db: &Connection,
        id: &str,
        dimensions: usize,
    ) -> rusqlite::Result<Arc<Matrix>> {
        let _publication = self.publication.lock().unwrap();
        let old = self.current(id).unwrap_or_else(|| {
            Arc::new(Matrix {
                dimensions,
                ..Matrix::default()
            })
        });
        let known = old.hashes();
        let mut segment = Segment {
            hashes: Vec::new(),
            matrix: Vec::new(),
        };
        let mut live = HashSet::new();
        let mut stmt = db.prepare("SELECT content_hash,vector FROM memory_vectors WHERE embedder_id=?1 AND normalizer_version=1 ORDER BY content_hash")?;
        let mut rows = stmt.query([id])?;
        while let Some(row) = rows.next()? {
            let hash: String = row.get(0)?;
            live.insert(hash.clone());
            if known.contains(hash.as_str()) {
                continue;
            }
            let bytes: Vec<u8> = row.get(1)?;
            if bytes.len() != dimensions * 4 {
                return Err(rusqlite::Error::InvalidQuery);
            }
            let floats = bytes
                .chunks_exact(4)
                .map(|b| f32::from_le_bytes(b.try_into().unwrap()))
                .collect();
            let floats =
                normalize_vector(floats, dimensions).map_err(|_| rusqlite::Error::InvalidQuery)?;
            segment.hashes.push(hash);
            segment.matrix.extend(floats);
        }
        let removed = known.iter().any(|h| !live.contains(*h));
        let mut next = (*old).clone();
        next.rows += segment.hashes.len();
        if !segment.hashes.is_empty() {
            next.segments.push(Arc::new(segment));
        }
        if removed {
            let mut main = Segment {
                hashes: Vec::new(),
                matrix: Vec::new(),
            };
            for seg in &next.segments {
                for (hash, vector) in seg.hashes.iter().zip(seg.matrix.chunks_exact(dimensions)) {
                    if live.contains(hash) {
                        main.hashes.push(hash.clone());
                        main.matrix.extend_from_slice(vector);
                    }
                }
            }
            next.rows = main.hashes.len();
            next.segments = vec![Arc::new(main)];
        } else if next.segments.len() > 1
            && (next.rows - next.segments[0].hashes.len()) * 10 > next.segments[0].hashes.len()
        {
            next = next.compact();
        }
        let next = Arc::new(next);
        self.matrices
            .write()
            .unwrap()
            .insert(id.into(), next.clone());
        Ok(next)
    }
}
