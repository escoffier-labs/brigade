//! Exact-duplicate merges auto-apply on a real scope under bounded drift (#2591).
//! Every hub here uses the real scope `fleet`, never the fixture scope.
use super::{
    index::{self, Builder, Card},
    proposal_blocks,
    proposal_tests::*,
    ranking_gate_tests::Real,
    tests::*,
    text, *,
};
use serde_json::{Value as Json, json};
use std::collections::BTreeSet;

const KEEP: &str = "dup-keeper";
const GONE: &str = "dup-retired";
const MIXED: &str = "retrieval_eval_merge_mixed_ops";
const SHAPE: &str = "retrieval_eval_merge_shape";
const TRUST: &str = "retrieval_eval_merge_trust_mismatch";
const AUTOMATED: &str = "retrieval_eval_merge_automated_mismatch";
const INBOUND: &str = "retrieval_eval_merge_inbound_link";
const TITLE: &str = "retrieval_eval_merge_title_uncovered";
const TOKENS: &str = "retrieval_eval_merge_tokens_lost";
const KEEPER: &str = "retrieval_eval_merge_keeper_changed";
const RETIRES_OLDER: &str = "retrieval_eval_merge_retires_older";
const LITERAL: &str = "retrieval_eval_merge_literal_lost";
const DRIFTED: &str = "retrieval_eval_target_drifted";
const LINEAGE: &str = "retrieval_eval_lineage_exposed";
const WRITER: &str = "retrieval_eval_writer_changed";
const PROJECTION: &str = "retrieval_eval_projection_changed";

fn dup(front: &str, anchor: &str) -> String {
    format!("{front}Shared gate claim. ^c-{anchor}\n")
}
const ALPHA: &str = "---\ntitle: Alpha\n---\n";
/// Two untitled exact duplicates and one unrelated card.
async fn pair() -> Real {
    let (keeper, retired) = (dup("", "aaaaaa"), dup("", "bbbbbb"));
    let other = "Another topic entirely. ^c-cccccc\n";
    Real::untrusted(&[(KEEP, &keeper), (GONE, &retired), ("other-card", other)]).await
}
async fn open(r: &Real) -> Reply {
    open_ops(r, KEEP, &[GONE]).await
}
/// One proposal that merges each of `retired` into `keeper`.
async fn open_ops(r: &Real, keeper: &str, retired: &[&str]) -> Reply {
    let k = card(&r.h, "fleet", keeper).await;
    let pin = proposal_blocks::pins(k["text"].as_str().unwrap()).remove(0);
    let mut ops = Vec::new();
    for id in retired {
        let o = card(&r.h, "fleet", id).await;
        ops.push(json!({"type":"merge_exact_duplicates","scope":"fleet","card_id":keeper,
            "base_revision":k["revision"],"block_id":pin["block_id"],"expected_block_hash":pin["hash"],
            "other_card":{"scope":"fleet","card_id":id,"base_revision":o["revision"]}}));
    }
    let opened = submit(&r.h, ADMIN_TOKEN, ops).await;
    assert_eq!(
        (opened.status, &opened.body["tier"]),
        (201, &json!(0)),
        "{opened:?}"
    );
    opened
}
/// The proposal with one card's after text rewritten.
fn edit(body: &Json, id: &str, f: impl Fn(&str) -> String) -> Json {
    let mut out = body.clone();
    let card = out["cards"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|c| c["card_id"] == id);
    let card = card.unwrap();
    let text = f(card["after_text"].as_str().unwrap());
    // A consistent forgery: the hash names the forged text.
    card["after_hash"] = json!(ids::sha256_hex(text.as_bytes()));
    card["after_text"] = json!(text);
    out
}
/// The proposal as if `id` had always read `text`, before and after.
fn retext(body: &Json, id: &str, text: &str) -> Json {
    let mut out = body.clone();
    let card = out["cards"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|c| c["card_id"] == id)
        .unwrap();
    card["before_text"] = json!(text);
    card["after_text"] = json!(text);
    out
}
fn held(r: &Real, body: &Json) -> Option<&'static str> {
    r.hold(body, "admin", None)
}
fn sql(r: &Real, statement: &str, id: &str) {
    r.db.execute(statement, [id]).unwrap();
}
async fn link(r: &Real, text: &str) {
    assert_eq!(
        r.h.put(ADMIN_TOKEN, "fleet", "linker", None, text)
            .await
            .status,
        201
    );
}

