//! Alias lineage dating on the real scope `fleet` (#2671, #2726). Each case
//! reads the dates from a real revision history, never from a fixture.
use super::{
    alias_dating::{self, RUN_CAP},
    index::{self, Change},
    merge_drift_tests::*,
    proposal_tests::*,
    ranking_gate_tests::Real,
    tests::*,
    *,
};
use rusqlite::Connection;

/// A trusted keeper older than `KEEP`, so a merge of `KEEP` into it retires the
/// newer card. `GONE` is the first merge's retired duplicate.
const WIDE: &str = "wide-keeper";
const NEW_Y: &str = "A new card that reuses the retired id.\n";

async fn chain() -> Real {
    Real::new(&[
        (WIDE, &dup("", "dddddd")),
        (KEEP, &dup("", "aaaaaa")),
        (GONE, &dup("", "bbbbbb")),
        ("other-card", "Another topic entirely. ^c-cccccc\n"),
    ])
    .await
}
async fn merge_automatically(r: &Real, keeper: &str, retired: &str) {
    let opened = open_ops(r, keeper, &[retired]).await;
    let applied = auto(r, &opened).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
}
/// The second merge of a chain. The retired keeper already declares lineage, so
/// the proposal opens at tier 2 and a reviewer applies it. A reviewer reads the
/// alias line in the diff, but the id it names may belong to a newer card.
async fn merge_reviewed(r: &Real, keeper: &str, retired: &str) {
    let opened = submit(&r.h, ADMIN_TOKEN, vec![merge_op(r, keeper, retired).await]).await;
    assert_eq!(opened.body["tier"], 2, "{opened:?}");
    let applied = decide(&r.h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
}
/// The merge gate alone. A chained merge is a tier 2 review, which the full
/// real-scope hold refuses before it reaches the gate.
fn gate(r: &Real, body: &serde_json::Value) -> Option<&'static str> {
    let live = retrieval_eval::Live {
        applier: "admin",
        principal: ADMIN.into(),
        snapshot: None,
        indexed_seq: r.h.events(),
        embedder_id: None,
        weights: serde_json::json!({"lexical":1.0,"dense":1.0}),
    };
    merge_gate::hold(&Reading(&r.db), body, &live).unwrap()
}
fn aliases_of(text: &str) -> Vec<String> {
    proposal_metadata::list(text, "aliases")
        .unwrap()
        .into_iter()
        .collect()
}
fn head(r: &Real, id: &str) -> i64 {
    r.db.query_row(
        "SELECT revision FROM memory_cards WHERE scope='fleet' AND card_id=?1",
        [id],
        |row| row.get(0),
    )
    .unwrap()
}

/// K merges Y, a new card takes the id Y, then W merges K. W would inherit the
/// alias for Y and date it from this second merge, which postdates the new Y.
/// The merge leaves the alias out, so the new Y stays visible and unlinked
/// (#2726). The first merge applies through the automatic path. The second is
/// a tier 2 review, because the retired keeper already holds an alias.
#[tokio::test]
async fn a_chained_merge_does_not_give_a_reused_id_to_the_new_keeper() {
    let r = chain().await;
    merge_automatically(&r, KEEP, GONE).await;
    reuse(&r, GONE, NEW_Y).await;
    // The merged card is out of its cooldown by the time the chain merges it.
    r.h.set_clock(r.h.now() + 2 * 86_400_000);
    merge_reviewed(&r, WIDE, KEEP).await;
    let wide = card(&r.h, "fleet", WIDE).await;
    assert_eq!(
        aliases_of(wide["text"].as_str().unwrap()),
        ["fleet/dup-keeper"],
        "the inherited alias for the reused id is left out"
    );
    let snap = trusted_index(&r);
    assert!(!snap.in_lineage("fleet", GONE));
    assert_eq!(visible(&snap, "reuses retired"), [GONE]);
}

