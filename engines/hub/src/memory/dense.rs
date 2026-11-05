//! Immutable dense snapshots. The writer appends small deltas after commit.
//! A background compactor captures a watermark and the writer replays its tail.
use super::{embedder::normalize_vector, index::Snapshot, writer};
use rusqlite::Connection;
use std::{
    collections::{HashMap, HashSet},
    sync::{
        Arc, Mutex, RwLock,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
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
    pub indexed_seq: i64,
    /// None means all materialized rows. Deletions copy only this metadata.
    pub live: Option<Arc<HashSet<String>>>,
}
impl Matrix {
    fn has(&self, hash: &str) -> bool {
        self.live.as_ref().is_none_or(|live| live.contains(hash))
    }
    pub fn hashes(&self) -> HashSet<&str> {
        self.segments
            .iter()
            .flat_map(|s| s.hashes.iter().map(String::as_str))
            .filter(|h| self.has(h))
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
                if self.has(hash) {
                    scores.insert(
                        hash.as_str(),
                        row.iter()
                            .zip(query)
                            .map(|(a, b)| *a as f64 * *b as f64)
                            .sum::<f64>(),
                    );
                }
            }
        }
        snapshot
            .live(allowed)
            .into_iter()
            .filter_map(|n| scores.get(&*snapshot.doc(n).content_hash).map(|s| (n, *s)))
            .collect()
    }
    pub fn compact(&self) -> Self {
        // Latest append wins when a collected hash was embedded again. Rows
        // no longer present in the captured live table are omitted.
        let mut latest = HashMap::new();
        for old in &self.segments {
            for (hash, row) in old
                .hashes
                .iter()
                .zip(old.matrix.chunks_exact(self.dimensions))
            {
                if self.has(hash) {
                    latest.insert(hash.clone(), row);
                }
            }
        }
        let mut hashes: Vec<_> = latest.keys().cloned().collect();
        hashes.sort();
        let mut matrix = Vec::with_capacity(hashes.len() * self.dimensions);
        for hash in &hashes {
            matrix.extend_from_slice(latest[hash]);
        }
        let rows = hashes.len();
        Self {
            dimensions: self.dimensions,
            segments: vec![Arc::new(Segment { hashes, matrix })],
            rows,
            indexed_seq: self.indexed_seq,
            live: None,
        }
    }
    fn needs_rebuild(&self) -> bool {
        let main = self.segments.first().map_or(0, |s| s.hashes.len());
        self.segments.len() > 1 && (self.rows - main) * 10 > main
            || self
                .live
                .as_ref()
                .is_some_and(|live| (self.rows - live.len()) * 10 > self.rows)
    }
}
pub struct Rebuilt {
    id: String,
    matrix: Matrix,
    epoch: u64,
}
#[derive(Default)]
pub struct Index {
    publication: Mutex<()>,
    matrices: RwLock<HashMap<String, Arc<Matrix>>>,
    active: RwLock<Option<(String, usize)>>,
    sender: Mutex<Option<tokio::sync::mpsc::WeakSender<writer::Job>>>,
    epoch: AtomicU64,
    rebuilding: AtomicBool,
    stopping: AtomicBool,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
    completed: tokio::sync::Notify,
    #[cfg(test)]
    gate: Mutex<Option<Arc<Gate>>>,
}
#[cfg(test)]
pub struct Gate {
    pub started: std::sync::Barrier,
    pub release: std::sync::Barrier,
}
impl Index {
    pub fn current(&self, id: &str) -> Option<Arc<Matrix>> {
        self.matrices.read().unwrap().get(id).cloned()
    }
    pub fn attach(&self, sender: tokio::sync::mpsc::WeakSender<writer::Job>) {
        *self.sender.lock().unwrap() = Some(sender);
    }
    pub fn configure(&self, active: Option<(String, usize)>) {
        let _publication = self.publication.lock().unwrap();
        self.epoch.fetch_add(1, Ordering::SeqCst);
        if active.is_none() {
            self.matrices.write().unwrap().clear();
        }
        *self.active.write().unwrap() = active;
    }
    pub fn stop(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.lock().unwrap().take() {
            let _ = thread.join();
        }
    }
    pub fn drop_id(&self, id: &str) {
        let _publication = self.publication.lock().unwrap();
        self.epoch.fetch_add(1, Ordering::SeqCst);
        self.matrices.write().unwrap().remove(id);
    }
    /// Read only vectors absent from the captured live table. The key scan
    /// replays additions and removals committed after a rebuild watermark.
    fn delta(db: &Connection, id: &str, base: &Matrix) -> rusqlite::Result<Matrix> {
        let known = base.hashes();
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
            if bytes.len() != base.dimensions * 4 {
                return Err(rusqlite::Error::InvalidQuery);
            }
            let floats = bytes
                .chunks_exact(4)
                .map(|b| f32::from_le_bytes(b.try_into().unwrap()))
                .collect();
            let floats = normalize_vector(floats, base.dimensions)
                .map_err(|_| rusqlite::Error::InvalidQuery)?;
            segment.hashes.push(hash);
            segment.matrix.extend(floats);
        }
        let mut next = base.clone();
        next.rows += segment.hashes.len();
        if !segment.hashes.is_empty() {
            next.segments.push(Arc::new(segment));
        }
        next.live = Some(Arc::new(live));
        next.indexed_seq =
            db.query_row("SELECT COALESCE(max(seq),0) FROM memory_events", [], |r| {
                r.get(0)
            })?;
        Ok(next)
    }
    pub fn refresh(
        self: &Arc<Self>,
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
        let next = Arc::new(Self::delta(db, id, &old)?);
        if next.rows == old.rows
            && next.indexed_seq == old.indexed_seq
            && next.hashes() == old.hashes()
        {
            if old.needs_rebuild() {
                self.rebuild(id, old.clone());
            }
            return Ok(old);
        }
        self.matrices
            .write()
            .unwrap()
            .insert(id.into(), next.clone());
        if next.needs_rebuild() {
            self.rebuild(id, next.clone());
        }
        Ok(next)
    }
    /// Called by the memory writer only after releasing both write locks.
    pub fn after_commit(self: &Arc<Self>, db: &Connection) {
        let active = self.active.read().unwrap().clone();
        if let Some((id, dimensions)) = active {
            let _ = self.refresh(db, &id, dimensions);
        }
    }
    fn rebuild(self: &Arc<Self>, id: &str, captured: Arc<Matrix>) {
        if self.stopping.load(Ordering::SeqCst) || self.rebuilding.load(Ordering::SeqCst) {
            return;
        }
        let sender = self.sender.lock().unwrap().clone();
        let Some(sender) = sender else {
            return;
        };
        if self.rebuilding.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Some(thread) = self.thread.lock().unwrap().take() {
            let _ = thread.join();
        }
        let weak = Arc::downgrade(self);
        let id = id.to_string();
        let epoch = self.epoch.load(Ordering::SeqCst);
        #[cfg(test)]
        let gate = self.gate.lock().unwrap().take();
        let thread = std::thread::Builder::new()
            .name("memory-dense-rebuild".into())
            .spawn(move || {
                #[cfg(test)]
                if let Some(gate) = gate {
                    gate.started.wait();
                    gate.release.wait();
                }
                let rebuilt = Rebuilt {
                    id,
                    matrix: captured.compact(),
                    epoch,
                };
                if let Some(index) = weak.upgrade() {
                    let sent = !index.stopping.load(Ordering::SeqCst)
                        && sender.upgrade().is_some_and(|sender| {
                            sender
                                .try_send(writer::Job::DenseRebuilt(Box::new(rebuilt)))
                                .is_ok()
                        });
                    if !sent {
                        index.rebuilding.store(false, Ordering::SeqCst);
                        index.completed.notify_waiters();
                    }
                }
            });
        match thread {
            Ok(thread) => *self.thread.lock().unwrap() = Some(thread),
            Err(_) => {
                self.rebuilding.store(false, Ordering::SeqCst);
                self.completed.notify_waiters();
            }
        }
    }
    /// The writer replays the committed tail and atomically publishes. A
    /// rebuild captured before a mode switch or drop cannot resurrect rows.
    pub fn installed(&self, rebuilt: Rebuilt, db: &Connection) {
        let _publication = self.publication.lock().unwrap();
        if !self.stopping.load(Ordering::SeqCst)
            && self.epoch.load(Ordering::SeqCst) == rebuilt.epoch
        {
            if let Ok(next) = Self::delta(db, &rebuilt.id, &rebuilt.matrix) {
                self.matrices
                    .write()
                    .unwrap()
                    .insert(rebuilt.id, Arc::new(next));
            }
        }
        self.rebuilding.store(false, Ordering::SeqCst);
        self.completed.notify_waiters();
    }
    #[cfg(test)]
    pub fn pause_next(&self) -> Arc<Gate> {
        let gate = Arc::new(Gate {
            started: std::sync::Barrier::new(2),
            release: std::sync::Barrier::new(2),
        });
        *self.gate.lock().unwrap() = Some(gate.clone());
        gate
    }
    #[cfg(test)]
    pub async fn wait_rebuild(&self) {
        loop {
            let wait = self.completed.notified();
            tokio::pin!(wait);
            wait.as_mut().enable();
            if !self.rebuilding.load(Ordering::SeqCst) {
                return;
            }
            wait.await;
        }
    }
}