/// The retrieval gate admits a clean merge. Three older gates still hold every
/// merge, so none can apply automatically yet: the one-card blast cap counts the
/// keeper and the retired card, evidence floors want attested proof a merge
/// never carries, and `card_trust` refuses the only class that can be Tier 0.
/// Pinned here so lifting one of them is a deliberate change.
#[tokio::test]
async fn a_clean_merge_passes_the_retrieval_gate_and_meets_the_older_holds() {
    let r = pair().await;
    let opened = open(&r).await;
    assert_eq!(held(&r, &r.body(&opened)), None);
    let report = attestation_tests::report(&r.h, &opened.body["proposal_id"]).await;
    let state = |gate: &str| {
        let checks = report.body["checks"].as_array().unwrap();
        let c = checks.iter().find(|c| c["gate"] == gate).unwrap();
        (c["state"].clone(), c["code"].clone())
    };
    let (ok, no) = (json!("satisfied"), json!("held"));
    assert_eq!(
        state("retrieval_eval"),
        (ok, json!("retrieval_eval_satisfied"))
    );
    assert_eq!(state("delta"), (no.clone(), json!("blast_cap_exceeded")));
    assert_eq!(
        state("card_trust"),
        (no.clone(), json!("card_trust_denied"))
    );
    assert_eq!(state("evidence_floor").0, no);
    let request = attestation_tests::auto_request(&r.h, &opened.body["proposal_id"]).await;
    let automatic = r.h.call(ADMIN_TOKEN, request).await;
    assert_eq!(automatic.body["state"], "pending", "{automatic:?}");
    assert_eq!(card(&r.h, "fleet", GONE).await["state"], "live");
}

/// A reviewed accept stands in for an automatic apply, then the stored proposal
/// is marked automatic so the compensation path sees the merge it would see.
#[tokio::test]
async fn the_compensation_inverse_of_a_merge_restores_both_cards_exactly() {
    let r = pair().await;
    let before = [
        card(&r.h, "fleet", KEEP).await,
        card(&r.h, "fleet", GONE).await,
    ];
    let id = open(&r).await.body["proposal_id"].clone();
    assert_eq!(
        decide(&r.h, ADMIN_TOKEN, &id).await.body["state"],
        "applied"
    );
    assert_eq!(card(&r.h, "fleet", GONE).await["state"], "retired");
    let mark =
        "UPDATE memory_proposals SET body=json_set(body,'$.decision_mode','automatic') WHERE ?1=?1";
    sql(&r, mark, "");
    r.h.set_clock(r.h.now() + 1);
    let undo = r.h.call(ADMIN_TOKEN, json!({"action":"rollback","proposal_id":id,"base_epoch":r.h.epoch().await,"client_event_id":r.h.id()})).await;
    assert_eq!(
        (&undo.body["state"], &undo.body["tier"]),
        (&json!("applied"), &json!(0)),
        "{undo:?}"
    );
    for (id, was) in [KEEP, GONE].into_iter().zip(&before) {
        let now = card(&r.h, "fleet", id).await;
        assert_eq!(
            (&now["text"], &now["state"]),
            (&was["text"], &json!("live"))
        );
    }
}

#[tokio::test]
async fn each_unsafe_merge_is_held_with_its_own_reason() {
    let r = Real::untrusted(&[(KEEP, &dup(ALPHA, "aaaaaa")), (GONE, &dup(ALPHA, "bbbbbb"))]).await;
    let body = r.body(&open(&r).await);
    assert_eq!(held(&r, &body), None);
    // A hybrid index also reads content hashes, so it is never admitted.
    assert_eq!(r.hold(&body, "admin", Some("embedder")), Some(PROJECTION));
    let mut mixed = body.clone();
    mixed["ops"]
        .as_array_mut()
        .unwrap()
        .push(json!({"type":"append_evidence","scope":"fleet","card_id":KEEP}));
    assert_eq!(held(&r, &mixed), Some(MIXED));
    let mut lone = body.clone();
    lone["ops"][0].as_object_mut().unwrap().remove("other_card");
    assert_eq!(held(&r, &lone), Some(SHAPE));
    let mut stale = body.clone();
    stale["cards"][0]["base_revision"] = json!(999);
    assert_eq!(held(&r, &stale), Some(DRIFTED));
    // The keeper's after text must be exactly what the merge makes.
    for to in [
        "title: Alpha beta",
        "title: Alpha\nsummary: new words",
        "title: Alpha\ntags: [new]",
    ] {
        let forged = edit(&body, KEEP, |t| t.replacen("title: Alpha", to, 1));
        assert_eq!(held(&r, &forged), Some(KEEPER), "{to}");
    }
    // Tier 0 needs identical front matter, so only a drifted or forged after
    // text can lose a token. The gate recomputes from the live cards anyway.
    assert_eq!(
        held(&r, &edit(&body, KEEP, |t| t.replace("gate ", ""))),
        Some(TOKENS)
    );
    let retitled = edit(&body, KEEP, |t| t.replace("title: Alpha", "title: Gamma"));
    assert_eq!(held(&r, &retitled), Some(TITLE));
}

