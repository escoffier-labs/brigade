//! The lexical index (S13b): derived, in-memory state over the memory store.
//!
//! - Segments: one immutable main segment and a chain of immutable delta
//!   segments, each a sorted term array, one offsets array and contiguous
//!   postings of document numbers and term frequencies.
//! - Snapshots list their segments and carry a copy-on-write document table
//!   (live bit, scope, revision, trust, fields and length) in chunks, plus
//!   per-scope statistics (live documents and tokens). A query holds one
//!   reference-counted snapshot from start to end.
//! - Publication: after each committed batch the memory writer replays the
//!   log after `indexed_seq`, builds one delta segment and publishes a new
//!   snapshot. `indexed_seq` is the last `seq` that snapshot covers.
//! - Rebuild: once delta documents pass 10% of the main segment (and at least
//!   [`REBUILD_FLOOR`]), or after an index failure, a background thread builds
//!   a new main segment from the store at a watermark it reads with the
//!   cards. The writer replays the log after that watermark onto it and
//!   publishes, so writes made during the rebuild are kept.
//! - Failure: if publication fails after a commit, the last good snapshot is
//!   republished marked stale, `/metrics` shows the lag, and a rebuild starts.
//! - Restart builds the index from `memory_cards` and `memory_blobs`.
use super::text::{self, Fields};
use rusqlite::{Connection, OpenFlags};
use std::{
    collections::{HashMap, HashSet},
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::{mpsc, watch};

pub const K1: f64 = 1.2;
pub const B: f64 = 0.75;
/// Lexical candidates kept before the page is cut (pipeline step 3).
pub const TOP: usize = 200;
/// Index format, recorded beside scorer goldens and eval records.
pub const FORMAT_VERSION: &str = "1";
/// Delta documents below this never start a rebuild, so a small index is not
/// rebuilt on every write.
pub const REBUILD_FLOOR: usize = 256;
const CHUNK: usize = 1024;
/// Events one tail query reads, the same window the `log` reader scans.
const WINDOW: i64 = 5000;

/// One indexed card revision. Dead documents stay until the next rebuild.
#[derive(Clone, Debug)]
pub struct Doc {
    pub live: bool,
    pub scope: u32,
    pub card_id: Arc<str>,
    pub revision: i64,
    pub content_hash: Arc<str>,
    pub trust: Arc<str>,
    pub fields: Arc<Fields>,
    /// Tokens in the card's single BM25 field.
    pub length: u32,
}
/// The document table, copied a chunk at a time when a snapshot changes it.
#[derive(Clone, Default)]
struct Docs {
    chunks: Vec<Arc<Vec<Doc>>>,
    len: usize,
}
impl Docs {
    fn get(&self, n: u32) -> &Doc {
        let n = n as usize;
        &self.chunks[n / CHUNK][n % CHUNK]
    }
    fn push(&mut self, doc: Doc) -> u32 {
        if self.len % CHUNK == 0 {
            self.chunks.push(Arc::new(Vec::with_capacity(CHUNK)));
        }
        Arc::make_mut(self.chunks.last_mut().unwrap()).push(doc);
        self.len += 1;
        (self.len - 1) as u32
    }
    fn kill(&mut self, n: u32) {
        let n = n as usize;
        Arc::make_mut(&mut self.chunks[n / CHUNK])[n % CHUNK].live = false;
    }
}

/// An immutable postings segment.
#[derive(Default)]
pub struct Segment {
    terms: Vec<Box<str>>,
    offsets: Vec<u32>,
    docs: Vec<u32>,
    freqs: Vec<u32>,
}
/// Postings gathered one document at a time, so a build never holds every
/// document's tokens at once.
#[derive(Default)]
struct Postings(HashMap<Box<str>, Vec<Posting>>);
/// A document number and the term's frequency in it.
type Posting = (u32, u32);
impl Postings {
    fn add(&mut self, doc: u32, tokens: Vec<String>) {
        let mut counts: HashMap<String, u32> = HashMap::new();
        for token in tokens {
            *counts.entry(token).or_default() += 1;
        }
        for (term, freq) in counts {
            match self.0.get_mut(term.as_str()) {
                Some(list) => list.push((doc, freq)),
                None => {
                    self.0.insert(term.into_boxed_str(), vec![(doc, freq)]);
                }
            }
        }
    }
    fn finish(self) -> Segment {
        let mut terms: Vec<(Box<str>, Vec<Posting>)> = self.0.into_iter().collect();
        terms.sort_unstable_by(|a, b| a.0.cmp(&b.0));
        let total = terms.iter().map(|(_, l)| l.len()).sum();
        let mut segment = Segment {
            terms: Vec::with_capacity(terms.len()),
            offsets: Vec::with_capacity(terms.len() + 1),
            docs: Vec::with_capacity(total),
            freqs: Vec::with_capacity(total),
        };
        segment.offsets.push(0);
        for (term, mut list) in terms {
            // Documents are added in increasing order; sort anyway for safety.
            list.sort_unstable();
            for (doc, freq) in list {
                segment.docs.push(doc);
                segment.freqs.push(freq);
            }
            segment.offsets.push(segment.docs.len() as u32);
            segment.terms.push(term);
        }
        segment
    }
}
impl Segment {
    fn postings(&self, term: &str) -> Option<(&[u32], &[u32])> {
        let i = self.terms.binary_search_by(|t| (**t).cmp(term)).ok()?;
        let (a, b) = (self.offsets[i] as usize, self.offsets[i + 1] as usize);
        Some((&self.docs[a..b], &self.freqs[a..b]))
    }
    pub fn terms(&self) -> usize {
        self.terms.len()
    }
}

/// Live documents and their tokens in one scope.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Stats {
    pub docs: u64,
    pub tokens: u64,
}

