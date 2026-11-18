//! Every lineage key is dated, on the real scope `fleet` (#2726). Each case
//! reads its dates from a real revision history. A name forms an edge only when
//! the card that holds the id now began before the owner wrote the name.
use super::{
    alias_dating_tests::{WIDE, chain, merge_automatically, replay},
    index::{self, Change},
    merge_drift_tests::*,
    proposal_tests::*,
    ranking_gate_tests::Real,
    tests::*,
    *,
};
use std::collections::BTreeSet;

const NEW_Y: &str = "A new card that reuses the retired id.\n";
const NEW_CLAIM: &str = "Shared gate claim from a new card.\n";

/// The keeper text that names `GONE` under `front`, with `GONE`'s exact claim.
fn naming(front: &str) -> String {
    dup(&format!("---\n{front}\n---\n"), "777777")
}
/// `KEEP` names `GONE` under `front`, then `GONE` is retired and its id taken
/// by a new card, as an attacker with a trusted write would do.
async fn named_then_reused(front: &str) -> Real {
    let r = pair().await;
    rewrite(&r, KEEP, &naming(front)).await;
    retire(&r, GONE).await;
    reuse(&r, GONE, NEW_CLAIM).await;
    r
}
async fn names_a_live_card(front: &str) -> Real {
    let r = pair().await;
    rewrite(&r, KEEP, &naming(front)).await;
    r
}

/// The exploit the review found: an id in both `aliases` and `supersedes` kept
/// the undated edge of the second key, so a card that reused the id collapsed
/// under the keeper.
#[tokio::test]
async fn an_id_in_aliases_and_supersedes_does_not_link_a_card_that_reused_it() {
    let both = "aliases: [\"fleet/dup-retired\"]\nsupersedes: [\"dup-retired\"]";
    let r = named_then_reused(both).await;
    let snap = trusted_index(&r);
    assert!(!snap.in_lineage("fleet", KEEP));
    assert!(!snap.in_lineage("fleet", GONE));
    assert_eq!(visible(&snap, "shared gate claim"), [KEEP, GONE]);
}

/// Each predecessor key forms the edge while the card it named is the one
/// holding the id, and none forms it once a new card has taken the id.
#[tokio::test]
async fn each_predecessor_key_forms_no_edge_to_a_reused_id() {
    for front in [
        "supersedes: [\"dup-retired\"]",
        "merged_from: [\"dup-retired\"]",
        "aliases: [\"dup-retired\"]",
    ] {
        let live = names_a_live_card(front).await;
        let snap = trusted_index(&live);
        assert!(snap.in_lineage("fleet", GONE), "{front}: control");
        assert_eq!(visible(&snap, "shared gate claim"), [KEEP], "{front}");

        let reused = named_then_reused(front).await;
        let snap = trusted_index(&reused);
        assert!(!snap.in_lineage("fleet", KEEP), "{front}");
        assert!(!snap.in_lineage("fleet", GONE), "{front}");
        assert_eq!(visible(&snap, "shared gate claim"), [KEEP, GONE], "{front}");
    }
}

/// A genuine succession is unchanged: the keeper names an older live card and
/// the older card collapses under it.
#[tokio::test]
async fn a_genuine_supersedes_of_an_older_live_card_still_collapses() {
    let r = names_a_live_card("supersedes: [\"dup-retired\"]").await;
    let snap = trusted_index(&r);
    assert!(snap.in_lineage("fleet", KEEP));
    assert_eq!(visible(&snap, "shared gate claim"), [KEEP]);
    // The same holds when the index is replayed onto a built one.
    let mut builder = index::load(&r.db).unwrap();
    replay(&r, &mut builder, KEEP);
    assert_eq!(visible(&builder.snapshot(), "shared gate claim"), [KEEP]);
}

/// A card the owner names when it is written is older than the name, so an
/// edit of the owner keeps the date of the unbroken run, not the edit's.
#[tokio::test]
async fn an_edit_of_the_owner_keeps_the_names_original_date() {
    let r = named_then_reused("supersedes: [\"dup-retired\"]").await;
    let edited = dup("---\nsupersedes: [\"dup-retired\"]\n---\n", "888888");
    rewrite(&r, KEEP, &edited).await;
    let snap = trusted_index(&r);
    assert!(!snap.in_lineage("fleet", GONE), "the run is unbroken");
}

/// An id named under several keys forms an edge when any one key passes. The
/// `aliases` name predates the reuse, so it fails, and a `supersedes` the owner
/// writes after the reuse is a fresh claim about the card that holds the id.
#[tokio::test]
async fn a_fresh_key_forms_the_edge_that_a_stale_key_cannot() {
    let r = named_then_reused("aliases: [\"dup-retired\"]").await;
    assert!(!trusted_index(&r).in_lineage("fleet", GONE), "stale alone");
    let both = naming("aliases: [\"dup-retired\"]\nsupersedes: [\"dup-retired\"]");
    rewrite(&r, KEEP, &both).await;
    let snap = trusted_index(&r);
    assert!(snap.in_lineage("fleet", GONE), "a fresh supersedes links");
}