#[tokio::test]
async fn trust_and_automation_must_match_across_the_pair() {
    let r = pair().await;
    let body = r.body(&open(&r).await);
    sql(
        &r,
        "UPDATE memory_cards SET trust='reviewed' WHERE card_id=?1",
        GONE,
    );
    assert_eq!(held(&r, &body), Some(TRUST));
    sql(
        &r,
        "UPDATE memory_cards SET trust='agent-unreviewed' WHERE card_id=?1",
        GONE,
    );
    assert_eq!(held(&r, &body), None);
    // Only the retired card came from a worker put.
    let r = Real::untrusted(&[(KEEP, &dup("", "aaaaaa"))]).await;
    let put = r.h.call(ADMIN_TOKEN, json!({"action":"put","scope":"fleet","card_id":GONE,"text":dup("", "bbbbbb"),"origin":"worker","client_event_id":r.h.id()})).await;
    assert_eq!(put.status, 201, "{put:?}");
    let body = r.body(&open(&r).await);
    assert_eq!(held(&r, &body), Some(AUTOMATED));
    // A principal-class applier makes both automated, so the keeper flips and
    // the hold says so instead of naming a changed keeper.
    assert_eq!(r.hold(&body, "principal", None), Some(AUTOMATED));
}

#[tokio::test]
async fn untitled_duplicates_are_covered_by_the_alias_but_not_by_a_forged_merge() {
    let r = pair().await;
    let body = r.body(&open(&r).await);
    // The default title is the card id, which the alias line carries.
    assert_eq!(held(&r, &body), None);
    let bare = |t: &str| {
        t.lines()
            .filter(|l| !l.starts_with("aliases:"))
            .collect::<Vec<_>>()
            .join("\n")
            + "\n"
    };
    assert_eq!(held(&r, &edit(&body, KEEP, bare)), Some(TITLE));
}

/// A card made of `text` is the only other live card, so it alone decides.
async fn naming_card_holds(text: &str) -> Option<&'static str> {
    let r = pair().await;
    let body = r.body(&open(&r).await);
    link(&r, text).await;
    held(&r, &body)
}

#[tokio::test]
async fn a_card_naming_the_retired_id_holds_the_merge() {
    let forms = [
        ("wiki link", "See [[dup-retired]] for more.\n"),
        ("aliased wiki link", "See [[dup-retired|the old one]].\n"),
        ("markdown link", "See [old](cards/dup-retired.md).\n"),
        (
            "superseded_by",
            "---\nsuperseded_by: dup-retired\n---\nBody.\n",
        ),
        (
            "predecessor",
            "---\nsupersedes: [dup-retired]\n---\nBody.\n",
        ),
        (
            "alias",
            "---\naliases: [\"fleet/dup-retired\"]\n---\nBody.\n",
        ),
        ("other key", "---\nrelated: [dup-retired]\n---\nBody.\n"),
        // Values go through `literal_eval`, so an escape spells the id with
        // no byte of it in the stored card.
        (
            "escaped predecessor",
            "---\nsupersedes: [\"dup-\\x72etired\"]\n---\nBody.\n",
        ),
    ];
    for (name, text) in forms {
        assert_eq!(naming_card_holds(text).await, Some(INBOUND), "{name}");
    }
    // A link to the keeper leaves the retired id alone.
    assert_eq!(naming_card_holds("See [[dup-keeper]].\n").await, None);
}