/// `<card_id>.md`: the tie path without its shared `cards/` prefix.
fn path(d: &Doc) -> impl Iterator<Item = u8> + '_ {
    d.card_id.bytes().chain(b".md".iter().copied())
}

/// A published, immutable view of the index.
#[derive(Clone, Default)]
pub struct Snapshot {
    segments: Vec<Arc<Segment>>,
    docs: Docs,
    scopes: Arc<Vec<String>>,
    stats: Arc<Vec<Stats>>,
    pub indexed_seq: i64,
    pub stale: bool,
    main_docs: usize,
    delta_docs: usize,
}
impl Snapshot {
    pub fn scopes(&self) -> &[String] {
        &self.scopes
    }
    pub fn doc(&self, n: u32) -> &Doc {
        self.docs.get(n)
    }
    pub fn segments(&self) -> usize {
        self.segments.len()
    }
    pub fn stats(&self, scope: u32) -> Stats {
        self.stats.get(scope as usize).copied().unwrap_or_default()
    }
    /// Documents in the table, dead ones included.
    pub fn documents(&self) -> usize {
        self.docs.len
    }
    /// Live documents whose scope is allowed, in document order.
    pub fn live(&self, allowed: &[bool]) -> Vec<u32> {
        (0..self.docs.len as u32)
            .filter(|n| {
                let d = self.docs.get(*n);
                d.live && allowed.get(d.scope as usize).copied().unwrap_or(false)
            })
            .collect()
    }
    /// Best first; ties by the string `cards/<card_id>.md`, then the scope.
    fn compare(&self, (a, x): &(u32, f64), (b, y): &(u32, f64)) -> std::cmp::Ordering {
        let (da, db) = (self.docs.get(*a), self.docs.get(*b));
        // Both paths share the `cards/` prefix, so compare `<card_id>.md`.
        y.partial_cmp(x)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| path(da).cmp(path(db)))
            .then_with(|| self.scopes[da.scope as usize].cmp(&self.scopes[db.scope as usize]))
    }
    /// The best `keep` hits in order. Only the kept hits are fully sorted.
    pub fn top(&self, mut hits: Vec<(u32, f64)>, keep: usize) -> Vec<(u32, f64)> {
        if hits.len() > keep {
            hits.select_nth_unstable_by(keep, |a, b| self.compare(a, b));
            hits.truncate(keep);
        }
        hits.sort_by(|a, b| self.compare(a, b));
        hits
    }
    /// `memory_bm25.Bm25Index.scores` over the visible corpus (live documents
    /// in allowed scopes), rounded as `rank` rounds them, unordered: pass the
    /// hits to [`Snapshot::top`].
    /// Statistics are summed over the allowed scopes; document frequency is
    /// counted over the same visible documents, so a retired or unreadable
    /// card never moves another caller's scores.
    pub fn score(&self, allowed: &[bool], query: &[String]) -> Vec<(u32, f64)> {
        let visible = |n: u32| {
            let d = self.docs.get(n);
            d.live && allowed.get(d.scope as usize).copied().unwrap_or(false)
        };
        let mut n: u64 = 0;
        let mut total: u64 = 0;
        for (scope, s) in self.stats.iter().enumerate() {
            if allowed.get(scope).copied().unwrap_or(false) {
                n += s.docs;
                total += s.tokens;
            }
        }
        let avg = if n == 0 { 0.0 } else { total as f64 / n as f64 };
        let avg = if avg == 0.0 { 1.0 } else { avg };
        let mut seen = HashSet::new();
        let mut out: HashMap<u32, f64> = HashMap::new();
        for term in query {
            if !seen.insert(term.as_str()) {
                continue;
            }
            let mut matched: Vec<(u32, u32)> = Vec::new();
            for segment in &self.segments {
                if let Some((docs, freqs)) = segment.postings(term) {
                    matched.extend(
                        docs.iter()
                            .zip(freqs)
                            .filter(|(d, _)| visible(**d))
                            .map(|(d, f)| (*d, *f)),
                    );
                }
            }
            if matched.is_empty() {
                continue;
            }
            let df = matched.len() as u64;
            // Same operations, in the same order, as memory_bm25.py.
            let idf = (1.0 + ((n - df) as f64 + 0.5) / (df as f64 + 0.5)).ln();
            for (doc, freq) in matched {
                let length = self.docs.get(doc).length as f64;
                let norm = K1 * (1.0 - B + B * length / avg);
                let freq = freq as f64;
                *out.entry(doc).or_insert(0.0) += idf * freq * (K1 + 1.0) / (freq + norm);
            }
        }
        out.into_iter()
            .map(|(doc, score)| (doc, text::round4(score)))
            .collect()
    }
}