/// `superseded_by` points from the owner to the successor. An older card whose
/// successor was retired must not collapse under the card that took the id.
#[tokio::test]
async fn superseded_by_forms_no_edge_to_a_reused_id() {
    let by = "superseded_by: dup-retired";
    let live = names_a_live_card(by).await;
    let snap = trusted_index(&live);
    assert_eq!(visible(&snap, "shared gate claim"), [GONE], "control");

    let r = named_then_reused(by).await;
    let snap = trusted_index(&r);
    assert!(!snap.in_lineage("fleet", KEEP));
    assert!(!snap.in_lineage("fleet", GONE));
    assert_eq!(visible(&snap, "shared gate claim"), [KEEP, GONE]);
}

/// A card may name its successor before it is written. The first card under
/// that id is the successor the owner meant, so it links.
#[tokio::test]
async fn superseded_by_may_name_a_card_written_later() {
    let r = Real::untrusted(&[(KEEP, &naming("superseded_by: later-card"))]).await;
    assert_eq!(
        r.h.put(ADMIN_TOKEN, "fleet", "later-card", None, NEW_CLAIM)
            .await
            .status,
        201
    );
    let snap = trusted_index(&r);
    assert_eq!(visible(&snap, "shared gate claim"), ["later-card"]);
}

/// A merge writes `aliases` and `evidence` and no other lineage key, so the
/// merge-time filter for inherited names covers everything a merge copies.
#[test]
fn a_merge_copies_no_lineage_key_but_aliases() {
    let keeper = "Shared gate claim. ^c-aaaaaa\n";
    let retired = "---\naliases: [\"old-name\"]\nsupersedes: [\"older\"]\nmerged_from: [\"oldest\"]\nsuperseded_by: newer\n---\nShared gate claim. ^c-bbbbbb\n";
    let op = json!({"type":"merge_exact_duplicates"});
    let merged =
        proposal_metadata::apply(keeper, &op, Some((retired, "fleet/gone")), &BTreeSet::new())
            .unwrap();
    let links = ranking::Links::parse(&merged, "fleet");
    let keys: BTreeSet<_> = links.named.iter().map(|(key, _)| *key).collect();
    assert_eq!(keys, BTreeSet::from(["aliases"]));
    assert_eq!(links.successor, None);
}

