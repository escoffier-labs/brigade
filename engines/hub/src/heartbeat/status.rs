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
    let rows = tx.query("SELECT node_id,run_id,repo,seat,harness,state,ts,sequence,digest,exit_status,capability_fingerprint,repo_identity,received_at,context FROM (SELECT e.node_id,e.run_id,e.sequence,e.digest,ROW_NUMBER() OVER (PARTITION BY CASE WHEN e.harness='grokbot' THEN 'grokbot' ELSE 'node:'||e.node_id END,e.run_id ORDER BY e.sequence DESC,e.received_at DESC,e.digest DESC) AS rn FROM events e) AS ranked JOIN events AS latest USING(node_id,run_id,sequence,digest) WHERE rn=1 ORDER BY node_id,run_id",&[])?;
    let mut runs = Vec::new();
    for row in &rows {
        let state = string(&row[5]);
        let received = timestamp(string(&row[12]));
        if received.is_none() {
            return Ok(None);
        }
        let age = now - received.unwrap().0;
        if !all && (terminal(state) || age > 1800.0) {
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