/// A live card read from the store.
#[derive(Clone, Debug)]
pub struct Card {
    pub scope: String,
    pub card_id: String,
    pub revision: i64,
    pub content_hash: String,
    pub trust: String,
    pub text: String,
}
/// What the tail replay found for one changed card.
#[derive(Debug)]
pub enum Change {
    Live(Card),
    Gone { scope: String, card_id: String },
}

/// Writer-owned state behind the published snapshots.
pub struct Builder {
    snap: Snapshot,
    heads: HashMap<(u32, Arc<str>), u32>,
    scope_ids: HashMap<String, u32>,
}
impl Builder {
    pub fn empty() -> Self {
        Self {
            snap: Snapshot::default(),
            heads: HashMap::new(),
            scope_ids: HashMap::new(),
        }
    }
    /// A new main segment over `cards`, covering the log up to `watermark`.
    pub fn from_cards(cards: Vec<Card>, watermark: i64) -> Self {
        let mut b = Self::empty();
        let (postings, added) = b.add(cards);
        b.snap.segments = vec![Arc::new(postings.finish())];
        b.snap.main_docs = added;
        b.snap.indexed_seq = watermark;
        b
    }
    fn scope_id(&mut self, scope: &str) -> u32 {
        if let Some(id) = self.scope_ids.get(scope) {
            return *id;
        }
        let id = self.snap.scopes.len() as u32;
        Arc::make_mut(&mut self.snap.scopes).push(scope.into());
        Arc::make_mut(&mut self.snap.stats).push(Stats::default());
        self.scope_ids.insert(scope.into(), id);
        id
    }
    fn remove(&mut self, scope: u32, card_id: &str) {
        if let Some(old) = self.heads.remove(&(scope, Arc::from(card_id))) {
            let length = self.snap.docs.get(old).length as u64;
            self.snap.docs.kill(old);
            let s = &mut Arc::make_mut(&mut self.snap.stats)[scope as usize];
            s.docs -= 1;
            s.tokens -= length;
        }
    }
    fn add(&mut self, cards: Vec<Card>) -> (Postings, usize) {
        let mut postings = Postings::default();
        let mut added = 0;
        for card in cards {
            let scope = self.scope_id(&card.scope);
            self.remove(scope, &card.card_id);
            let fields = text::fields(&card.text, &card.card_id);
            let tokens = text::tokenize(&text::card_text(&fields, &card.text));
            let card_id: Arc<str> = card.card_id.into();
            let doc = self.snap.docs.push(Doc {
                live: true,
                scope,
                card_id: card_id.clone(),
                revision: card.revision,
                content_hash: card.content_hash.into(),
                trust: card.trust.into(),
                fields: Arc::new(fields),
                length: tokens.len() as u32,
            });
            let s = &mut Arc::make_mut(&mut self.snap.stats)[scope as usize];
            s.docs += 1;
            s.tokens += tokens.len() as u64;
            self.heads.insert((scope, card_id), doc);
            postings.add(doc, tokens);
            added += 1;
        }
        (postings, added)
    }
    /// Applies one replayed tail as one delta segment, then moves `indexed_seq`.
    pub fn apply(&mut self, changes: Vec<Change>, seq: i64) {
        let mut live = Vec::new();
        for change in changes {
            match change {
                Change::Live(card) => live.push(card),
                Change::Gone { scope, card_id } => {
                    if let Some(id) = self.scope_ids.get(&scope).copied() {
                        self.remove(id, &card_id);
                    }
                }
            }
        }
        let (postings, added) = self.add(live);
        if added > 0 {
            self.snap.delta_docs += added;
            self.snap.segments.push(Arc::new(postings.finish()));
        }
        self.snap.indexed_seq = seq;
    }
    pub fn snapshot(&self) -> Arc<Snapshot> {
        Arc::new(self.snap.clone())
    }
    fn wants_rebuild(&self) -> bool {
        self.snap.delta_docs >= REBUILD_FLOOR && self.snap.delta_docs * 10 > self.snap.main_docs
    }
}