/// A chain merge whose inherited alias id was unheld at submit time. Another
/// writer reuses the id before a reviewer accepts. The stored text would date
/// the alias from the merge, so the apply refuses it as stale.
#[tokio::test]
async fn a_reviewed_chained_merge_is_stale_when_the_inherited_id_was_reused() {
    let r = chain().await;
    merge_automatically(&r, KEEP, GONE).await;
    r.h.set_clock(r.h.now() + 2 * 86_400_000);
    // The id is unheld when the chain merge is submitted.
    let opened = submit(&r.h, ADMIN_TOKEN, vec![merge_op(&r, WIDE, KEEP).await]).await;
    assert_eq!(opened.body["tier"], 2, "{opened:?}");
    let stored = r.body(&opened);
    let kept = |text: &str| proposal_metadata::list(text, "aliases").unwrap();
    let wide = stored["cards"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["card_id"] == WIDE)
        .unwrap();
    assert!(kept(wide["after_text"].as_str().unwrap()).contains("fleet/dup-retired"));
    // Another writer takes the id, then a reviewer accepts the stored text.
    reuse(&r, GONE, NEW_Y).await;
    let applied = decide(&r.h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    let state = &applied.body["state"];
    assert_eq!(state, "stale", "{applied:?}");
    assert_eq!(applied.body["reason"], "inherited_alias_reused");
    // Nothing was written, and the new card stays visible and unlinked.
    let head = card(&r.h, "fleet", WIDE).await;
    assert!(!kept(head["text"].as_str().unwrap()).contains("fleet/dup-keeper"));
    let snap = trusted_index(&r);
    assert!(!snap.in_lineage("fleet", GONE));
    assert_eq!(visible(&snap, "reuses"), [GONE]);
}

/// Control: with nothing reused the same reviewed chain merge applies.
#[tokio::test]
async fn a_reviewed_chained_merge_still_applies_when_nothing_was_reused() {
    let r = chain().await;
    merge_automatically(&r, KEEP, GONE).await;
    r.h.set_clock(r.h.now() + 2 * 86_400_000);
    let opened = submit(&r.h, ADMIN_TOKEN, vec![merge_op(&r, WIDE, KEEP).await]).await;
    let applied = decide(&r.h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
}

/// Owners that name one id, which nothing holds yet, so the store was never
/// asked about their history.
async fn many_owners(count: usize, front: &str) -> (Real, index::Builder) {
    let owners: Vec<(String, String)> = (0..count)
        .map(|n| {
            (
                format!("owner-{n:03}"),
                format!("---\n{front}\n---\nOwner claim {n}. ^c-{n:06x}\n"),
            )
        })
        .collect();
    let cards: Vec<(&str, &str)> = owners
        .iter()
        .map(|(id, text)| (id.as_str(), text.as_str()))
        .collect();
    let r = Real::untrusted(&cards).await;
    r.db.execute("UPDATE memory_cards SET trust='reviewed'", [])
        .unwrap();
    let builder = index::load(&r.db).unwrap();
    (r, builder)
}
async fn live_card(r: &Real, id: &str) -> Change {
    r.db.execute("UPDATE memory_cards SET trust='reviewed'", [])
        .unwrap();
    let (revision, content_hash, bytes): (i64, String, Vec<u8>) =
        r.db.query_row(
            "SELECT c.revision,c.content_hash,b.bytes FROM memory_cards c JOIN memory_blobs b ON b.content_hash=c.content_hash WHERE c.card_id=?1",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    Change::Live(index::Card {
        scope: "fleet".into(),
        card_id: id.into(),
        revision,
        content_hash,
        trust: "reviewed".into(),
        text: String::from_utf8(bytes).unwrap(),
        automated: false,
    })
}

/// One card becoming live under an id many owners name must not read every
/// owner's history in one publication. The reverse index finds exactly those
/// owners, a publication reads at most its budget, and the rest finish on the
/// next publications. An owner not yet dated forms no edge.
#[tokio::test]
async fn a_target_becoming_live_dates_its_owners_within_the_budget() {
    const OWNERS: usize = 40;
    const BUDGET: usize = 6;
    let (r, mut builder) = many_owners(OWNERS, "aliases: [\"zed\"]").await;
    assert_eq!(builder.dated_keepers(), 0, "nothing holds the id yet");
    assert!(!builder.dating_pending());
    builder.budget = BUDGET;
    assert_eq!(
        r.h.put(ADMIN_TOKEN, "fleet", "zed", None, "Zed claim. ^c-444444\n")
            .await
            .status,
        201
    );
    let zed = live_card(&r, "zed").await;
    let seq = r.h.events();
    builder.apply_dated(vec![zed], seq, &r.db).unwrap();
    assert!(
        builder.dated_keepers() <= BUDGET,
        "one publication is bounded"
    );
    assert!(builder.dating_pending(), "the rest are left for later");
    let mut rounds = 1;
    let mut dated = builder.dated_keepers();
    while builder.dating_pending() {
        builder.apply_dated(Vec::new(), seq, &r.db).unwrap();
        assert!(builder.dated_keepers() - dated <= BUDGET, "round {rounds}");
        dated = builder.dated_keepers();
        rounds += 1;
        assert!(rounds <= OWNERS, "dating makes progress each round");
    }
    assert_eq!(builder.dated_keepers(), OWNERS);
    assert!(rounds > 1, "the budget split the work");
    // Zed is newer than every alias, so no owner links it.
    assert!(!builder.snapshot().in_lineage("fleet", "zed"));
}

/// An edit of a target that many owners name reads only the target's own
/// history. Each owner's dates are kept, so the owners cost no read.
#[tokio::test]
async fn editing_a_heavily_named_target_reads_no_owner_history() {
    const OWNERS: usize = 40;
    let r = Real::untrusted(&[("old-name", "Old topic claim. ^c-111111\n")]).await;
    // The owners are written after the target, so their aliases are genuine.
    for n in 0..OWNERS {
        let text =
            format!("---\naliases: [\"fleet/old-name\"]\n---\nOwner claim {n}. ^c-{n:06x}\n");
        let id = format!("owner-{n:03}");
        assert_eq!(
            r.h.put(ADMIN_TOKEN, "fleet", &id, None, &text).await.status,
            201
        );
    }
    r.db.execute("UPDATE memory_cards SET trust='reviewed'", [])
        .unwrap();
    let mut builder = index::load(&r.db).unwrap();
    assert_eq!(builder.dated_keepers(), OWNERS);
    assert!(builder.snapshot().in_lineage("fleet", "old-name"));
    // One read of budget is enough: the edited target's own incarnation.
    builder.budget = 1;
    rewrite(&r, "old-name", "Old topic claim edited. ^c-111111\n").await;
    replay(&r, &mut builder, "old-name");
    assert!(
        !builder.dating_pending(),
        "a budget of one finished the work"
    );
    assert!(
        builder.snapshot().in_lineage("fleet", "old-name"),
        "still linked"
    );
}