/// The same alias is kept when its card is not live, because a card that takes
/// the id later starts after the second merge too.
#[tokio::test]
async fn an_inherited_alias_to_an_id_nothing_holds_is_kept() {
    let r = chain().await;
    merge_automatically(&r, KEEP, GONE).await;
    r.h.set_clock(r.h.now() + 2 * 86_400_000);
    merge_reviewed(&r, WIDE, KEEP).await;
    let wide = card(&r.h, "fleet", WIDE).await;
    let aliases = aliases_of(wide["text"].as_str().unwrap());
    assert_eq!(aliases, ["fleet/dup-keeper", "fleet/dup-retired"]);
    // A card that takes the id after both merges is not linked to the keeper.
    reuse(&r, GONE, NEW_Y).await;
    let snap = trusted_index(&r);
    assert!(!snap.in_lineage("fleet", GONE));
}

/// An inherited alias whose card is older than the retired keeper's alias is a
/// genuine predecessor, so the merge carries it.
#[tokio::test]
async fn an_inherited_alias_to_an_older_live_card_is_kept() {
    let r = Real::new(&[
        ("old-name", "Old topic claim. ^c-111111\n"),
        (WIDE, &dup("", "dddddd")),
    ])
    .await;
    let keeper = format!(
        "---\naliases: [\"fleet/old-name\"]\n---\n{}",
        dup("", "aaaaaa")
    );
    assert_eq!(
        r.h.put(ADMIN_TOKEN, "fleet", KEEP, None, &keeper)
            .await
            .status,
        201
    );
    let (kept, retired) = (
        card(&r.h, "fleet", WIDE).await,
        card(&r.h, "fleet", KEEP).await,
    );
    let tx = Reading(&r.db);
    let stale = alias_dating::stale_inherited(
        &tx,
        "fleet",
        kept["text"].as_str().unwrap(),
        (
            KEEP,
            retired["revision"].as_i64().unwrap(),
            retired["text"].as_str().unwrap(),
        ),
    )
    .unwrap();
    assert!(stale.is_empty(), "{stale:?}");
}

/// Control: with no new card on the retired id, the chained merge keeps every
/// inherited alias and the gate passes it clean.
#[tokio::test]
async fn the_gate_passes_a_chained_merge_with_nothing_stale_to_drop() {
    let r = chain().await;
    merge_automatically(&r, KEEP, GONE).await;
    r.h.set_clock(r.h.now() + 2 * 86_400_000);
    let opened = submit(&r.h, ADMIN_TOKEN, vec![merge_op(&r, WIDE, KEEP).await]).await;
    assert_eq!(gate(&r, &r.body(&opened)), None);
}

/// With a new card on the retired id, a proposal that keeps the stale alias is
/// held, because the gate recomputes the keeper's text with the same function.
/// The text that drops it is held too, since the keeper no longer carries the
/// alias line's tokens, and that goes to a reviewer. Neither is admitted.
#[tokio::test]
async fn the_gate_never_admits_a_keeper_text_that_kept_a_stale_inherited_alias() {
    let r = chain().await;
    merge_automatically(&r, KEEP, GONE).await;
    reuse(&r, GONE, NEW_Y).await;
    r.h.set_clock(r.h.now() + 2 * 86_400_000);
    let opened = submit(&r.h, ADMIN_TOKEN, vec![merge_op(&r, WIDE, KEEP).await]).await;
    let body = r.body(&opened);
    assert_eq!(gate(&r, &body), Some("retrieval_eval_merge_tokens_lost"));
    let forged = edit(&body, WIDE, |text| {
        text.replace(
            "[\"fleet/dup-keeper\"]",
            "[\"fleet/dup-keeper\",\"fleet/dup-retired\"]",
        )
    });
    assert_ne!(forged, body);
    assert_eq!(
        gate(&r, &forged),
        Some("retrieval_eval_merge_keeper_changed")
    );
}