/// Spellings a reader resolves to the retired card but a strict parse of the
/// target would skip.
#[tokio::test]
async fn a_link_spelled_another_way_still_holds_the_merge() {
    let forms = [
        ("dot slash", "See [old](./cards/dup-retired.md).\n"),
        ("dot dot slash", "See [old](../cards/dup-retired.md).\n"),
        ("root slash", "See [old](/cards/dup-retired.md).\n"),
        (
            "memory path",
            "See [old](../memory/cards/dup-retired.md).\n",
        ),
        ("angle brackets", "See [old](<cards/dup-retired.md>).\n"),
        (
            "markdown fragment",
            "See [old](cards/dup-retired.md#claim).\n",
        ),
        ("wiki fragment", "See [[dup-retired#claim]].\n"),
        ("wiki path", "See [[./cards/dup-retired.md]].\n"),
        (
            "reference definition",
            "See [old].\n\n[old]: cards/dup-retired.md\n",
        ),
        (
            "reference with angle brackets and title",
            "See [old].\n\n  [old]: <./cards/dup-retired.md> \"Old\"\n",
        ),
        (
            "front matter fragment",
            "---\nrelated: fleet/dup-retired#claim\n---\nBody.\n",
        ),
        (
            "front matter path",
            "---\nrelated: ./cards/dup-retired.md\n---\nBody.\n",
        ),
        (
            "front matter angle brackets",
            "---\nrelated: <cards/dup-retired.md#claim>\n---\nBody.\n",
        ),
    ];
    for (name, text) in forms {
        assert_eq!(naming_card_holds(text).await, Some(INBOUND), "{name}");
    }
}

/// Keys outside the lineage list carry ids too, and a literal can spell one
/// with escapes or by concatenation, so no stored byte names it.
#[tokio::test]
async fn an_id_spelled_with_escapes_in_any_key_holds_the_merge() {
    let junk = vec!["\"x\""; 256].join(",");
    let forms = [
        (
            "hex escape",
            "---\nrelated: [\"dup-\\x72etired\"]\n---\nBody.\n".to_string(),
        ),
        (
            "unicode escape",
            "---\nrelated: [\"dup-\\u0072etired\"]\n---\nBody.\n".to_string(),
        ),
        (
            "octal escape",
            "---\nrelated: [\"dup-\\162etired\"]\n---\nBody.\n".to_string(),
        ),
        (
            "concatenation",
            "---\nrelated: [\"dup-\" \"retired\"]\n---\nBody.\n".to_string(),
        ),
        (
            "concatenated path",
            "---\nsee_also: [\"cards/dup-\" \"retired.md\"]\n---\nBody.\n".to_string(),
        ),
        // `Links::parse` reads the first 256 items. A dangling link past them
        // still dangles.
        (
            "past the lineage cap",
            format!("---\nrelated: [{junk},\"dup-retired\"]\n---\nBody.\n"),
        ),
    ];
    for (name, text) in forms {
        assert_eq!(naming_card_holds(&text).await, Some(INBOUND), "{name}");
    }
}

#[tokio::test]
async fn a_link_added_between_preflight_and_apply_is_caught_at_apply() {
    let mut r = pair().await;
    // The linker exists but names nothing, so the first read finds no link.
    link(&r, "Nothing to see.\n").await;
    let opened = open(&r).await;
    assert_eq!(held(&r, &r.body(&opened)), None);
    // The link lands inside the writer's own seam, after the preparation read
    // and before the writer lock, so only the recheck under the lock can see it.
    let path = r.h.memory();
    r.h.store.take();
    r.h.open(
        clean(),
        Some(std::sync::Arc::new(move |step| {
            if step == writer::Step::AutoPrepared {
                rusqlite::Connection::open(&path)
                    .unwrap()
                    .execute(
                        "UPDATE memory_blobs SET bytes=CAST('See [[dup-retired]].' AS BLOB) WHERE content_hash=(SELECT content_hash FROM memory_cards WHERE card_id='linker')",
                        [],
                    )
                    .unwrap();
            }
        })),
    );
    let request = attestation_tests::auto_request(&r.h, &opened.body["proposal_id"]).await;
    let reply = r.h.call(ADMIN_TOKEN, request).await;
    // The older gates hold every merge today, so the apply never reaches the
    // recheck under the lock and the reply cannot name the link. What the seam
    // can pin is that the merge does not apply and that the gate reads the link
    // from the store, the same read the recheck makes once a merge can reach it.
    assert_ne!(reply.body["state"], "applied", "{reply:?}");
    assert_eq!(card(&r.h, "fleet", GONE).await["state"], "live");
    assert_eq!(held(&r, &r.body(&opened)), Some(INBOUND));
}