fn card_row(r: &rusqlite::Row) -> rusqlite::Result<Card> {
    let bytes: Vec<u8> = r.get(5)?;
    Ok(Card {
        scope: r.get(0)?,
        card_id: r.get(1)?,
        revision: r.get(2)?,
        content_hash: r.get(3)?,
        trust: r.get(4)?,
        text: String::from_utf8_lossy(&bytes).into_owned(),
    })
}
/// Every live head and the watermark it is consistent with, in one read transaction.
pub fn load(db: &Connection) -> rusqlite::Result<(Vec<Card>, i64)> {
    db.execute_batch("BEGIN")?;
    let result = (|| {
        let watermark: i64 =
            db.query_row("SELECT COALESCE(max(seq),0) FROM memory_events", [], |r| {
                r.get(0)
            })?;
        let mut s = db.prepare(
            "SELECT c.scope,c.card_id,c.revision,c.content_hash,c.trust,b.bytes FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.state='live' ORDER BY c.scope,c.card_id",
        )?;
        let cards = s
            .query_map([], card_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok((cards, watermark))
    })();
    let _ = db.execute_batch("COMMIT");
    result
}
/// The card heads the log changed after `after`, and the last `seq` read.
fn tail(db: &Connection, after: i64) -> rusqlite::Result<(Vec<(String, String)>, i64)> {
    let mut keys = Vec::new();
    let mut seen = HashSet::new();
    let mut last = after;
    let mut s = db.prepare_cached(
        "SELECT seq,scope,card_id FROM memory_events WHERE seq>?1 AND revision IS NOT NULL ORDER BY seq LIMIT ?2",
    )?;
    loop {
        let rows = s
            .query_map(rusqlite::params![last, WINDOW], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, Option<String>>(1)?,
                    r.get::<_, Option<String>>(2)?,
                ))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        for (seq, scope, card_id) in &rows {
            last = *seq;
            if let (Some(scope), Some(card_id)) = (scope, card_id) {
                if seen.insert((scope.clone(), card_id.clone())) {
                    keys.push((scope.clone(), card_id.clone()));
                }
            }
        }
        if (rows.len() as i64) < WINDOW {
            break;
        }
    }
    // Events that change no head (grants, handoffs, captures) still move the watermark.
    let top: i64 = db.query_row("SELECT COALESCE(max(seq),0) FROM memory_events", [], |r| {
        r.get(0)
    })?;
    Ok((keys, last.max(top)))
}
fn heads(db: &Connection, keys: Vec<(String, String)>) -> rusqlite::Result<Vec<Change>> {
    let mut s = db.prepare_cached(
        "SELECT c.scope,c.card_id,c.revision,c.content_hash,c.trust,b.bytes FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope=?1 AND c.card_id=?2 AND c.state='live'",
    )?;
    let mut out = Vec::with_capacity(keys.len());
    for (scope, card_id) in keys {
        let mut rows = s.query_map([&scope, &card_id], card_row)?;
        out.push(match rows.next().transpose()? {
            Some(card) => Change::Live(card),
            None => Change::Gone { scope, card_id },
        });
    }
    Ok(out)
}

