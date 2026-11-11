//! Bounded push scope discovery. A scope-less read inspects at most 64 copies.
//! The 65th slot detects overflow, which fails closed. Name a scope to read
//! ids with more copies. Every scope-less query joins 65 real or stand-in heads, even
//! for an absent id, so revision and scan joins cannot grow with copy count.
//!
//! Threat model: this bounds work; it is not a constant-time SQLite program.
//! On bundled SQLite the warmed discovery takes 12,507 / 12,509 / 12,517 /
//! 12,634 / 12,637 VM instructions for 0 / 1 / 5 / 64 / 70 withheld copies:
//! about two extra instructions per copy up to the 65-slot cap (130 total).
//! The regression allows at most 160 instructions of spread. An aggregate-min
//! probe increased the spread, so retain the simpler ordered seek. Do not pad
//! SQLite opcodes or impose a response delay to simulate constant-time behavior.
//! Two interleaved HTTP runs of 500 reads per case on a local test hub measured
//! negative-path p99-minus-p50 jitter of 569-1,658 microseconds, versus 47-55
//! microseconds between absent and 70-copy medians. This residual is below the
//! noise of individual HTTP reads in that experiment, before remote network
//! jitter. Repeated sampling can reduce noise: these measurements do not prove
//! resistance to a statistical remote timing attack and require security review.
//! Without memory_cards_id the planner can fall back to a scan for correctness;
//! the indexed-work bound applies to the normal schema, not a damaged database.
use super::{Credential, ReadTx, Value, num, search, text};

const SLOTS: usize = 65;

pub(super) fn push_scopes<T: ReadTx + ?Sized>(
    tx: &T,
    credential: &Credential,
    card_id: &str,
    scope: Option<&str>,
    ruleset: &str,
) -> rusqlite::Result<Vec<String>> {
    if let Some(scope) = scope {
        return scoped_push_scopes(tx, credential, card_id, scope, ruleset);
    }
    let params = vec![
        Value::Text(card_id.into()),
        Value::Text(ruleset.into()),
        Value::Text(credential.principal().into()),
        Value::Text(credential.class().into()),
    ];
    let rows = tx.query(
        &format!(
            "WITH RECURSIVE walk(n,cursor) AS (VALUES(0,'') UNION ALL \
             SELECT n+1,COALESCE((SELECT scope FROM memory_cards \
             WHERE card_id=?1 AND scope>walk.cursor ORDER BY scope LIMIT 1),cursor) \
             FROM walk WHERE n<{SLOTS}), \
             heads AS MATERIALIZED (SELECT n,cursor,cursor!=lag(cursor,1,'') OVER (ORDER BY n) AS real FROM walk), \
             decoy AS MATERIALIZED (SELECT scope,card_id FROM memory_cards LIMIT 1), \
             pick AS MATERIALIZED (SELECT heads.n,heads.real, \
             CASE WHEN heads.real THEN heads.cursor ELSE decoy.scope END AS scope, \
             CASE WHEN heads.real THEN ?1 ELSE decoy.card_id END AS card_id \
             FROM heads LEFT JOIN decoy ON 1 WHERE heads.n>0) \
             SELECT c.scope,pick.real,COALESCE(c.state='live' AND {} AND r.scan_state='clean' AND s.state='clean',0) \
             FROM pick LEFT JOIN memory_cards c ON c.scope=pick.scope AND c.card_id=pick.card_id \
             LEFT JOIN memory_card_revisions r ON r.scope=c.scope AND r.card_id=c.card_id AND r.revision=c.revision \
             LEFT JOIN memory_scans s ON s.content_hash=c.content_hash AND s.ruleset_version=?2 ORDER BY pick.n",
            search::push_trust_sql(3)
        ),
        &params,
    )?;
    if rows.last().is_some_and(|r| num(&r[1]) == Some(1)) {
        return Ok(Vec::new());
    }
    Ok(rows
        .into_iter()
        .filter(|r| num(&r[1]) == Some(1) && num(&r[2]) == Some(1))
        .map(|r| text(&r[0]))
        .collect())
}

// An explicit scope has at most one head. Preserve its existing one-row padding.
fn scoped_push_scopes<T: ReadTx + ?Sized>(
    tx: &T,
    credential: &Credential,
    card_id: &str,
    scope: &str,
    ruleset: &str,
) -> rusqlite::Result<Vec<String>> {
    let params = vec![
        Value::Text(card_id.into()),
        Value::Text(ruleset.into()),
        Value::Text(credential.principal().into()),
        Value::Text(credential.class().into()),
        Value::Text(scope.into()),
    ];
    let hit = "SELECT scope,card_id,1 AS real FROM memory_cards WHERE scope=?5 AND card_id=?1";
    let rows = tx.query(
        &format!(
            "WITH hit AS ({hit}), pick AS (SELECT scope,card_id,real FROM hit UNION ALL SELECT scope,card_id,0 FROM (SELECT scope,card_id FROM memory_cards LIMIT 1) WHERE NOT EXISTS (SELECT 1 FROM hit)) \
             SELECT c.scope,pick.real,COALESCE(c.state='live' AND {} AND r.scan_state='clean' AND s.state='clean',0) \
             FROM pick JOIN memory_cards c ON c.scope=pick.scope AND c.card_id=pick.card_id \
             LEFT JOIN memory_card_revisions r ON r.scope=c.scope AND r.card_id=c.card_id AND r.revision=c.revision \
             LEFT JOIN memory_scans s ON s.content_hash=c.content_hash AND s.ruleset_version=?2 ORDER BY c.scope",
            search::push_trust_sql(3)
        ),
        &params,
    )?;
    Ok(rows
        .into_iter()
        .filter(|r| num(&r[1]) == Some(1) && num(&r[2]) == Some(1))
        .map(|r| text(&r[0]))
        .collect())
}
