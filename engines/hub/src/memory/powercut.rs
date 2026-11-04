//! Power cuts on the memory file and across the authority protocol (S13a).
//! The S4a shim sits under both files. After each cut: every acknowledged
//! write is present with its idempotency record, an unacknowledged write is
//! all there or all gone, no `seq` is handed out twice, and the hub file,
//! locked for every batch, is unchanged.
use super::{
    Credential, ids,
    ops::{Action, Prepared},
    scan::Scan,
    schema,
    tests::{NODE_TOKEN, digest, hub_fixture, temp_dir},
    writer,
};
use crate::writer::{powercut as shim, powercut::DISK, worker};
use rusqlite::Connection;
use std::{
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

const NOW: i64 = 1_800_000_000_000;

fn commands() -> Vec<Prepared> {
    let node = Credential::Node {
        id: "node-1".into(),
        digest: digest(NODE_TOKEN),
    };
    let mut out = Vec::new();
    for i in 0..6 {
        // 8 KiB of text spans several pages, so frames precede the commit frame.
        let text = ids::normalize(&format!("card {i}\n{}", "x".repeat(8192)));
        out.push(Prepared {
            credential: node.clone(),
            action: Action::Put {
                scope: "fleet".into(),
                card_id: format!("card-{i}"),
                base: None,
                hash: ids::sha256_hex(text.as_bytes()),
                text,
                origin: None,
                scan: Scan::Unavailable,
                import: false,
            },
            client_event_id: ids::mint(NOW),
            request_digest: format!("digest-{i}"),
            now_ms: NOW,
        });
    }
    let text = ids::normalize("handoff after a cut");
    // Nodes hold no default handoff grant; the admin token holds every grant.
    out.push(Prepared {
        credential: Credential::Admin,
        action: Action::Handoff {
            kind: "session".into(),
            hash: ids::sha256_hex(text.as_bytes()),
            text,
            scan: Scan::Unavailable,
        },
        client_event_id: ids::mint(NOW),
        request_digest: "digest-handoff".into(),
        now_ms: NOW,
    });
    out.push(Prepared {
        credential: Credential::Admin,
        action: Action::Grant {
            principal: "agent:dot".into(),
            scope: "fleet".into(),
            rights: vec!["read".into()],
        },
        client_event_id: ids::mint(NOW),
        request_digest: "digest-grant".into(),
        now_ms: NOW,
    });
    out
}

fn hub_dump(path: &Path) -> String {
    let c = Connection::open(path).unwrap();
    let mut out = String::new();
    for sql in [
        "SELECT node_id,token_sha256,revoked_at FROM nodes ORDER BY node_id",
        "SELECT principal_id,revoked_at FROM fleet_message_principals ORDER BY principal_id",
        "SELECT revision,digest FROM fleet_policy_revisions ORDER BY revision",
        "SELECT current_revision FROM fleet_policy_meta",
        "SELECT count(*) FROM claims_probe",
    ] {
        let mut s = c.prepare(sql).unwrap();
        let n = s.column_count();
        let rows = s
            .query_map([], |r| {
                (0..n)
                    .map(|i| {
                        r.get::<_, rusqlite::types::Value>(i)
                            .map(|v| format!("{v:?}"))
                    })
                    .collect::<rusqlite::Result<Vec<_>>>()
            })
            .unwrap();
        for row in rows {
            out.push_str(&row.unwrap().join("|"));
            out.push('\n');
        }
    }
    out
}

#[derive(Default, Debug)]
struct Outcome {
    /// Storage operations of this run's phase, counted up to the cut.
    operations: usize,
    crashed: bool,
    acknowledged: usize,
    lost: usize,
}

fn cut_run(cut: usize, phase: usize, ordered: bool, normal: bool) -> Outcome {
    let dir = temp_dir();
    let hub = hub_fixture(&dir);
    let memory = dir.join("fleet-hub-memory.db");
    {
        let c = worker::open_file(&memory, true).unwrap();
        schema::migrate(&c, NOW).unwrap();
        c.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)").unwrap();
    }
    Connection::open(&hub)
        .unwrap()
        .execute_batch("PRAGMA wal_checkpoint(TRUNCATE)")
        .unwrap();
    let hub_before = hub_dump(&hub);
    shim::seed_files(&[&hub, &memory], ordered);
    DISK.with(|d| {
        let mut d = d.borrow_mut();
        d.writer = true;
        d.normal = normal;
        d.cut = if phase == 6 { 0 } else { cut };
        d.loss_mode = cut % 3;
        d.inside_sync = phase == 2;
        d.phase = phase;
    });
    // The external ledger lives outside the files the shim manages.
    let ledger = dir.join("ledger.json");
    let mut operations = 0;
    let mut acknowledged: Vec<(String, Option<i64>)> = Vec::new();
    if let (Ok(db), Ok(lock)) = (worker::open(&memory), writer::hub_lock(&hub)) {
        let checkpoints = Mutex::new(worker::open(&memory).ok());
        let mut config = writer::Config::new(memory.clone(), hub.clone(), "unused".into());
        config.max_commands = 1;
        config.hook = Some(Arc::new(move |step| {
            if step != writer::Step::Released || !matches!(phase, 2..=5) || (phase == 2 && !normal)
            {
                return;
            }
            if let Some(c) = checkpoints.lock().unwrap().as_ref() {
                DISK.with(|d| d.borrow_mut().active = matches!(phase, 3 | 4).then_some(phase));
                let _ = c.execute_batch(if phase == 4 {
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                } else {
                    "PRAGMA wal_checkpoint(PASSIVE)"
                });
                DISK.with(|d| d.borrow_mut().active = None);
            }
        }));
        let (send, receive) = tokio::sync::mpsc::channel(64);
        let mut replies = Vec::new();
        for command in commands() {
            let (reply, answer) = tokio::sync::oneshot::channel();
            let cid = command.client_event_id.clone();
            send.try_send(writer::Job::Write {
                command: Box::new(command),
                deadline: Instant::now() + Duration::from_secs(30),
                reply,
            })
            .unwrap_or_else(|_| panic!("queue"));
            replies.push((cid, answer));
        }
        drop(send);
        let metrics = Arc::new(Mutex::new(writer::Metrics::default()));
        writer::run(db, lock, receive, config, Arc::default(), metrics);
        for (cid, mut answer) in replies {
            if let Ok(Ok(reply)) = answer.try_recv() {
                if reply.status < 300 {
                    acknowledged.push((cid, reply.body["event"]["seq"].as_i64()));
                    std::fs::write(&ledger, serde_json::to_vec(&acknowledged).unwrap()).unwrap();
                }
            }
        }
    }
    let crashed = DISK.with(|d| {
        let mut d = d.borrow_mut();
        let crashed = d.crashed;
        operations = d.count;
        d.crashed = true;
        crashed
    });
    shim::restore();
    if phase == 6 {
        // A second cut while the first one's WAL is replayed.
        DISK.with(|d| {
            let mut d = d.borrow_mut();
            d.count = 0;
            d.cut = cut;
            d.active = Some(6);
        });
        if let Ok(c) = worker::open(&memory) {
            let _ = c.query_row("SELECT count(*) FROM memory_events", [], |r| {
                r.get::<_, i64>(0)
            });
        }
        DISK.with(|d| d.borrow_mut().crashed = true);
        shim::restore();
    }
    DISK.with(|d| *d.borrow_mut() = Default::default());

    let m = Connection::open(&memory).unwrap();
    for c in [&m, &Connection::open(&hub).unwrap()] {
        assert_eq!(
            c.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "ok",
            "phase={phase} cut={cut}"
        );
    }
    assert_eq!(
        hub_dump(&hub),
        hub_before,
        "hub file changed: phase={phase} cut={cut}"
    );
    let recorded: Vec<(String, Option<i64>)> = std::fs::read(&ledger)
        .ok()
        .map(|b| serde_json::from_slice(&b).unwrap())
        .unwrap_or_default();
    assert_eq!(recorded.len(), acknowledged.len());
    let mut lost = 0;
    for (cid, _) in &recorded {
        let present: bool = m
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM memory_events WHERE client_event_id=?1) AND EXISTS(SELECT 1 FROM memory_idempotency WHERE client_event_id=?1)",
                [cid],
                |r| r.get(0),
            )
            .unwrap();
        if !present {
            lost += 1;
            assert!(
                normal,
                "FULL lost an acknowledged write: ordered={ordered} phase={phase} cut={cut}"
            );
        }
    }
    // All or nothing: every event has its idempotency record and its head rows.
    let orphans: i64 = m
        .query_row(
            "SELECT (SELECT count(*) FROM memory_events e WHERE NOT EXISTS(SELECT 1 FROM memory_idempotency i WHERE i.principal=e.principal AND i.client_event_id=e.client_event_id))
                  + (SELECT count(*) FROM memory_idempotency i WHERE NOT EXISTS(SELECT 1 FROM memory_events e WHERE e.principal=i.principal AND e.client_event_id=i.client_event_id))
                  + (SELECT count(*) FROM memory_events e WHERE e.revision IS NOT NULL AND NOT EXISTS(SELECT 1 FROM memory_card_revisions r WHERE r.scope=e.scope AND r.card_id=e.card_id AND r.revision=e.revision))
                  + (SELECT count(*) FROM memory_cards c WHERE NOT EXISTS(SELECT 1 FROM memory_blobs b WHERE b.content_hash=c.content_hash))
                  + (SELECT count(*) FROM memory_handoffs h WHERE NOT EXISTS(SELECT 1 FROM memory_events e WHERE e.seq=h.seq))",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(orphans, 0, "partial command: phase={phase} cut={cut}");
    // No acknowledged seq can be handed out again.
    let (high, sequence): (i64, i64) = m
        .query_row(
            "SELECT COALESCE(max(seq),0),COALESCE((SELECT seq FROM sqlite_sequence WHERE name='memory_events'),0) FROM memory_events",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert!(sequence >= high);
    if !normal {
        for (_, seq) in &recorded {
            assert!(
                seq.is_none_or(|s| s <= high),
                "seq reuse risk: phase={phase} cut={cut}"
            );
        }
    }
    drop(m);
    std::fs::remove_dir_all(&dir).unwrap();
    Outcome {
        operations,
        crashed,
        acknowledged: recorded.len(),
        lost,
    }
}

/// Cut points spread over each phase's measured operation count: `points`
/// evenly spaced cuts plus the edges, or every operation when `points` is 0.
fn cuts(operations: usize, points: usize) -> Vec<usize> {
    if points == 0 {
        return (1..=operations + 1).collect();
    }
    let mut out: Vec<usize> = (0..=points)
        .map(|i| (operations * i / points).max(1))
        .chain([operations + 1])
        .collect();
    out.dedup();
    out
}

fn schedule(points: usize) {
    let _lock = shim::VFS_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _vfs = unsafe { shim::Registration::new() };
    for ordered in [false, true] {
        for phase in 0..8 {
            let baseline = cut_run(0, phase, ordered, false);
            assert!(!baseline.crashed);
            assert_eq!(baseline.acknowledged, commands().len());
            // Recovery cuts count reads while an earlier cut's WAL is replayed.
            let operations = if phase == 6 { 64 } else { baseline.operations };
            let (mut crashes, mut acknowledged, mut runs) = (0, 0, 0);
            for cut in cuts(operations, points) {
                let o = cut_run(cut, phase, ordered, false);
                runs += 1;
                crashes += usize::from(o.crashed || phase == 6);
                acknowledged += o.acknowledged;
            }
            eprintln!(
                "memory powercut FULL ordered={ordered} phase={phase}: operations={operations} cuts={runs} crashes={crashes} acknowledged={acknowledged} lost=0"
            );
            assert!(crashes > 0, "no cut landed in phase {phase}");
        }
    }
    let mut normal_losses = [0usize; 2];
    for (i, phase) in [1, 2].into_iter().enumerate() {
        let operations = cut_run(0, phase, false, true).operations;
        // The sync phases are short, so the control cuts at every operation.
        for cut in cuts(operations, 0) {
            normal_losses[i] += cut_run(cut, phase, false, true).lost;
        }
    }
    eprintln!(
        "memory powercut NORMAL control: acknowledged losses in sync phases {normal_losses:?}"
    );
    assert!(
        normal_losses.iter().all(|n| *n > 0),
        "NORMAL must lose acknowledged memory writes in both sync phases: {normal_losses:?}"
    );
}

#[test]
fn memory_power_cut_short() {
    schedule(8);
}

#[test]
#[ignore = "every operation cut in every phase, run alone with --ignored --exact memory::powercut::memory_power_cut_matrix"]
fn memory_power_cut_matrix() {
    schedule(0);
}

/// An `SQLITE_FULL` on the memory commit releases the hub lock at once and
/// answers the batch with 503; hub writes go on and the next batch commits.
#[test]
fn sqlite_full_on_memory_commit_releases_the_hub_lock() {
    let _lock = shim::VFS_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _vfs = unsafe { shim::Registration::new() };
    let dir = temp_dir();
    let hub = hub_fixture(&dir);
    let memory = dir.join("fleet-hub-memory.db");
    {
        let c = worker::open_file(&memory, true).unwrap();
        schema::migrate(&c, NOW).unwrap();
    }
    DISK.with(|d| d.borrow_mut().writer = true);
    let db = worker::open(&memory).unwrap();
    let lock = writer::hub_lock(&hub).unwrap();
    let hub_path = hub.clone();
    let probe = Arc::new(Mutex::new(Vec::new()));
    let seen = probe.clone();
    let mut config = writer::Config::new(memory.clone(), hub.clone(), "unused".into());
    config.max_commands = 1;
    config.hook = Some(Arc::new(move |step| {
        if step == writer::Step::MemoryLocked && seen.lock().unwrap().is_empty() {
            DISK.with(|d| d.borrow_mut().full_on_commit = true);
        }
        if step == writer::Step::Released {
            // Immediately after the turn returns, another writer gets the hub lock.
            let c = Connection::open(&hub_path).unwrap();
            c.busy_timeout(Duration::ZERO).unwrap();
            seen.lock().unwrap().push(
                c.execute_batch("INSERT INTO claims_probe(value) VALUES('claim')")
                    .is_ok(),
            );
        }
    }));
    let (send, receive) = tokio::sync::mpsc::channel(8);
    let mut answers = Vec::new();
    for command in commands().into_iter().take(2) {
        let (reply, answer) = tokio::sync::oneshot::channel();
        send.try_send(writer::Job::Write {
            command: Box::new(command),
            deadline: Instant::now() + Duration::from_secs(30),
            reply,
        })
        .unwrap_or_else(|_| panic!("queue"));
        answers.push(answer);
    }
    drop(send);
    writer::run(db, lock, receive, config, Arc::default(), Arc::default());
    DISK.with(|d| *d.borrow_mut() = Default::default());
    let first = answers[0].try_recv().unwrap();
    assert_eq!(
        first,
        Err(writer::Failure {
            status: 503,
            reason: "commit-failed"
        })
    );
    assert_eq!(answers[1].try_recv().unwrap().unwrap().status, 201);
    assert_eq!(*probe.lock().unwrap(), vec![true, true]);
    let events: i64 = Connection::open(&memory)
        .unwrap()
        .query_row("SELECT count(*) FROM memory_events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(events, 1);
    std::fs::remove_dir_all(&dir).unwrap();
}