/// Index counters for `/metrics`.
#[derive(Clone, Debug, Default)]
pub struct Metrics {
    /// The highest `seq` the memory writer has committed.
    pub committed_seq: i64,
    pub publications: u64,
    pub failures: u64,
    pub rebuilds: u64,
    pub rebuild_failures: u64,
    /// The last full build, at startup or in the background.
    pub build_seconds_last: f64,
    pub build_docs_last: u64,
}

/// A finished background build, sent to the memory writer to replay its tail.
pub struct Rebuilt {
    pub result: Result<Builder, String>,
    pub seconds: f64,
}

/// Test hook: (loaded, release).
#[cfg(test)]
pub type Gate = (std::sync::mpsc::Sender<()>, std::sync::mpsc::Receiver<()>);

/// The shared handle: published snapshots, the `indexed_seq` watch and the
/// state the memory writer takes over when it starts.
pub struct Index {
    current: Mutex<Arc<Snapshot>>,
    seq: watch::Sender<i64>,
    builder: Mutex<Option<Builder>>,
    rebuilds: Mutex<Option<mpsc::WeakSender<super::writer::Job>>>,
    path: PathBuf,
    pub metrics: Mutex<Metrics>,
    /// Test hook: the next this many publications fail after their commit.
    #[cfg(test)]
    pub fail: std::sync::atomic::AtomicUsize,
    /// Test hook: a background build reports that it has read the store,
    /// then waits for a release before it hands its result to the writer.
    #[cfg(test)]
    pub gate: Mutex<Option<Gate>>,
}
impl Index {
    /// Builds the index from the store, as at every restart.
    pub fn open(db: &Connection, path: PathBuf) -> rusqlite::Result<Arc<Self>> {
        let started = Instant::now();
        let (cards, watermark) = load(db)?;
        let count = cards.len() as u64;
        let builder = Builder::from_cards(cards, watermark);
        let snapshot = builder.snapshot();
        let (seq, _) = watch::channel(watermark);
        Ok(Arc::new(Self {
            current: Mutex::new(snapshot),
            seq,
            builder: Mutex::new(Some(builder)),
            rebuilds: Mutex::new(None),
            path,
            metrics: Mutex::new(Metrics {
                committed_seq: watermark,
                build_seconds_last: started.elapsed().as_secs_f64(),
                build_docs_last: count,
                ..Default::default()
            }),
            #[cfg(test)]
            fail: Default::default(),
            #[cfg(test)]
            gate: Mutex::new(None),
        }))
    }
    pub fn current(&self) -> Arc<Snapshot> {
        self.current.lock().unwrap().clone()
    }
    /// Waits up to `limit` for `indexed_seq` to reach `min_seq`.
    pub async fn wait(&self, min_seq: i64, limit: Duration) -> Arc<Snapshot> {
        let mut seen = self.seq.subscribe();
        let _ = tokio::time::timeout(limit, seen.wait_for(|s| *s >= min_seq)).await;
        self.current()
    }
    pub(super) fn attach(&self, sender: mpsc::WeakSender<super::writer::Job>) {
        *self.rebuilds.lock().unwrap() = Some(sender);
    }
    /// The writer-side state. The memory writer takes it once, at start.
    pub(super) fn publisher(self: &Arc<Self>) -> Option<Publisher> {
        let builder = self.builder.lock().unwrap().take()?;
        Some(Publisher {
            index: self.clone(),
            builder,
            rebuilding: false,
        })
    }
}

