//! Latest event per run, receipt-time aging and T3 observation freshness.
use super::*;
const NAMES: [&str; 14] = [
    "node_id",
    "run_id",
    "repo",
    "seat",
    "harness",
    "state",
    "ts",
    "sequence",
    "digest",
    "exit_status",
    "capability_fingerprint",
    "repo_identity",
    "received_at",
    "context",
];
fn terminal(s: &str) -> bool {
    [
        ".completed",
        ".failed",
        ".interrupted",
        ".cancelled",
        ".canceled",
        ".cancel-acknowledged",
        ".timed_out",
        ".timeout",
        ".orphaned",
        ".expired",
    ]
    .iter()
    .any(|suffix| s.ends_with(suffix))
}
fn fence_error(message: &str) -> Reply {
    Reply::new(
        503,
        &[
            ("error", quote(message)),
            ("reason", quote("claim-fence-refused")),
        ],
    )
}
// The history view (`?all=1`) returns every run, so it must rank the journal.
// It stays the single statement that names all events at once, and the parity
// test keeps it as the oracle for the active view below.
const HISTORY: &str = "SELECT node_id,run_id,repo,seat,harness,state,ts,sequence,digest,exit_status,capability_fingerprint,repo_identity,received_at,context FROM (SELECT e.node_id,e.run_id,e.sequence,e.digest,ROW_NUMBER() OVER (PARTITION BY CASE WHEN e.harness='grokbot' THEN 'grokbot' ELSE 'node:'||e.node_id END,e.run_id ORDER BY e.sequence DESC,e.received_at DESC,e.digest DESC) AS rn FROM events e) AS ranked JOIN events AS latest USING(node_id,run_id,sequence,digest) WHERE rn=1 ORDER BY node_id,run_id";
// The active view holds no terminal run and no run whose latest event is older
// than the active window, so only runs with an event inside the window can
// appear. `events_received_at` finds those runs without touching the rest of
// the journal. `INDEXED BY` pins that plan: without it the planner prefers a
// covering walk of `events_run_seq` for the DISTINCT, which is the full-table
// read this exists to avoid.
const NODE_CANDIDATES: &str = "SELECT DISTINCT node_id,run_id FROM events INDEXED BY events_received_at WHERE received_at>=? AND harness IS NOT 'grokbot'";
// Grok Bot revisions are hub-owned and keyed by run_id alone, whichever node
// reported them, so their candidates are too. This takes the same window
// index: the partial Grok Bot index covers every revision ever reported.
const JOB_CANDIDATES: &str = "SELECT DISTINCT run_id FROM events INDEXED BY events_received_at WHERE received_at>=? AND harness='grokbot'";
// The winning row of one partition: latest sequence, then latest receipt, then
// larger digest, exactly as the history ranking orders a partition.
const NODE_LATEST: &str = "SELECT node_id,run_id,repo,seat,harness,state,ts,sequence,digest,exit_status,capability_fingerprint,repo_identity,received_at,context FROM events WHERE node_id=? AND run_id=? AND harness IS NOT 'grokbot' ORDER BY sequence DESC,received_at DESC,digest DESC LIMIT 1";
const JOB_LATEST: &str = "SELECT node_id,run_id,repo,seat,harness,state,ts,sequence,digest,exit_status,capability_fingerprint,repo_identity,received_at,context FROM events WHERE harness='grokbot' AND run_id=? ORDER BY sequence DESC,received_at DESC,digest DESC LIMIT 1";
/// How long a receipt keeps a run in the active view.
const ACTIVE_SECONDS: f64 = 1800.0;
/// A `received_at` that carries a UTC offset compares as local time against a
/// UTC bound, and an offset may put that local clock up to a day from UTC, so
/// the candidate scan starts a day before the window. Widening the scan can
/// only add candidates; `list` applies the exact parsed-age test per row.
const ZONE_SLACK_SECONDS: f64 = 86400.0;
/// The rows the active view ranks: one winning row per candidate partition,
/// through `events_run_seq` (node, run, sequence) and the Grok Bot partial
/// index. Terminal winners are returned too; `list` drops them, as the
/// history ranking always has.
fn active(tx: &dyn ReadTx, now: f64) -> rusqlite::Result<Vec<Vec<Value>>> {
    let cutoff = text(&iso(now - ACTIVE_SECONDS - ZONE_SLACK_SECONDS));
    let mut rows = Vec::new();
    for candidate in tx.query(NODE_CANDIDATES, std::slice::from_ref(&cutoff))? {
        rows.extend(tx.query(NODE_LATEST, &candidate)?);
    }
    for candidate in tx.query(JOB_CANDIDATES, std::slice::from_ref(&cutoff))? {
        rows.extend(tx.query(JOB_LATEST, &candidate)?);
    }
    // SQLite's ORDER BY node_id, run_id uses BINARY collation, which is a byte
    // comparison like `str`'s. A run whose history spans both partitions tie-
    // breaks the way the ranking's partition key does: `grokbot` sorts before
    // `node:`.
    rows.sort_by(|a, b| {
        (string(&a[0]), string(&a[1]), string(&a[4]) != "grokbot").cmp(&(
            string(&b[0]),
            string(&b[1]),
            string(&b[4]) != "grokbot",
        ))
    });
    Ok(rows)
}
pub(super) fn list(tx: &dyn ReadTx, all: bool, now: f64, stale: i64) -> Result<Option<Reply>> {
    // Match Python's exact sqlite_master comparison, including extra indexes
    // and triggers. The schema owner supplies refusal text on any mismatch.
    let normalize = |s: &str| {
        s.split(py_space)
            .filter(|p| !p.is_empty())
            .collect::<Vec<_>>()
            .join(" ")
    };
    for (table, schema, indexes) in [
        (
            "claim_fences",
            "CREATE TABLE claim_fences ( target TEXT NOT NULL PRIMARY KEY, generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 1 AND generation <= 9223372036854775807) )",
            &[][..],
        ),
        (
            "claim_retired_holders",
            "CREATE TABLE claim_retired_holders ( target TEXT NOT NULL, owner_node TEXT NOT NULL, holder_token TEXT NOT NULL, generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 1 AND generation <= 9223372036854775807), retired_at REAL NOT NULL CHECK (typeof(retired_at) IN ('integer', 'real') AND retired_at >= 0 AND retired_at <= 1.7976931348623157e308), PRIMARY KEY (target, owner_node, holder_token) )",
            &[
                "CREATE INDEX claim_retired_holders_age ON claim_retired_holders (retired_at)",
                "CREATE INDEX claim_retired_holders_node_age ON claim_retired_holders (owner_node, retired_at)",
            ][..],
        ),
    ] {
        let rows = tx.query(
            "SELECT type, sql FROM sqlite_master WHERE tbl_name=? COLLATE NOCASE",
            &[text(table)],
        )?;
        let tables: Vec<_> = rows.iter().filter(|r| string(&r[0]) == "table").collect();
        let mut actual: Vec<_> = rows
            .iter()
            .filter(|r| {
                string(&r[0]) != "table" && !(string(&r[0]) == "index" && r[1] == Value::Null)
            })
            .map(|r| (string(&r[0]).to_string(), normalize(string(&r[1]))))
            .collect();
        actual.sort();
        let mut declared: Vec<_> = indexes
            .iter()
            .map(|s| ("index".to_string(), normalize(s)))
            .collect();
        declared.sort();
        if tables.len() != 1 || normalize(string(&tables[0][1])) != schema || actual != declared {
            return Ok(None);
        }
    }
    for (sql, message, limit) in [
        (
            "SELECT COUNT(*) FROM claim_fences WHERE typeof(generation)!='integer' OR generation<1",
            "claim generation fence is missing or corrupt",
            0,
        ),
        (
            "SELECT COUNT(*) FROM claims c LEFT JOIN claim_fences f ON f.target=c.target WHERE f.target IS NULL",
            "claim generation fence is missing or corrupt",
            0,
        ),
        (
            "SELECT COUNT(*) FROM claim_fences",
            "claim generation fence capacity exceeded",
            100000,
        ),
        (
            "SELECT COUNT(*) FROM claim_retired_holders r LEFT JOIN claim_fences f ON f.target=r.target WHERE f.target IS NULL OR typeof(r.generation)!='integer' OR r.generation<1 OR r.generation>f.generation OR typeof(r.owner_node)!='text' OR length(r.owner_node) NOT BETWEEN 1 AND 128 OR typeof(r.holder_token)!='text' OR length(r.holder_token) NOT BETWEEN 1 AND 128 OR typeof(r.retired_at) NOT IN ('integer','real') OR r.retired_at<0 OR r.retired_at>1.7976931348623157e308",
            "claim retired-holder fence is missing or corrupt",
            0,
        ),
        (
            "SELECT COUNT(*) FROM claim_retired_holders",
            "claim retired-holder fence capacity exceeded",
            1000000,
        ),
    ] {
        if matches!(&tx.query(sql,&[])?[0][0],Value::Integer(i) if *i>limit) {
            return Ok(Some(fence_error(message)));
        }
    }
    let rows = if all {
        tx.query(HISTORY, &[])?
    } else {
        active(tx, now)?
    };
    let Some(runs) = project(tx, &rows, all, now, stale)? else {
        return Ok(None);
    };
    let count = sql_json(&tx.query("SELECT COUNT(*) FROM claim_retired_holders", &[])?[0][0]);
    Ok(Some(Reply::new(
        200,
        &[
            (
                "claim_fencing",
                object(&[
                    ("retired_holder_rows", count),
                    ("retired_holder_horizon_seconds", "604800".into()),
                ]),
            ),
            ("runs", array(runs)),
        ],
    )))
}
/// Render the reply's run objects, dropping what the view excludes and
/// resolving a blank seat from the run's own history. `None` means a stored
/// value is outside the spellings this native projection owns, so the request
/// goes to Python whole.
fn project(
    tx: &dyn ReadTx,
    rows: &[Vec<Value>],
    all: bool,
    now: f64,
    stale: i64,
) -> Result<Option<Vec<String>>> {
    let mut runs = Vec::new();
    for row in rows {
        let state = string(&row[5]);
        let received = timestamp(string(&row[12]));
        if received.is_none() {
            return Ok(None);
        }
        let age = now - received.unwrap().0;
        if !all && (terminal(state) || age > ACTIVE_SECONDS) {
            continue;
        }
        let mut fields: Vec<(&str, String)> = NAMES
            .iter()
            .zip(row)
            .map(|(k, v)| (*k, sql_json(v)))
            .collect();
        if row[13] != Value::Null {
            let Some(context) = stored_json(string(&row[13])) else {
                return Ok(None);
            };
            fields[13].1 = json_wire(&context);
        }
        let seat = string(&row[3]);
        if py_trim(seat).is_empty() || py_trim(seat) == "-" {
            let seats = if string(&row[4]) == "grokbot" {
                tx.query("SELECT seat FROM events WHERE harness='grokbot' AND run_id=? AND seat IS NOT NULL AND trim(seat)!='' AND seat!='-' ORDER BY sequence DESC,received_at DESC,digest DESC LIMIT 1",&[row[1].clone()])?
            } else {
                tx.query("SELECT seat FROM events WHERE node_id=? AND run_id=? AND seat IS NOT NULL AND trim(seat)!='' AND seat!='-' ORDER BY sequence DESC,received_at DESC,digest DESC LIMIT 1",&row[..2])?
            };
            if let Some(seat) = seats.first() {
                fields[3].1 = sql_json(&seat[0]);
            }
        }
        let mut projected = state;
        if !terminal(state) && string(&row[4]) == "t3-fleet" && state != "external.unknown" {
            match timestamp(string(&row[6])) {
                Some((time, true)) if now - time >= -300.0 => {
                    if now - time > 900.0 {
                        projected = "run.stale";
                    }
                }
                Some(_) => projected = "external.unknown",
                None => return Ok(None),
            }
        }
        if all && !terminal(state) && projected != "external.unknown" && age > stale as f64 {
            projected = "run.stale";
        }
        if !all && projected == "run.stale" {
            continue;
        }
        if projected != state {
            fields[5].1 = quote(projected);
            fields.push(("original_state", quote(state)));
        }
        runs.push(object(&fields));
    }
    Ok(Some(runs))
}
/// Common ISO calendar spellings. Exotic datetime forms stay Python-owned.
fn timestamp(s: &str) -> Option<(f64, bool)> {
    let b = s.as_bytes();
    if b.len() < 19
        || b[4] != b'-'
        || b[7] != b'-'
        || !matches!(b[10], b'T' | b' ')
        || b[13] != b':'
        || b[16] != b':'
    {
        return None;
    }
    let number = |a: usize, n: usize| -> Option<i64> {
        let s = b.get(a..a + n)?;
        if !s.iter().all(u8::is_ascii_digit) {
            return None;
        }
        std::str::from_utf8(s).ok()?.parse().ok()
    };
    let (y, m, d, h, min, sec) = (
        number(0, 4)?,
        number(5, 2)?,
        number(8, 2)?,
        number(11, 2)?,
        number(14, 2)?,
        number(17, 2)?,
    );
    let leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
    let days = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    if !(1..=9999).contains(&y)
        || !(1..=12).contains(&m)
        || d < 1
        || d > days[(m - 1) as usize]
        || h > 23
        || min > 59
        || sec > 59
    {
        return None;
    }
    let mut p = 19;
    let mut fraction = 0.0;
    if b.get(p).is_some_and(|c| matches!(c, b'.' | b',')) {
        p += 1;
        let start = p;
        while b.get(p).is_some_and(u8::is_ascii_digit) {
            p += 1;
        }
        if p == start {
            return None;
        }
        let digits = &s[start..p.min(start + 6)];
        fraction = digits.parse::<f64>().ok()? / 10f64.powi(digits.len() as i32);
    }
    let (offset, zoned) = match &b[p..] {
        [] => (0, true), // received_at naive is treated as UTC. Observation checked below.
        b"Z" => (0, true),
        [sign @ (b'+' | b'-'), h1, h2, b':', m1, m2]
            if [h1, h2, m1, m2].iter().all(|b| b.is_ascii_digit()) =>
        {
            let h = i64::from(h1 - b'0') * 10 + i64::from(h2 - b'0');
            let m = i64::from(m1 - b'0') * 10 + i64::from(m2 - b'0');
            if h > 23 || m > 59 {
                return None;
            }
            ((h * 60 + m) * 60 * if *sign == b'-' { -1 } else { 1 }, true)
        }
        _ => return None,
    };
    Some((
        (((days_from_civil(y, m, d) * 24 + h) * 60 + min) * 60 + sec - offset) as f64 + fraction,
        zoned && p < b.len(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use brigade_hub::claims::ReadConnection;
    use rusqlite::trace::{TraceEvent, TraceEventCodes};
    use std::{path::PathBuf, sync::Mutex};

    const NOW: f64 = 1767225600.0; // 2026-01-01T00:00:00Z

    /// A hub-shaped database from the Python schema owner, as every other hub
    /// fixture builds one. `BRIGADE_HUB_PYTHON` must name an interpreter with
    /// this checkout installed.
    fn database(name: &str) -> (rusqlite::Connection, PathBuf) {
        let dir = std::env::temp_dir().join(format!("hub-status-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("hub.db");
        let python = std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into());
        let status = std::process::Command::new(python)
            .args(["-c", "import sys; from pathlib import Path; from brigade import fleet_hub as h; h.init_db(Path(sys.argv[1])).close()"])
            .arg(&path)
            .status()
            .unwrap();
        assert!(status.success());
        let db = rusqlite::Connection::open(&path).unwrap();
        (db, path)
    }

    fn rows(db: &rusqlite::Connection, sql: &str) -> Vec<Vec<Value>> {
        let mut statement = db.prepare(sql).unwrap();
        let count = statement.column_count();
        statement
            .query_map([], |r| (0..count).map(|i| r.get(i)).collect())
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap()
    }

    #[allow(clippy::too_many_arguments)]
    fn event(
        db: &rusqlite::Connection,
        row: (&str, &str, i64, &str),
        seat: Option<&str>,
        harness: Option<&str>,
        state: &str,
        ts: &str,
        received: &str,
    ) {
        db.execute(
            "INSERT INTO events (node_id,run_id,sequence,digest,repo,seat,harness,state,ts,exit_status,capability_fingerprint,repo_identity,context,received_at) VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)",
            rusqlite::params![row.0, row.1, row.2, row.3, "escoffier-labs/brigade", seat, harness, state, ts, received],
        )
        .unwrap();
    }

    fn at(offset: f64) -> String {
        iso(NOW + offset)
    }

    /// The rows the pre-fix status read ranked, rendered through the same
    /// projection the active view uses: the parity oracle.
    fn oracle(db: &rusqlite::Connection, all: bool, now: f64, stale: i64) -> Vec<String> {
        let tx = ReadConnection(db);
        project(&tx, &rows(db, HISTORY), all, now, stale)
            .ok()
            .unwrap()
            .unwrap()
    }

    fn run_ids(reply: &Reply) -> Vec<String> {
        let body: Json = serde_json::from_str(&reply.body).unwrap();
        body["runs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|run| run["run_id"].as_str().unwrap().to_string())
            .collect()
    }

    /// Ties on sequence, a run split across both partitions, blank and hyphen
    /// seats, terminal and live runs, a UTC-offset receipt, and receipts older
    /// than the candidate window. The active view must return exactly what the
    /// history ranking returns after its own filters.
    #[test]
    fn active_view_matches_the_history_ranking() {
        let (db, _) = database("parity");
        let tx = ReadConnection(&db);
        event(
            &db,
            ("node-a", "run-tie", 1, "t0"),
            Some("seat-a"),
            Some("codex"),
            "run.created",
            &at(-600.0),
            &at(-599.0),
        );
        event(
            &db,
            ("node-a", "run-tie", 2, "aaa"),
            Some("seat-a"),
            Some("codex"),
            "run.created",
            &at(-500.0),
            &at(-500.0),
        );
        event(
            &db,
            ("node-a", "run-tie", 2, "bbb"),
            Some("seat-b"),
            Some("codex"),
            "run.progress",
            &at(-400.0),
            &at(-400.0),
        );
        // One run_id in both partitions on one node, plus a blank-seat winner.
        event(
            &db,
            ("node-a", "run-shared", 0, "n0"),
            Some("seat-history"),
            Some("codex"),
            "run.created",
            &at(-300.0),
            &at(-300.0),
        );
        event(
            &db,
            ("node-a", "run-shared", 1, "n1"),
            Some(""),
            Some("codex"),
            "run.created",
            &at(-200.0),
            &at(-200.0),
        );
        event(
            &db,
            ("node-a", "run-shared", 1, "g1"),
            Some("grok-seat"),
            Some("grokbot"),
            "grokbot.running",
            &at(-200.0),
            &at(-150.0),
        );
        // Grok Bot revisions rank by run_id across nodes.
        event(
            &db,
            ("node-c", "run-grok", 3, "x0"),
            Some("-"),
            Some("grokbot"),
            "grokbot.queued",
            &at(-120.0),
            &at(-120.0),
        );
        event(
            &db,
            ("node-d", "run-grok", 3, "x1"),
            None,
            Some("grokbot"),
            "grokbot.running",
            &at(-110.0),
            &at(-110.0),
        );
        event(
            &db,
            ("node-d", "run-grok", 2, "x2"),
            Some("seat-g"),
            Some("grokbot"),
            "grokbot.queued",
            &at(-200.0),
            &at(-200.0),
        );
        // Live, terminal, expired, offset, t3 projections, and a null harness.
        event(
            &db,
            ("node-b", "run-live", 1, "l0"),
            Some("seat-l"),
            Some("codex"),
            "run.created",
            &at(-100.0),
            &at(-100.0),
        );
        event(
            &db,
            ("node-b", "run-live", 2, "l1"),
            Some("-"),
            Some("codex"),
            "run.progress",
            &at(-90.0),
            &at(-90.0),
        );
        event(
            &db,
            ("node-b", "run-terminal", 1, "c1"),
            Some("seat-c"),
            Some("codex"),
            "run.completed",
            &at(-80.0),
            &at(-80.0),
        );
        event(
            &db,
            ("node-b", "run-old", 1, "o1"),
            Some("seat-o"),
            Some("codex"),
            "run.progress",
            &at(-20000.0),
            &at(-20000.0),
        );
        let local = at(-30.0 - 7200.0).replace("+00:00", "-02:00");
        event(
            &db,
            ("node-b", "run-offset", 1, "z1"),
            Some("seat-z"),
            Some("codex"),
            "run.progress",
            &at(-30.0),
            &local,
        );
        event(
            &db,
            ("node-b", "run-stale-t3", 1, "s1"),
            Some("seat-s"),
            Some("t3-fleet"),
            "run.progress",
            &at(-1000.0),
            &at(-60.0),
        );
        event(
            &db,
            ("node-b", "run-future", 1, "f1"),
            Some("seat-f"),
            Some("t3-fleet"),
            "run.progress",
            &at(1000.0),
            &at(-60.0),
        );
        event(
            &db,
            ("node-b", "run-null", 1, "h1"),
            None,
            None,
            "run.progress",
            &at(-50.0),
            &at(-50.0),
        );
        event(
            &db,
            ("node-b", "run-ancient", 1, "a1"),
            Some("seat-a"),
            Some("codex"),
            "run.progress",
            &at(-200000.0),
            &at(-200000.0),
        );
        event(
            &db,
            ("node-e", "run-ancient-grok", 1, "ag"),
            Some("seat-a"),
            Some("grokbot"),
            "grokbot.queued",
            &at(-200000.0),
            &at(-200000.0),
        );

        let active_rows = active(&tx, NOW).unwrap();
        let active_view = project(&tx, &active_rows, false, NOW, 86400)
            .ok()
            .unwrap()
            .unwrap();
        assert_eq!(active_view, oracle(&db, false, NOW, 86400));

        let reply = list(&tx, false, NOW, 86400).ok().unwrap().unwrap();
        assert_eq!(reply.status, 200);
        let ids = run_ids(&reply);
        let has = |id: &str| ids.iter().any(|run| run == id);
        // The offset receipt is live, the ancient runs never become candidates,
        // and terminal, expired, and stale-projected runs stay out.
        assert!(has("run-offset"));
        assert!(!has("run-ancient"));
        assert!(!has("run-ancient-grok"));
        assert!(!has("run-terminal"));
        assert!(!has("run-old"));
        assert!(!has("run-stale-t3"));
        // The grokbot partition outranks the node partition for a shared run.
        assert_eq!(ids.iter().filter(|id| *id == "run-shared").count(), 2);
        let body: Json = serde_json::from_str(&reply.body).unwrap();
        let entry = |id: &str, field: &str| {
            body["runs"]
                .as_array()
                .unwrap()
                .iter()
                .find(|run| run["run_id"] == id)
                .unwrap()[field]
                .clone()
        };
        assert_eq!(
            entry("run-future", "state"),
            Json::String("external.unknown".into())
        );
        assert_eq!(entry("run-tie", "sequence"), Json::from(2));
        assert_eq!(entry("run-tie", "digest"), Json::String("bbb".into()));
        // Blank and hyphen seats come from the run's own history, as Python does.
        let seats: Vec<String> = body["runs"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|run| run["seat"].as_str().map(str::to_owned))
            .collect();
        assert!(seats.contains(&"seat-l".to_string()));
        assert!(seats.contains(&"grok-seat".to_string()));

        // The history view keeps the terminal runs and the stale projection.
        let all_reply = list(&tx, true, NOW, 86400).ok().unwrap().unwrap();
        let all_body: Json = serde_json::from_str(&all_reply.body).unwrap();
        let all_ids = run_ids(&all_reply);
        assert!(all_ids.iter().any(|id| id == "run-terminal"));
        assert!(
            all_body["runs"]
                .as_array()
                .unwrap()
                .iter()
                .any(|run| run["run_id"] == "run-stale-t3"
                    && run["original_state"] == "run.progress")
        );
        assert_eq!(
            all_body["runs"].as_array().unwrap().len(),
            oracle(&db, true, NOW, 86400).len()
        );
    }

    static TRACED: Mutex<Vec<String>> = Mutex::new(Vec::new());

    fn record(event: TraceEvent<'_>) {
        if let TraceEvent::Stmt(_, sql) = event {
            TRACED.lock().unwrap().push(sql.to_string());
        }
    }

    /// The active read must not plan a scan of `events`. The history ranking
    /// does (`SCAN e`), which is why it stays off the polled path: this test
    /// fails on the statement the read used to run.
    #[test]
    fn active_view_plans_no_events_scan() {
        let (db, path) = database("plan");
        // Most of the journal is old terminal history, the shape production had.
        for i in 0..2000 {
            let sequence = i % 3 + 1;
            event(
                &db,
                ("node-a", &format!("run-{i:05}"), sequence, "digest"),
                Some("seat"),
                Some("codex"),
                if sequence == 3 {
                    "run.completed"
                } else {
                    "run.created"
                },
                &at(-400000.0 - i as f64),
                &at(-400000.0 - i as f64),
            );
        }
        for i in 0..5 {
            event(
                &db,
                ("node-a", &format!("live-{i}"), 1, "digest"),
                Some("seat"),
                Some("codex"),
                "run.progress",
                &at(-60.0),
                &at(-60.0),
            );
        }
        let traced = rusqlite::Connection::open(&path).unwrap();
        traced.trace_v2(TraceEventCodes::SQLITE_TRACE_STMT, Some(record));
        let reply = list(&ReadConnection(&traced), false, NOW, 86400)
            .ok()
            .unwrap()
            .unwrap();
        traced.trace_v2(TraceEventCodes::SQLITE_TRACE_STMT, None);
        assert_eq!(reply.status, 200);
        assert_eq!(run_ids(&reply).len(), 5);

        let captured = TRACED.lock().unwrap().clone();
        assert!(!captured.is_empty());
        let planner = rusqlite::Connection::open(&path).unwrap();
        for sql in captured {
            let mut statement = planner
                .prepare(&format!("EXPLAIN QUERY PLAN {sql}"))
                .unwrap();
            // Plans do not depend on the bound values; the trace text keeps
            // the placeholders.
            let bound = vec![Value::Null; sql.matches('?').count()];
            let details: Vec<String> = statement
                .query_map(rusqlite::params_from_iter(bound), |r| r.get::<_, String>(3))
                .unwrap()
                .collect::<rusqlite::Result<Vec<_>>>()
                .unwrap();
            for detail in &details {
                // A scan of `events`, under its name or the ranking's `e`
                // alias, is the work this route exists without.
                assert!(
                    !detail.starts_with("SCAN events") && detail != "SCAN e",
                    "status read scanned events: {detail} in {sql}"
                );
            }
        }
    }
}