/// A keeper that is retired and then reused does not inherit the alias history
/// of the card it replaced. The new incarnation wrote the alias after the named
/// card existed, so the edge forms.
#[tokio::test]
async fn a_reused_keeper_id_dates_its_aliases_from_its_own_incarnation() {
    let r = Real::untrusted(&[(
        "keeper",
        "---\naliases: [\"zed\"]\n---\nKeeper claim. ^c-333333\n",
    )])
    .await;
    // The alias names an id nothing holds, then a card takes it.
    assert_eq!(
        r.h.put(ADMIN_TOKEN, "fleet", "zed", None, "Zed claim. ^c-444444\n")
            .await
            .status,
        201
    );
    retire(&r, "keeper").await;
    let text = "---\naliases: [\"zed\"]\n---\nKeeper claim again. ^c-555555\n";
    rewrite(&r, "keeper", text).await;
    let dates = alias_dating::alias_since(&r.db, "fleet", "keeper", head(&r, "keeper")).unwrap();
    assert_eq!(dates["zed"], head(&r, "keeper"), "dated from the revival");
    let snap = trusted_index(&r);
    assert!(snap.in_lineage("fleet", "keeper"));
    assert_eq!(visible(&snap, "claim"), ["keeper"]);
}

/// An alias that is dropped and written again dates from the second write, so
/// a card that took the id during the gap is older than the alias and links. A
/// card that took it after the second write does not.
#[tokio::test]
async fn an_alias_dropped_and_written_again_dates_from_the_second_write() {
    let r = Real::untrusted(&[(
        "keeper",
        "---\naliases: [\"zed\"]\n---\nKeeper claim. ^c-333333\n",
    )])
    .await;
    rewrite(&r, "keeper", "Keeper claim. ^c-333333\n").await;
    assert_eq!(
        r.h.put(ADMIN_TOKEN, "fleet", "zed", None, "Zed claim. ^c-444444\n")
            .await
            .status,
        201
    );
    let readded = "---\naliases: [\"zed\"]\n---\nKeeper claim. ^c-333333\n";
    rewrite(&r, "keeper", readded).await;
    let at = head(&r, "keeper");
    let dates = alias_dating::alias_since(&r.db, "fleet", "keeper", at).unwrap();
    assert_eq!(dates["zed"], at);
    let snap = trusted_index(&r);
    assert!(snap.in_lineage("fleet", "zed"), "written after zed existed");
}