/// Publication, owned by the memory writer thread.
pub struct Publisher {
    index: Arc<Index>,
    builder: Builder,
    rebuilding: bool,
}
impl Publisher {
    fn publish(&self) {
        let snapshot = self.builder.snapshot();
        let seq = snapshot.indexed_seq;
        *self.index.current.lock().unwrap() = snapshot;
        self.index.metrics.lock().unwrap().publications += 1;
        self.index.seq.send_replace(seq);
    }
    fn catch_up(&mut self, db: &Connection) -> rusqlite::Result<()> {
        #[cfg(test)]
        {
            use std::sync::atomic::Ordering;
            if self
                .index
                .fail
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
                .is_ok()
            {
                return Err(rusqlite::Error::InvalidQuery);
            }
        }
        let (keys, last) = tail(db, self.builder.snap.indexed_seq)?;
        if last == self.builder.snap.indexed_seq && keys.is_empty() {
            return Ok(());
        }
        let changes = heads(db, keys)?;
        self.builder.apply(changes, last);
        Ok(())
    }
    /// After every committed batch, once its commands have been answered.
    pub fn after_commit(&mut self, db: &Connection) {
        if let Ok(top) = db.query_row("SELECT COALESCE(max(seq),0) FROM memory_events", [], |r| {
            r.get::<_, i64>(0)
        }) {
            self.index.metrics.lock().unwrap().committed_seq = top;
        }
        if self.builder.snap.stale {
            // The last good snapshot keeps answering, marked stale, until a
            // rebuild from the store replaces it.
            self.rebuild();
            return;
        }
        match self.catch_up(db) {
            Ok(()) => {
                self.publish();
                if self.builder.wants_rebuild() {
                    self.rebuild();
                }
            }
            Err(_) => self.fail(),
        }
    }
    fn fail(&mut self) {
        self.builder.snap.stale = true;
        self.publish();
        self.index.metrics.lock().unwrap().failures += 1;
        self.rebuild();
    }
    /// Starts one background build from the store, unless one is running.
    fn rebuild(&mut self) {
        if self.rebuilding {
            return;
        }
        let Some(sender) = self.index.rebuilds.lock().unwrap().clone() else {
            return;
        };
        self.rebuilding = true;
        let path = self.index.path.clone();
        #[cfg(test)]
        let gate = self.index.gate.lock().unwrap().take();
        let spawned = std::thread::Builder::new()
            .name("memory-index-rebuild".into())
            .spawn(move || {
                let started = Instant::now();
                let result = crate::schema::audit::open(
                    &path,
                    OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
                )
                .map_err(|e| e.to_string())
                .and_then(|c| load(&c).map_err(|e| e.to_string()))
                .map(|(cards, watermark)| Builder::from_cards(cards, watermark));
                #[cfg(test)]
                if let Some((loaded, release)) = gate {
                    let _ = loaded.send(());
                    let _ = release.recv();
                }
                let rebuilt = Rebuilt {
                    result,
                    seconds: started.elapsed().as_secs_f64(),
                };
                if let Some(sender) = sender.upgrade() {
                    let _ = sender.blocking_send(super::writer::Job::Rebuilt(Box::new(rebuilt)));
                }
            });
        if spawned.is_err() {
            self.rebuilding = false;
        }
    }
    /// A background build finished: replay the log after its watermark onto
    /// it, then publish it in place of the old segments.
    pub fn installed(&mut self, rebuilt: Rebuilt, db: &Connection) {
        self.rebuilding = false;
        let builder = match rebuilt.result {
            Ok(b) => b,
            Err(_) => {
                self.index.metrics.lock().unwrap().rebuild_failures += 1;
                return;
            }
        };
        {
            let mut m = self.index.metrics.lock().unwrap();
            m.rebuilds += 1;
            m.build_seconds_last = rebuilt.seconds;
            m.build_docs_last = builder.snap.main_docs as u64;
        }
        let previous = std::mem::replace(&mut self.builder, builder);
        match self.catch_up(db) {
            Ok(()) => self.publish(),
            Err(_) => {
                // Keep answering from the last good snapshot; the next commit retries.
                self.builder = previous;
                self.builder.snap.stale = true;
                self.index.metrics.lock().unwrap().failures += 1;
                self.publish();
            }
        }
    }
}
