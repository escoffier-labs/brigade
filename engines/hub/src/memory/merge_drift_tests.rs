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
    let (k, o) = (
        card(&r.h, "fleet", KEEP).await,
        card(&r.h, "fleet", GONE).await,
    );
    let pin = proposal_blocks::pins(k["text"].as_str().unwrap()).remove(0);
    let op = json!({"type":"merge_exact_duplicates","scope":"fleet","card_id":KEEP,
        "base_revision":k["revision"],"block_id":pin["block_id"],"expected_block_hash":pin["hash"],
        "other_card":{"scope":"fleet","card_id":GONE,"base_revision":o["revision"]}});
    let opened = submit(&r.h, ADMIN_TOKEN, vec![op]).await;
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
    card["after_text"] = json!(f(card["after_text"].as_str().unwrap()));
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
    // A principal-class applier makes both automated, so the keeper moves instead.
    assert_eq!(r.hold(&body, "principal", None), Some(KEEPER));
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
        ("only the keeper", "See [[dup-keeper]].\n"),
    ];
    for (name, text) in forms {
        let r = pair().await;
        let body = r.body(&open(&r).await);
        link(&r, text).await;
        let want = (name != "only the keeper").then_some(INBOUND);
        assert_eq!(held(&r, &body), want, "{name}");
    }
}

#[tokio::test]
async fn a_link_added_between_preflight_and_apply_is_caught_at_apply() {
    let r = pair().await;
    let opened = open(&r).await;
    assert_eq!(held(&r, &r.body(&opened)), None);
    let request = attestation_tests::auto_request(&r.h, &opened.body["proposal_id"]).await;
    let store = r.h.store.as_ref().unwrap();
    let credential = store.authenticate(ADMIN_TOKEN).await.unwrap();
    let map = request.as_object().unwrap();
    let mut prepared = store
        .prepare(&credential, "auto_apply", map, &request)
        .unwrap();
    let tx = crate::writer::worker::Tx(&r.db);
    prepared.action = Action::Proposal(
        proposals::prepare(&tx, &credential, request.clone(), clean().as_ref()).unwrap(),
    );
    // The preparation read saw no link. One lands before the writer lock.
    link(&r, "See [[dup-retired]].\n").await;
    let reply = store.write(prepared).await;
    let codes = [
        &reply.body["preflight"]["checks"],
        &reply.body["auto_apply_gates"],
    ];
    let codes: Vec<_> = codes
        .iter()
        .filter_map(|c| c.as_array())
        .flatten()
        .map(|c| c["code"].clone())
        .collect();
    assert_ne!(reply.body["state"], "applied", "{reply:?}");
    assert!(codes.contains(&json!(INBOUND)), "{reply:?}");
    assert_eq!(card(&r.h, "fleet", GONE).await["state"], "live");
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
    sql(
        &r,
        "UPDATE memory_cards SET writer='node:one',writer_class='node' WHERE card_id=?1",
        KEEP,
    );
    for (principal, class) in [(ADMIN, "admin"), ("node:two", "node")] {
        let hold = r.hold_as(&body, class, principal, None, None);
        assert_eq!(hold, Some(WRITER), "{principal} {class}");
    }
    // The same credential rewrites the same writer and class, so nothing moves.
    assert_eq!(r.hold_as(&body, "node", "node:one", None, None), None);
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
/// Top results for each query over every live card, as card ids.
async fn rankings(r: &Real, queries: &[&str]) -> Vec<Vec<String>> {
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
    let snap = Builder::from_cards(cards, 1).snapshot();
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
        assert!(want.is_subset(&got), "{query}: before {was:?} after {now:?}");
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
        (
            "wide-card",
            "Alpha alpha one two three four. ^c-aaaaa1\n",
        ),
        ("tight-card", "Alpha. ^c-aaaaa2\n"),
    ])
    .await;
    let before = rankings(&r, &["alpha"]).await;
    apply_merge(&r).await;
    let after = rankings(&r, &["alpha"]).await;
    assert_eq!(before[0], ["tight-card", "wide-card"]);
    assert_eq!(after[0], ["wide-card", "tight-card"]);
}