/// The run is read back at most `RUN_CAP` revisions. A run that long still has
/// a date. One revision longer has none, and an unknown date forms no edge.
#[tokio::test]
async fn the_revision_cap_leaves_a_longer_run_undated_for_both_dates() {
    let r = Real::untrusted(&[(
        "busy",
        "---\naliases: [\"zed\"]\n---\nBusy claim 0. ^c-333333\n",
    )])
    .await;
    let first = head(&r, "busy");
    let write = |n: usize| format!("---\naliases: [\"zed\"]\n---\nBusy claim {n}. ^c-333333\n");
    for n in 1..RUN_CAP {
        rewrite(&r, "busy", &write(n)).await;
    }
    let at = head(&r, "busy");
    let revisions: i64 =
        r.db.query_row(
            "SELECT COUNT(*) FROM memory_card_revisions WHERE card_id='busy'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(revisions as usize, RUN_CAP);
    let dates = alias_dating::alias_since(&r.db, "fleet", "busy", at).unwrap();
    assert_eq!(dates["zed"], first, "a run of exactly the cap is read");
    assert_eq!(
        alias_dating::incarnation_start(&r.db, "fleet", "busy", at).unwrap(),
        Some(first)
    );
    rewrite(&r, "busy", &write(RUN_CAP)).await;
    let at = head(&r, "busy");
    assert!(
        alias_dating::alias_since(&r.db, "fleet", "busy", at)
            .unwrap()
            .is_empty(),
        "one revision past the cap is unknown"
    );
    assert_eq!(
        alias_dating::incarnation_start(&r.db, "fleet", "busy", at).unwrap(),
        None
    );
    // An unknown date forms no edge.
    assert_eq!(
        r.h.put(ADMIN_TOKEN, "fleet", "zed", None, "Zed claim. ^c-444444\n")
            .await
            .status,
        201
    );
    let snap = trusted_index(&r);
    assert!(!snap.in_lineage("fleet", "busy"));
}

/// A revision whose blob is gone ends the read with the alias undated, so it
/// forms no edge. It never links.
#[tokio::test]
async fn a_missing_blob_leaves_the_alias_undated() {
    let r = Real::untrusted(&[(
        "keeper",
        "---\naliases: [\"zed\"]\n---\nKeeper claim 0. ^c-333333\n",
    )])
    .await;
    let oldest: String =
        r.db.query_row(
            "SELECT content_hash FROM memory_card_revisions WHERE card_id='keeper'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    rewrite(
        &r,
        "keeper",
        "---\naliases: [\"zed\"]\n---\nKeeper claim 1. ^c-333333\n",
    )
    .await;
    assert_eq!(
        r.h.put(ADMIN_TOKEN, "fleet", "zed", None, "Zed claim. ^c-444444\n")
            .await
            .status,
        201
    );
    let at = head(&r, "keeper");
    let before = alias_dating::alias_since(&r.db, "fleet", "keeper", at).unwrap();
    assert_eq!(before.len(), 1, "control: the full run is dated");
    r.db.execute_batch("PRAGMA foreign_keys=OFF").unwrap();
    r.db.execute("DELETE FROM memory_blobs WHERE content_hash=?1", [&oldest])
        .unwrap();
    assert!(
        alias_dating::alias_since(&r.db, "fleet", "keeper", at)
            .unwrap()
            .is_empty()
    );
    let snap = trusted_index(&r);
    assert!(!snap.in_lineage("fleet", "keeper"));
}

/// An id that `supersedes` also names keeps that key's undated edge, so a
/// reused id is still linked. Dating covers the ids only `aliases` names.
#[tokio::test]
async fn an_id_that_aliases_and_supersedes_both_name_stays_undated() {
    let r = pair().await;
    let keeper = "---\naliases: [\"fleet/dup-retired\"]\nsupersedes: [\"dup-retired\"]\n---\nKeeper claim. ^c-777777\n";
    rewrite(&r, KEEP, keeper).await;
    retire(&r, GONE).await;
    reuse(&r, GONE, NEW_Y).await;
    let snap = trusted_index(&r);
    assert!(snap.in_lineage("fleet", KEEP));
    assert!(snap.in_lineage("fleet", GONE));
}

/// A card that is edited keeps the incarnation that dated its alias, and one
/// that is retired and written again starts a new one. The cached date follows
/// the document, so replaying either change is read afresh.
#[tokio::test]
async fn re_adding_a_live_target_refreshes_its_cached_date() {
    let r = Real::untrusted(&[
        ("old-name", "Old topic claim. ^c-111111\n"),
        (
            "new-name",
            "---\naliases: [\"fleet/old-name\"]\n---\nCurrent topic claim. ^c-222222\n",
        ),
    ])
    .await;
    r.db.execute("UPDATE memory_cards SET trust='reviewed'", [])
        .unwrap();
    let mut builder = index::load(&r.db).unwrap();
    assert!(
        builder.snapshot().in_lineage("fleet", "new-name"),
        "genuine"
    );
    // An edit of the target is a new document in the same incarnation.
    rewrite(&r, "old-name", "Old topic claim edited. ^c-111111\n").await;
    replay(&r, &mut builder, "old-name");
    assert!(builder.snapshot().in_lineage("fleet", "new-name"), "edited");
    // Retiring it and writing it again starts an incarnation after the alias.
    retire(&r, "old-name").await;
    rewrite(&r, "old-name", "Old topic claim reborn. ^c-111111\n").await;
    replay(&r, &mut builder, "old-name");
    assert!(
        !builder.snapshot().in_lineage("fleet", "new-name"),
        "reborn"
    );
    assert_eq!(builder.dated_keepers(), 1);
}
/// Replays the live head of `id` onto the builder, as the hub does after a write.
fn replay(r: &Real, builder: &mut index::Builder, id: &str) {
    r.db.execute("UPDATE memory_cards SET trust='reviewed'", [])
        .unwrap();
    let (revision, content_hash, trust, bytes): (i64, String, String, Vec<u8>) =
        r.db.query_row(
            "SELECT c.revision,c.content_hash,c.trust,b.bytes FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.scope='fleet' AND c.card_id=?1",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap();
    let card = index::Card {
        scope: "fleet".into(),
        card_id: id.into(),
        revision,
        content_hash,
        trust,
        text: String::from_utf8(bytes).unwrap(),
        automated: false,
    };
    builder
        .apply_dated(vec![Change::Live(card)], revision, &r.db)
        .unwrap();
}

/// The cheap path: a keeper none of whose alias ids is live is never read.
#[tokio::test]
async fn a_keeper_whose_aliases_name_no_live_card_is_not_dated() {
    let r = Real::untrusted(&[(
        "keeper",
        "---\naliases: [\"nobody\"]\n---\nKeeper claim. ^c-333333\n",
    )])
    .await;
    r.db.execute("UPDATE memory_cards SET trust='reviewed'", [])
        .unwrap();
    let builder = index::load(&r.db).unwrap();
    assert_eq!(builder.dated_keepers(), 0);
    // The date is read once a card takes the id.
    assert_eq!(
        r.h.put(
            ADMIN_TOKEN,
            "fleet",
            "nobody",
            None,
            "Nobody claim. ^c-444444\n"
        )
        .await
        .status,
        201
    );
    r.db.execute("UPDATE memory_cards SET trust='reviewed'", [])
        .unwrap();
    assert_eq!(index::load(&r.db).unwrap().dated_keepers(), 1);
}

/// Both history reads walk the primary key backwards, so SQLite sorts nothing.
#[test]
fn the_history_reads_seek_the_primary_key() {
    let db = Connection::open_in_memory().unwrap();
    db.execute_batch(&format!(
        "CREATE TABLE memory_blobs(content_hash TEXT PRIMARY KEY, bytes BLOB);{}",
        schema_of_revisions()
    ))
    .unwrap();
    for sql in [alias_dating::STATES, alias_dating::RUN] {
        let plan: Vec<String> = db
            .prepare(&format!("EXPLAIN QUERY PLAN {sql}"))
            .unwrap()
            .query_map(rusqlite::params!["s", "c", 9, 257], |r| {
                r.get::<_, String>(3)
            })
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap();
        let plan = plan.join(" | ");
        assert!(!plan.contains("TEMP B-TREE"), "{plan}");
        assert!(plan.contains("PRIMARY KEY"), "{plan}");
    }
}
fn schema_of_revisions() -> &'static str {
    "CREATE TABLE memory_card_revisions(scope TEXT NOT NULL, card_id TEXT NOT NULL, revision INTEGER NOT NULL, content_hash TEXT NOT NULL, trust TEXT NOT NULL, state TEXT NOT NULL, writer TEXT NOT NULL, seq INTEGER NOT NULL, proposal_id TEXT, scan_state TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(scope, card_id, revision)) WITHOUT ROWID;"
}

/// A reader of the store for the helpers that take a writer transaction.
struct Reading<'a>(&'a Connection);
impl crate::writer::ReadTx for Reading<'_> {
    fn query(
        &self,
        sql: &str,
        params: &[rusqlite::types::Value],
    ) -> rusqlite::Result<Vec<Vec<rusqlite::types::Value>>> {
        let mut statement = self.0.prepare(sql)?;
        let width = statement.column_count();
        statement
            .query_map(rusqlite::params_from_iter(params), |row| {
                (0..width).map(|i| row.get(i)).collect()
            })?
            .collect()
    }
}