#[tokio::test]
async fn a_lineage_exposed_pair_stays_with_the_lineage_review_rule() {
    let older = "---\nsupersedes: [older]\n---\n";
    for (name, front) in [
        ("keeper gains a predecessor", ""),
        ("both declare lineage", older),
    ] {
        let r =
            Real::untrusted(&[(KEEP, &dup(front, "aaaaaa")), (GONE, &dup(front, "bbbbbb"))]).await;
        let body = r.body(&open(&r).await);
        assert_eq!(held(&r, &body), None, "{name}");
        // Both cards became trusted after tiering, so lineage recency applies.
        r.db.execute("UPDATE memory_cards SET trust='reviewed'", [])
            .unwrap();
        assert_eq!(held(&r, &body), Some(LINEAGE), "{name}");
    }
}

/// A published index whose trusted lineage graph holds `linked` as an edge end.
fn published(linked: &str) -> std::sync::Arc<index::Snapshot> {
    let card = |id: &str, text: &str, revision| Card {
        scope: "fleet".into(),
        card_id: id.into(),
        revision,
        content_hash: format!("hash-{id}"),
        trust: "imported".into(),
        text: text.into(),
        automated: false,
    };
    let edge = format!("---\nsuperseded_by: {linked}\n---\nOlder.\n");
    Builder::from_cards([card(linked, "Newer.\n", 1), card("older", &edge, 2)], 2).snapshot()
}

/// Search answers from the published graph, which keeps an edge its owner has
/// since dropped from the store. Either merge card at its end holds the merge,
/// whichever of the two it is. A snapshot without the edge does not.
#[tokio::test]
async fn a_merge_card_in_the_published_lineage_graph_is_held_for_review() {
    let r = pair().await;
    let body = r.body(&open(&r).await);
    let hold = |snapshot| r.hold_as(&body, "admin", ADMIN, None, Some(snapshot));
    assert_eq!(hold(published("other-card")), None);
    for (name, id) in [("keeper", KEEP), ("retired", GONE)] {
        assert_eq!(hold(published(id)), Some(LINEAGE), "{name}");
    }
}

/// An apply rewrites the keeper's writer and class, and a connector sees an
/// agent-unreviewed head only for its writer. The ranking digest cannot see
/// that, so a different applier holds the merge.
#[tokio::test]
async fn a_merge_held_when_the_applier_is_not_the_unreviewed_keepers_writer() {
    let r = pair().await;
    let body = r.body(&open(&r).await);
    assert_eq!(held(&r, &body), None);
    let own = "UPDATE memory_cards SET writer='node:one',writer_class='node' WHERE card_id=?1";
    sql(&r, own, KEEP);
    for (principal, class) in [(ADMIN, "admin"), ("node:two", "node")] {
        let hold = r.hold_as(&body, class, principal, None, None);
        assert_eq!(hold, Some(WRITER), "{principal} {class}");
    }
    // The retired card is judged the same way, so one foreign card holds it.
    assert_eq!(
        r.hold_as(&body, "node", "node:one", None, None),
        Some(WRITER)
    );
    // The same credential wrote both, so the apply rewrites nothing that moves.
    sql(&r, own, GONE);
    assert_eq!(r.hold_as(&body, "node", "node:one", None, None), None);
    sql(
        &r,
        "UPDATE memory_cards SET writer_class='admin' WHERE card_id=?1",
        GONE,
    );
    assert_eq!(
        r.hold_as(&body, "node", "node:one", None, None),
        Some(WRITER)
    );
}

/// Claims compare bodies only, so a copy of a card's body is an exact duplicate
/// of it. Whoever wrote the copy last must never be able to retire the original.
#[tokio::test]
async fn a_newer_copy_cannot_retire_the_original() {
    // The original is KEEP, and GONE is the copy written after it.
    let r = pair().await;
    let kept_original = r.body(&open(&r).await);
    assert_eq!(held(&r, &kept_original), None);
    // The copy as keeper would retire the original, so it is held.
    let r = pair().await;
    let reversed = open_ops(&r, GONE, &[KEEP]).await;
    assert_eq!(held(&r, &r.body(&reversed)), Some(RETIRES_OLDER));
    // Without a first revision to compare, nothing proves who came first.
    let r = pair().await;
    let body = r.body(&open(&r).await);
    let wipe = "DELETE FROM memory_card_revisions WHERE card_id=?1";
    sql(&r, wipe, KEEP);
    assert_eq!(held(&r, &body), Some(RETIRES_OLDER));
}

/// A new revision does not make a card newer. Only its first write counts.
#[tokio::test]
async fn editing_the_original_does_not_make_it_newer() {
    let r = Real::untrusted(&[(KEEP, &dup(ALPHA, "aaaaaa")), (GONE, &dup(ALPHA, "bbbbbb"))]).await;
    let first = card(&r.h, "fleet", KEEP).await;
    let text = dup(ALPHA, "aaaaaa");
    let again =
        r.h.put(
            ADMIN_TOKEN,
            "fleet",
            KEEP,
            first["revision"].as_i64(),
            &text,
        )
        .await;
    assert_eq!(again.status, 201, "{again:?}");
    assert!(card(&r.h, "fleet", KEEP).await["revision"].as_i64() > first["revision"].as_i64());
    assert_eq!(held(&r, &r.body(&open(&r).await)), None);
}

#[tokio::test]
async fn a_merge_group_onto_one_keeper_passes_and_is_held_as_a_whole() {
    let third = "dup-third";
    let cards = [
        (KEEP, dup("", "aaaaaa")),
        (GONE, dup("", "bbbbbb")),
        (third, dup("", "cccccc")),
    ];
    let make = || async {
        let refs: Vec<(&str, &str)> = cards.iter().map(|(i, t)| (*i, t.as_str())).collect();
        Real::untrusted(&refs).await
    };
    let r = make().await;
    let opened = open_ops(&r, KEEP, &[GONE, third]).await;
    assert_eq!(held(&r, &r.body(&opened)), None);
    let applied = decide(&r.h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    for id in [GONE, third] {
        assert_eq!(card(&r.h, "fleet", id).await["state"], "retired", "{id}");
    }
    let text = card(&r.h, "fleet", KEEP).await["text"].clone();
    let text = text.as_str().unwrap();
    assert!(text.contains("fleet/dup-retired") && text.contains("fleet/dup-third"));
    // One bad member holds the proposal, naming whichever check it fails.
    let r = make().await;
    let body = r.body(&open_ops(&r, KEEP, &[GONE, third]).await);
    link(&r, "See [[dup-third]].\n").await;
    assert_eq!(held(&r, &body), Some(INBOUND));
    let r = make().await;
    let body = r.body(&open_ops(&r, KEEP, &[GONE, third]).await);
    sql(
        &r,
        "UPDATE memory_cards SET trust='reviewed' WHERE card_id=?1",
        third,
    );
    assert_eq!(held(&r, &body), Some(TRUST));
    // The keeper's own place in the log decides for every member.
    let r = make().await;
    let reversed = open_ops(&r, third, &[KEEP, GONE]).await;
    assert_eq!(held(&r, &r.body(&reversed)), Some(RETIRES_OLDER));
}

/// Retiring a card the unreviewed keeper's writer did not write would pull a
/// card out of search for its writer. The retired card is judged like the keeper.
#[tokio::test]
async fn an_unreviewed_retired_card_must_be_the_appliers_too() {
    let r = pair().await;
    let body = r.body(&open(&r).await);
    let foreign = "UPDATE memory_cards SET writer='node:two',writer_class='node' WHERE card_id=?1";
    sql(&r, foreign, GONE);
    assert_eq!(held(&r, &body), Some(WRITER));
    // Reviewed cards are visible to everyone, so who wrote them does not matter.
    for id in [KEEP, GONE] {
        sql(
            &r,
            "UPDATE memory_cards SET trust='reviewed' WHERE card_id=?1",
            id,
        );
    }
    assert_ne!(held(&r, &body), Some(WRITER));
}

#[tokio::test]
async fn forged_bodies_are_held() {
    let r = Real::untrusted(&[(KEEP, &dup(ALPHA, "aaaaaa")), (GONE, &dup(ALPHA, "bbbbbb"))]).await;
    let body = r.body(&open(&r).await);
    assert_eq!(held(&r, &body), None);
    // Only the keeper changes, so the retired card's text must stay as it is.
    let touched = edit(&body, GONE, |t| format!("{t}Extra.\n"));
    assert_eq!(held(&r, &touched), Some(SHAPE));
    let mut stale = body.clone();
    let before = stale["cards"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|c| c["card_id"] == GONE)
        .unwrap();
    before["before_text"] = json!("Other text.\n");
    assert_eq!(held(&r, &stale), Some(SHAPE));
    // The apply stores the blob under `after_hash`, so it must hash `after_text`.
    let mut forged = body.clone();
    let keeper = forged["cards"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|c| c["card_id"] == KEEP)
        .unwrap();
    keeper["after_hash"] = json!("0".repeat(64));
    assert_eq!(held(&r, &forged), Some(KEEPER));
}

/// Token counts do not carry punctuation, and literal search matches it.
/// Tier 0 needs equal front matter at open, so a retired card that later reads
/// differently is modeled by rewriting its stored bytes.
#[tokio::test]
async fn literal_search_must_still_find_what_the_retired_card_held() {
    let rich = "---\ntitle: CI deploy\ntags: [ci, deploy]\nsummary: ci deploy\n---\n";
    let r = Real::untrusted(&[(KEEP, &dup(rich, "aaaaaa")), (GONE, &dup(rich, "bbbbbb"))]).await;
    let body = r.body(&open(&r).await);
    assert_eq!(held(&r, &body), None);
    let rewrite = "UPDATE memory_blobs SET bytes=CAST(?1 AS BLOB) WHERE content_hash=(SELECT content_hash FROM memory_cards WHERE card_id='dup-retired')";
    // Each variant keeps every token the keeper holds. Only punctuation moves.
    for (name, from, to) in [
        ("title", "title: CI deploy", "title: CI -> deploy"),
        ("tags", "tags: [ci, deploy]", "tags: [ci-deploy]"),
        ("summary", "summary: ci deploy", "summary: ci, deploy"),
    ] {
        let text = dup(&rich.replace(from, to), "bbbbbb");
        sql(&r, rewrite, &text);
        assert_eq!(
            held(&r, &retext(&body, GONE, &text)),
            Some(LITERAL),
            "{name}"
        );
    }
}

/// The merge rewrites the `aliases` line, so it is not compared as a line.
#[tokio::test]
async fn the_retired_cards_own_aliases_do_not_count_as_lost_literal_text() {
    let cards = [
        (KEEP, dup("---\ntitle: Alpha\n---\n", "aaaaaa")),
        (
            GONE,
            dup(
                "---\ntitle: Alpha\naliases: [\"old name\"]\n---\n",
                "bbbbbb",
            ),
        ),
    ];
    let r = Real::untrusted(&[(cards[0].0, &cards[0].1), (cards[1].0, &cards[1].1)]).await;
    assert_eq!(held(&r, &r.body(&open(&r).await)), None);
}

/// The writer closes front matter at the first `---` line and search at the
/// first line that trims to `---`. When they disagree, `without_key` drops
/// nothing, so the alias line the merge writes is searchable text and the
/// keeper's projection moves.
#[tokio::test]
async fn a_front_matter_fence_the_readers_disagree_on_is_not_admitted() {
    let split = "---\ntitle: Alpha\n--- \nnote: x\n---\n";
    let r = Real::untrusted(&[(KEEP, &dup(split, "aaaaaa")), (GONE, &dup(split, "bbbbbb"))]).await;
    let opened = open(&r).await;
    assert_eq!(held(&r, &r.body(&opened)), Some(KEEPER));
}

/// The alias the merge writes names the retired id as a predecessor of the
/// keeper. Only trusted cards form edges, and the keeper of a Tier 0 merge is
/// not trusted, so a card that later reuses the retired id gains no edge to it.
#[tokio::test]
async fn the_keepers_alias_makes_no_edge_to_a_card_that_reuses_the_retired_id() {
    let r = pair().await;
    apply_merge(&r).await;
    assert_eq!(card(&r.h, "fleet", GONE).await["state"], "retired");
    let revision = card(&r.h, "fleet", GONE).await["revision"].as_i64();
    let reused =
        r.h.put(
            ADMIN_TOKEN,
            "fleet",
            GONE,
            revision,
            "A new card that reuses the retired id.\n",
        )
        .await;
    assert_eq!(reused.status, 201, "{reused:?}");
    let live = live_cards(&r).await;
    let edges = |trust: &str| {
        let mut cards = live.clone();
        for c in &mut cards {
            c.trust = trust.into();
        }
        let snap = Builder::from_cards(cards, 1).snapshot();
        (
            snap.in_lineage("fleet", KEEP),
            snap.in_lineage("fleet", GONE),
        )
    };
    assert_eq!(edges("agent-unreviewed"), (false, false));
    // Control: once the keeper is trusted the alias is a live edge. The merge
    // gate must not admit that pair without a rule for it.
    assert_eq!(edges("reviewed"), (true, true));
}

const QUERIES: [&str; 14] = [
    "gate",
    "claim",
    "shared",
    "shared gate claim",
    "topic",
    "words",
    "retired",
    "dup",
    "keeper",
    "dup-retired",
    "reviewed",
    "entirely",
    "note",
    "zzz",
];
/// Every card id the ranking tests put, live or retired.
const RANKED: [&str; 8] = [
    KEEP,
    GONE,
    "tight-note",
    "long-note",
    "other-card",
    "sibling",
    "wide-card",
    "tight-card",
];
/// Every live card among [`RANKED`], as the index would read it.
async fn live_cards(r: &Real) -> Vec<Card> {
    let mut cards = Vec::new();
    for id in RANKED {
        let c = card(&r.h, "fleet", id).await;
        let text = |key: &str| c[key].as_str().unwrap().to_string();
        if c["state"] == "live" {
            cards.push(Card {
                scope: "fleet".into(),
                card_id: id.into(),
                revision: c["revision"].as_i64().unwrap(),
                content_hash: text("content_hash"),
                trust: text("trust"),
                text: text("text"),
                automated: false,
            });
        }
    }
    cards
}
/// Top results for each query over every live card, as card ids.
async fn rankings(r: &Real, queries: &[&str]) -> Vec<Vec<String>> {
    let snap = Builder::from_cards(live_cards(r).await, 1).snapshot();
    let allowed = vec![true; snap.scopes().len()];
    let ranked = |q: &str| {
        let hits = snap.score(&allowed, &text::tokenize(q));
        snap.top(hits, usize::MAX)
            .into_iter()
            .map(|(n, _)| snap.doc(n).card_id.to_string())
            .collect()
    };
    queries.iter().map(|q| ranked(q)).collect()
}
async fn apply_merge(r: &Real) {
    let opened = open(r).await;
    let applied = decide(&r.h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
}

/// What the gate enforces. The retired card's matches move to the keeper, whose
/// searchable content is a superset of the retired card's. Nothing else is
/// promised about order, so this pins the matching set and not the ranking.
#[tokio::test]
async fn the_retired_cards_matches_move_to_the_keeper() {
    let r = Real::untrusted(&[
        (KEEP, &dup("", "aaaaaa")),
        (GONE, &dup("", "bbbbbb")),
        (
            "tight-note",
            "Shared gate claim reviewed twice. ^c-dddddd\n",
        ),
        (
            "long-note",
            "A long note about the gate and many unrelated words that dilute it. ^c-eeeeee\n",
        ),
        ("other-card", "Another topic entirely. ^c-cccccc\n"),
        ("sibling", "Shared claim without the g word. ^c-ffffff\n"),
    ])
    .await;
    let before = rankings(&r, &QUERIES).await;
    apply_merge(&r).await;
    let after = rankings(&r, &QUERIES).await;
    for ((query, was), now) in QUERIES.iter().zip(&before).zip(&after) {
        let mut want: BTreeSet<&str> = BTreeSet::new();
        for id in was.iter().map(|id| if id == GONE { KEEP } else { id }) {
            want.insert(id);
        }
        let got: BTreeSet<&str> = now.iter().map(String::as_str).collect();
        // Every card that matched still matches, the retired one as the keeper.
        assert!(
            want.is_subset(&got),
            "{query}: before {was:?} after {now:?}"
        );
        // Only the keeper can match more, through the alias line it gained.
        let extra: Vec<_> = got.difference(&want).collect();
        assert!(
            extra.iter().all(|id| **id == KEEP),
            "{query}: {extra:?} gained a match"
        );
    }
    // The keeper's alias line lengthens its document and every statistic moves,
    // so scores shift and the keeper can fall below a close competitor.
    assert_eq!(before[0], [KEEP, GONE, "tight-note", "long-note"]);
    assert_eq!(after[0], ["tight-note", KEEP, "long-note"]);
    // Control: the keeper really does move into the retired card's results.
    assert!(before[6].iter().all(|id| id != KEEP) && after[6] == [KEEP]);
}

/// The gate does not freeze other cards' order. Retiring a card changes the
/// scope's document count, document frequencies and average length, so two
/// cards that are neither the keeper nor the retired card can swap places even
/// on one term. Pinned so the docs keep saying only what is enforced.
#[tokio::test]
async fn two_non_keeper_cards_can_swap_places_on_a_single_term() {
    let r = Real::untrusted(&[
        (KEEP, &dup("", "aaaaaa")),
        (GONE, &dup("", "bbbbbb")),
        ("wide-card", "Alpha alpha one two three four. ^c-aaaaa1\n"),
        ("tight-card", "Alpha. ^c-aaaaa2\n"),
    ])
    .await;
    let before = rankings(&r, &["alpha"]).await;
    apply_merge(&r).await;
    let after = rankings(&r, &["alpha"]).await;
    assert_eq!(before[0], ["tight-card", "wide-card"]);
    assert_eq!(after[0], ["wide-card", "tight-card"]);
}
