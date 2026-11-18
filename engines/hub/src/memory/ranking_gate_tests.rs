//! The real-scope gate admits only edits that search cannot see (#2591).
//! Every hub here uses the real scope `fleet`, never the fixture scope.
use super::{
    index::{Builder, Card},
    proposal_blocks::anchor_blocks,
    proposal_metadata,
    proposal_tests::*,
    search,
    tests::*,
    text, *,
};
use crate::writer::worker::Tx;
use rusqlite::Connection;
use serde_json::{Value as Json, json};
use std::collections::HashMap;

const PROJECTION: &str = "retrieval_eval_projection_changed";
const TARGETS: [&str; 6] = [
    "Claim.\n",
    "---\ntitle: T\nevidence: [\"a/b/c\"]\n---\nOne.\n\n- Two. ^c-abcdef\n",
    "---\ntitle: T\n---\nA < B.\n\nOne.\n",
    "---\nevidence: []\n---\nOne.\n\nTwo.\n",
    "One.\n\n- Two.\t\n",
    // A blank last metadata line, which `set` drops when it rewrites the card.
    "---\ntitle: T\nevidence: [\"a/b/c\"]\n\n---\nOne.\n\n- Two. ^c-abcdef\n",
];
const QUERIES: [&str; 13] = [
    "claim",
    "keyword",
    "evidence",
    "abcdef",
    "c",
    "ns",
    "shared",
    "^",
    "/",
    "^c-abcdef",
    "ns/keyword",
    "-",
    "---",
];

fn append(text: &str, refs: &[&str]) -> String {
    let op = json!({"type":"append_evidence","evidence_ids":refs});
    proposal_metadata::apply(text, &op, None, &Default::default()).unwrap()
}
fn stuffed() -> Vec<&'static str> {
    vec![
        "ns/keyword-keyword-keyword/shared",
        "ns/<claim>/claim",
        "ns/a^c-abcdef/b",
    ]
}

#[test]
fn ranking_text_ignores_what_anchoring_and_evidence_add() {
    for before in TARGETS {
        let anchored = anchor_blocks(before, "a").unwrap();
        assert_eq!(
            text::ranking_text(&anchored),
            text::ranking_text(before),
            "{before:?}"
        );
        let with_evidence = append(before, &stuffed());
        assert_eq!(
            text::ranking_text(&with_evidence),
            text::ranking_text(before),
            "{before:?}"
        );
        let both = append(&anchored, &["ns/x/y"]);
        assert_eq!(text::ranking_text(&both), text::ranking_text(before));
    }
}

fn answers(target: &str) -> Vec<Vec<(String, f64)>> {
    let others = [
        ("b", "claim shared words ^ here / there --- -"),
        ("c", "keyword evidence ns c abcdef"),
    ];
    let mut bodies = HashMap::new();
    let mut cards = Vec::new();
    for (i, (id, body)) in std::iter::once(("a", target)).chain(others).enumerate() {
        let hash = format!("hash-{id}-{}", target.len());
        bodies.insert(hash.clone(), body.to_string());
        cards.push(Card {
            scope: "fleet".into(),
            card_id: id.into(),
            revision: i as i64 + 1,
            content_hash: hash,
            trust: "agent-unreviewed".into(),
            text: body.into(),
            automated: false,
        });
    }
    let snap = Builder::from_cards(cards, 1).snapshot();
    let allowed = vec![true; snap.scopes().len()];
    QUERIES
        .iter()
        .map(|query| {
            let tokens = text::tokenize(query);
            let hits = if tokens.is_empty() {
                search::literal(&snap, &allowed, query, usize::MAX, |h| {
                    Ok(bodies.get(h).cloned())
                })
                .unwrap()
                .hits
            } else {
                snap.score(&allowed, &tokens)
            };
            snap.top(hits, usize::MAX)
                .into_iter()
                .map(|(n, score)| (snap.doc(n).card_id.to_string(), score))
                .collect()
        })
        .collect()
}

#[test]
fn every_query_ranks_the_same_after_an_admitted_edit() {
    for before in TARGETS {
        let want = answers(before);
        assert_eq!(answers(&anchor_blocks(before, "a").unwrap()), want);
        assert_eq!(answers(&append(before, &stuffed())), want, "{before:?}");
    }
    // Control: a real edit does move the answers, so the check can fail.
    assert_ne!(answers("Claim.\n"), answers("Claim. keyword\n"));
}

pub(super) struct Real {
    pub(super) h: Hub,
    pub(super) db: Connection,
    hub: Connection,
}
impl Real {
    pub(super) async fn new(cards: &[(&str, &str)]) -> Self {
        Self::build("fleet", cards, false).await
    }
    /// Trusted cards on the reserved fixture scope, which the merge gate never sees.
    pub(super) async fn fixture(cards: &[(&str, &str)]) -> Self {
        Self::build(super::retrieval_eval::FIXTURE_SCOPE, cards, false).await
    }
    /// Cards put by the admin stay `agent-unreviewed`, the class a merge can use.
    pub(super) async fn untrusted(cards: &[(&str, &str)]) -> Self {
        Self::build("fleet", cards, true).await
    }
    async fn build(scope: &str, cards: &[(&str, &str)], put: bool) -> Self {
        let h = Hub::with(clean(), None);
        attestation_tests::policy(&h);
        let hub = Connection::open(&h.hub).unwrap();
        let raw: String = hub
            .query_row("SELECT document FROM fleet_policy_revisions", [], |r| {
                r.get(0)
            })
            .unwrap();
        let mut policy: Json = serde_json::from_str(&raw).unwrap();
        policy["memory"]["tier0_op_classes"] =
            json!(["append_evidence", "anchor_blocks", "merge_exact_duplicates"]);
        hub.execute(
            "UPDATE fleet_policy_revisions SET document=?1",
            [policy.to_string()],
        )
        .unwrap();
        grant(&h, scope, &["read", "reflect"]).await;
        for (id, body) in cards {
            let put = if put {
                h.put(ADMIN_TOKEN, scope, id, None, body).await
            } else {
                h.call(ADMIN_TOKEN, json!({"action":"import","scope":scope,"card_id":id,"text":body,"client_event_id":h.id()})).await
            };
            assert_eq!(put.status, 201, "{put:?}");
        }
        let db = Connection::open(h.memory()).unwrap();
        Self { h, db, hub }
    }
    /// Model a head written by an earlier gated automatic apply.
    pub(super) fn auto_authored(&self, id: &str) {
        self.db.execute(
            "INSERT INTO memory_events(seq,event_id,client_event_id,kind,principal,scope,card_id,revision,content_hash,trust,payload,created_at) SELECT 900000,'prior-auto','prior-auto','proposal.apply',writer,scope,card_id,900000,content_hash,trust,'{\"decision_mode\":\"automatic\"}',?2 FROM memory_cards WHERE scope='fleet' AND card_id=?1",
            rusqlite::params![id, ids::isoformat(self.h.now() - 86_401_000)],
        ).unwrap();
        self.db.execute("INSERT INTO memory_card_revisions SELECT scope,card_id,900000,content_hash,trust,state,writer,900000,NULL,scan_state,created_at FROM memory_card_revisions WHERE scope='fleet' AND card_id=?1", [id]).unwrap();
        self.db
            .execute(
                "UPDATE memory_cards SET revision=900000 WHERE scope='fleet' AND card_id=?1",
                [id],
            )
            .unwrap();
    }
    async fn revision(&self, id: &str) -> i64 {
        card(&self.h, "fleet", id).await["revision"]
            .as_i64()
            .unwrap()
    }
    pub(super) async fn evidence(&self, id: &str, refs: &[&str]) -> Reply {
        let current = card(&self.h, "fleet", id).await;
        // Evidence is appended through the card's first anchored block.
        let pin = proposal_blocks::pins(current["text"].as_str().unwrap()).remove(0);
        let op = json!({"type":"append_evidence","scope":"fleet","card_id":id,
            "base_revision":current["revision"],"block_id":pin["block_id"],
            "expected_block_hash":pin["hash"],"evidence_ids":refs});
        submit(&self.h, ADMIN_TOKEN, vec![op]).await
    }
    pub(super) async fn anchor(&self, id: &str) -> Reply {
        let op = json!({"type":"anchor_blocks","scope":"fleet","card_id":id,"base_revision":self.revision(id).await});
        submit(&self.h, ADMIN_TOKEN, vec![op]).await
    }
    pub(super) fn body(&self, opened: &Reply) -> Json {
        assert_eq!(opened.status, 201, "{opened:?}");
        let raw: String = self
            .db
            .query_row(
                "SELECT body FROM memory_proposals WHERE proposal_id=?1",
                [opened.body["proposal_id"].as_str().unwrap()],
                |r| r.get(0),
            )
            .unwrap();
        serde_json::from_str(&raw).unwrap()
    }
    pub(super) fn hold(
        &self,
        body: &Json,
        applier: &'static str,
        dense: Option<&str>,
    ) -> Option<&'static str> {
        self.hold_as(body, applier, ADMIN, dense, None)
    }
    /// The gate as a given credential would meet it, against a given published index.
    pub(super) fn hold_as(
        &self,
        body: &Json,
        applier: &'static str,
        principal: &str,
        dense: Option<&str>,
        snapshot: Option<std::sync::Arc<index::Snapshot>>,
    ) -> Option<&'static str> {
        let mut authority =
            ops::Authority::with_credentials(&Tx(&self.hub), &digest(ADMIN_TOKEN)).unwrap();
        authority.retrieval = Some(retrieval_eval::Live {
            applier,
            principal: principal.into(),
            snapshot,
            indexed_seq: self.h.events(),
            embedder_id: dense.map(Into::into),
            weights: json!({"lexical":1.0,"dense":1.0}),
        });
        retrieval_eval::hold(&Tx(&self.db), body, &authority, self.h.now()).unwrap()
    }
    /// The whole preflight report as a given credential would meet it.
    pub(super) fn evaluate_as(&self, body: &Json, applier: &'static str, principal: &str) -> Json {
        let mut authority =
            ops::Authority::with_credentials(&Tx(&self.hub), &digest(ADMIN_TOKEN)).unwrap();
        authority.retrieval = Some(retrieval_eval::Live {
            applier,
            principal: principal.into(),
            snapshot: None,
            indexed_seq: self.h.events(),
            embedder_id: None,
            weights: json!({"lexical":1.0,"dense":1.0}),
        });
        preflight::evaluate(
            &Tx(&self.db),
            body,
            &authority,
            clean().as_ref(),
            self.h.now(),
        )
        .unwrap()
    }
}

#[tokio::test]
async fn evidence_and_anchoring_are_admitted_on_a_real_scope() {
    let r = Real::new(&[(
        "a",
        "---\ntitle: Alpha\n---\nClaim one. ^c-abcdef\n\nClaim two.\n",
    )])
    .await;
    assert_eq!(card(&r.h, "fleet", "a").await["trust"], "imported");
    let opened = r.evidence("a", &stuffed()).await;
    assert_eq!(opened.body["tier"], 0, "{opened:?}");
    assert_eq!(r.hold(&r.body(&opened), "admin", None), None);
    // Anchoring needs a head the hub's own automatic apply wrote.
    r.auto_authored("a");
    let opened = r.anchor("a").await;
    assert_eq!(opened.body["tier"], 0, "{opened:?}");
    let body = r.body(&opened);
    assert_eq!(r.hold(&body, "admin", None), None);
    let report = attestation_tests::report(&r.h, &opened.body["proposal_id"]).await;
    let held: Vec<_> = report.body["checks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["state"] != "satisfied")
        .map(|c| c["code"].clone())
        .collect();
    assert!(held.is_empty(), "{report:?}");
    let before = card(&r.h, "fleet", "a").await;
    let applied =
        r.h.call(
            ADMIN_TOKEN,
            attestation_tests::auto_request(&r.h, &opened.body["proposal_id"]).await,
        )
        .await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let after = card(&r.h, "fleet", "a").await;
    assert_eq!(
        after["text"],
        anchor_blocks(before["text"].as_str().unwrap(), "a").unwrap()
    );
    assert_eq!(
        text::ranking_text(after["text"].as_str().unwrap()),
        text::ranking_text(before["text"].as_str().unwrap())
    );
}

#[tokio::test]
async fn gate_holds_everything_search_could_see() {
    let r = Real::new(&[
        ("a", "---\ntitle: Alpha\n---\nClaim one. ^c-abcdef\n"),
        ("b", "Other claim.\n"),
    ])
    .await;
    let opened = r.evidence("a", &["ns/one/two"]).await;
    let body = r.body(&opened);
    assert_eq!(r.hold(&body, "admin", None), None);
    // A hybrid index also reads content hashes, so it is never admitted.
    assert_eq!(r.hold(&body, "admin", Some("embedder")), Some(PROJECTION));
    // The applier's class becomes the card's writer class, which flips
    // source demotion for a card an admin wrote.
    assert_eq!(r.hold(&body, "principal", None), Some(PROJECTION));
    let mut mixed = body.clone();
    let replace = json!({"type":"replace_block","scope":"fleet","card_id":"a"});
    mixed["ops"].as_array_mut().unwrap().push(replace);
    assert_eq!(r.hold(&mixed, "admin", None), Some(PROJECTION));
    let mut merge = body.clone();
    merge["ops"][0]["type"] = json!("merge_exact_duplicates");
    assert_eq!(
        r.hold(&merge, "admin", None),
        Some("retrieval_eval_merge_shape")
    );
    let mut stale = body.clone();
    stale["cards"][0]["base_revision"] = json!(999);
    assert_eq!(
        r.hold(&stale, "admin", None),
        Some("retrieval_eval_target_drifted")
    );
    // A forged after text that adds a searchable word is not admitted.
    let mut forged = body.clone();
    let after = format!(
        "{}keyword\n",
        forged["cards"][0]["after_text"].as_str().unwrap()
    );
    forged["cards"][0]["after_text"] = json!(after);
    assert_eq!(r.hold(&forged, "admin", None), Some(PROJECTION));
}

#[tokio::test]
async fn ambiguous_evidence_lines_never_reach_the_gate() {
    for (name, text) in [
        (
            "duplicate",
            "---\nevidence: [\"a/b/c\"]\nevidence: [\"d/e/f\"]\n---\nClaim. ^c-abcdef\n",
        ),
        (
            "multiline",
            "---\nevidence: |\n  a/b/c\n---\nClaim. ^c-abcdef\n",
        ),
        (
            "separator",
            "---\nevidence: [\"a/b/c\"]\u{2028}title: X\n---\nClaim. ^c-abcdef\n",
        ),
    ] {
        let r = Real::new(&[("a", text)]).await;
        let opened = r.evidence("a", &["ns/keyword/one"]).await;
        let admitted = opened.status == 201 && r.hold(&r.body(&opened), "admin", None).is_none();
        assert!(!admitted, "{name}: {opened:?}");
    }
}

#[tokio::test]
async fn trusted_lineage_targets_go_to_review() {
    for (name, cards) in [
        (
            "cycle",
            [
                ("a", "---\nsuperseded_by: b\n---\nClaim a. ^c-abcdef\n"),
                ("b", "---\nsuperseded_by: a\n---\nClaim b.\n"),
            ],
        ),
        (
            "named by another card",
            [
                ("a", "Claim a. ^c-abcdef\n"),
                ("b", "---\nsuperseded_by: a\n---\nClaim b.\n"),
            ],
        ),
        (
            "named as a predecessor",
            [
                ("a", "Claim a. ^c-abcdef\n"),
                ("b", "---\nsupersedes: [cards/a.md]\n---\nClaim b.\n"),
            ],
        ),
    ] {
        let r = Real::new(&cards).await;
        let opened = r.evidence("a", &["ns/one/two"]).await;
        assert_eq!(opened.body["tier"], 2, "{name}: {opened:?}");
        let id = opened.body["proposal_id"].clone();
        let automatic =
            r.h.call(
                ADMIN_TOKEN,
                attestation_tests::auto_request(&r.h, &id).await,
            )
            .await;
        assert_eq!(automatic.body["state"], "pending", "{name}: {automatic:?}");
        assert!(
            automatic.body["preflight"]["checks"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["code"] == "lineage_requires_review"),
            "{name}: {automatic:?}"
        );
        // The proposal stays open and a reviewer can still accept it.
        let applied = decide(&r.h, ADMIN_TOKEN, &id).await;
        assert_eq!(applied.body["state"], "applied", "{name}: {applied:?}");
    }
}

#[tokio::test]
async fn a_link_added_after_tiering_is_caught_by_the_gate() {
    let r = Real::new(&[("a", "Claim a. ^c-abcdef\n")]).await;
    let opened = r.evidence("a", &["ns/one/two"]).await;
    let body = r.body(&opened);
    assert_eq!(opened.body["tier"], 0);
    assert_eq!(r.hold(&body, "admin", None), None);
    let linked = "---\nsuperseded_by: a\n---\nClaim b.\n";
    let put = r.h.call(ADMIN_TOKEN, json!({"action":"import","scope":"fleet","card_id":"b","text":linked,"client_event_id":r.h.id()})).await;
    assert_eq!(put.status, 201, "{put:?}");
    assert_eq!(
        r.hold(&body, "admin", None),
        Some("retrieval_eval_lineage_exposed")
    );
}

/// `Links::parse` decodes values with `literal_eval`, so a neighbour can name
/// the target in ways its raw bytes never spell.
const SPELLED_OUT: [(&str, &str); 3] = [
    (
        "implicit concatenation",
        "---\nsupersedes: ['a' 'b']\n---\nClaim n.\n",
    ),
    (
        "escaped id",
        "---\nsuperseded_by: [\"\\x61b\"]\n---\nClaim n.\n",
    ),
    (
        "escaped and concatenated",
        "---\nmerged_from: ['\\x61' \"\\x62\"]\n---\nClaim n.\n",
    ),
];

#[test]
fn the_spelled_out_neighbours_really_name_the_target() {
    for (name, text) in SPELLED_OUT {
        let links = ranking::Links::parse(text, "fleet");
        let named = links.successor.as_deref() == Some("ab")
            || links.predecessors.iter().any(|p| p == "ab");
        assert!(named, "{name}: {links:?}");
        assert!(!text.contains("ab"), "{name} must not spell the id");
    }
}

#[tokio::test]
async fn a_neighbour_that_spells_the_id_out_still_sends_the_target_to_review() {
    for (name, neighbour) in SPELLED_OUT {
        // Tiering sees the neighbour already in the store.
        let r = Real::new(&[("ab", "Claim a. ^c-abcdef\n"), ("n", neighbour)]).await;
        let opened = r.evidence("ab", &["ns/one/two"]).await;
        assert_eq!(opened.body["tier"], 2, "{name}: {opened:?}");
        let id = opened.body["proposal_id"].clone();
        let automatic =
            r.h.call(
                ADMIN_TOKEN,
                attestation_tests::auto_request(&r.h, &id).await,
            )
            .await;
        assert_eq!(automatic.body["state"], "pending", "{name}: {automatic:?}");
        // The backstop sees a neighbour added after tiering.
        let r = Real::new(&[("ab", "Claim a. ^c-abcdef\n")]).await;
        let opened = r.evidence("ab", &["ns/one/two"]).await;
        let body = r.body(&opened);
        assert_eq!(opened.body["tier"], 0, "{name}");
        assert_eq!(r.hold(&body, "admin", None), None, "{name}");
        let put = r.h.call(ADMIN_TOKEN, json!({"action":"import","scope":"fleet","card_id":"n","text":neighbour,"client_event_id":r.h.id()})).await;
        assert_eq!(put.status, 201, "{name}: {put:?}");
        assert_eq!(
            r.hold(&body, "admin", None),
            Some("retrieval_eval_lineage_exposed"),
            "{name}"
        );
    }
}

#[tokio::test]
async fn an_edge_only_the_published_index_still_has_holds_the_target() {
    let r = Real::new(&[("a", "Claim a. ^c-abcdef\n")]).await;
    let opened = r.evidence("a", &["ns/one/two"]).await;
    let body = r.body(&opened);
    assert_eq!(opened.body["tier"], 0);
    // The store shows no link to `a`, but the snapshot search answers from does.
    let cards = |linked: bool| {
        let card = |id: &str, text: &str, revision| Card {
            scope: "fleet".into(),
            card_id: id.into(),
            revision,
            content_hash: format!("hash-{id}"),
            trust: "imported".into(),
            text: text.into(),
            automated: false,
        };
        let neighbour = if linked {
            "---\nsuperseded_by: a\n---\nClaim b.\n"
        } else {
            "Claim b.\n"
        };
        Builder::from_cards([card("a", "Claim a.\n", 1), card("b", neighbour, 2)], 2).snapshot()
    };
    assert!(cards(true).in_lineage("fleet", "a"));
    assert!(!cards(false).in_lineage("fleet", "a"));
    assert!(!cards(true).in_lineage("other", "a"));
    assert_eq!(
        r.hold_as(&body, "admin", ADMIN, None, Some(cards(true))),
        Some("retrieval_eval_lineage_exposed")
    );
    assert_eq!(
        r.hold_as(&body, "admin", ADMIN, None, Some(cards(false))),
        None
    );
}

/// An apply writes the applier's principal and class onto the head. Push and
/// connector search admit an `agent-unreviewed` head only for its writer.
async fn unreviewed(writer: &str, class: &str) -> Real {
    let r = Real::new(&[("a", "Claim a. ^c-abcdef\n")]).await;
    r.db.execute(
        "UPDATE memory_cards SET trust='agent-unreviewed',writer=?1,writer_class=?2 WHERE card_id='a'",
        [writer, class],
    )
    .unwrap();
    r
}

#[tokio::test]
async fn a_different_applier_would_move_an_unreviewed_card_in_push_search() {
    // A node wrote it, so source demotion reads the same after an admin apply
    // and the ranking digest cannot tell the two apart.
    let r = unreviewed("node:one", "node").await;
    let opened = r.evidence("a", &["ns/one/two"]).await;
    assert_eq!(opened.body["tier"], 0, "{opened:?}");
    let body = r.body(&opened);
    for (principal, class) in [
        (ADMIN, "admin"),
        ("node:two", "node"),
        ("node:one", "admin"),
    ] {
        assert_eq!(
            r.hold_as(&body, class, principal, None, None),
            Some("retrieval_eval_writer_changed"),
            "{principal} {class}"
        );
    }
    // The same credential rewrites the same writer and class, so nothing moves.
    assert_eq!(r.hold_as(&body, "node", "node:one", None, None), None);
}

#[tokio::test]
async fn a_reviewed_card_is_not_held_for_its_writer() {
    // Push admits a reviewed or imported head whoever wrote it.
    let r = Real::new(&[("a", "Claim a. ^c-abcdef\n")]).await;
    r.db.execute(
        "UPDATE memory_cards SET writer='node:one',writer_class='node' WHERE card_id='a'",
        [],
    )
    .unwrap();
    let opened = r.evidence("a", &["ns/one/two"]).await;
    assert_eq!(
        r.hold_as(&r.body(&opened), "admin", ADMIN, None, None),
        None
    );
}

/// The automatic path never reaches the writer hold today, because preflight
/// denies an unreviewed target first (`card_trust_denied`). The hold is the
/// backstop for the day that check moves.
#[tokio::test]
async fn an_unreviewed_target_is_denied_before_the_automatic_apply() {
    let r = unreviewed("node:one", "node").await;
    let opened = r.evidence("a", &["ns/one/two"]).await;
    assert_eq!(opened.body["tier"], 0, "{opened:?}");
    let id = opened.body["proposal_id"].clone();
    let automatic =
        r.h.call(
            ADMIN_TOKEN,
            attestation_tests::auto_request(&r.h, &id).await,
        )
        .await;
    assert_eq!(automatic.body["state"], "pending", "{automatic:?}");
    let codes: Vec<_> = automatic.body["preflight"]["checks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["state"] != "satisfied")
        .map(|c| c["code"].clone())
        .collect();
    assert!(codes.contains(&json!("card_trust_denied")), "{codes:?}");
    let writer: String =
        r.db.query_row(
            "SELECT writer FROM memory_cards WHERE card_id='a'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(writer, "node:one");
}

/// `set` rewrites metadata with `str::lines`, which drops a blank last line.
/// The card's search text must read the same on both sides of that rewrite.
/// Tiering has its own metadata comparison, which still sends this card to a
/// reviewer, so the gate is asked directly with the proposal marked Tier 0.
#[tokio::test]
async fn a_blank_metadata_line_does_not_hold_every_evidence_append() {
    let text = "---\ntitle: T\nevidence: [\"a/b/c\"]\n\n---\nClaim a. ^c-abcdef\n";
    let r = Real::new(&[("a", text)]).await;
    let opened = r.evidence("a", &["ns/one/two"]).await;
    let mut body = r.body(&opened);
    body["tier"] = json!(0);
    assert_eq!(r.hold(&body, "admin", None), None);
}

/// One query through the whole `search` path, collapse and push mask included,
/// over a trusted lineage component, before and after an admitted apply to a
/// card that is in no lineage.
#[tokio::test]
async fn an_admitted_apply_leaves_every_search_path_unchanged() {
    let r = Real::new(&[
        ("old", "Shared claim about the old word.\n"),
        (
            "new",
            "---\nsupersedes: [cards/old.md]\n---\nShared claim about the new word.\n",
        ),
        ("other", "Shared claim, other card.\n"),
    ])
    .await;
    let shapes = [
        (
            "plain",
            json!({"action":"search","query":"shared claim word","limit":20}),
        ),
        (
            "connector",
            json!({"action":"search","query":"shared claim word","limit":20,"query_source":"connector"}),
        ),
        (
            "push",
            json!({"action":"search","query":"shared claim word","limit":20,"query_source":"push"}),
        ),
    ];
    let answers = |seq: i64| {
        let (r, shapes) = (&r, &shapes);
        async move {
            let mut out = Vec::new();
            for (name, shape) in shapes {
                let mut request = shape.clone();
                request["min_seq"] = json!(seq);
                let reply = r.h.call(ADMIN_TOKEN, request).await;
                assert_eq!(reply.status, 200, "{name}: {reply:?}");
                assert_eq!(reply.body["retrieval"]["stale"], json!(false), "{name}");
                let hits: Vec<_> = reply.body["matches"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|m| (m["card_id"].clone(), m["score"].clone()))
                    .collect();
                out.push((*name, hits));
            }
            out
        }
    };
    let before = answers(r.h.events()).await;
    // The control: collapse hid the superseded card, so the answer has shape.
    let plain: Vec<_> = before[0].1.iter().map(|(id, _)| id.clone()).collect();
    assert!(plain.contains(&json!("new")) && plain.contains(&json!("other")));
    assert!(!plain.contains(&json!("old")), "{plain:?}");
    assert_eq!(before[0].1.len(), 2);
    // Anchoring is the admitted edit that completes end to end. It adds only
    // a suffix, so the card's stored bytes change and its ranking cannot.
    r.auto_authored("other");
    let opened = r.anchor("other").await;
    assert_eq!(opened.body["tier"], 0, "{opened:?}");
    let id = opened.body["proposal_id"].clone();
    let report = attestation_tests::report(&r.h, &id).await;
    let held: Vec<_> = report.body["checks"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["state"] != "satisfied")
        .map(|c| c["code"].clone())
        .collect();
    assert!(held.is_empty(), "{report:?}");
    let applied =
        r.h.call(
            ADMIN_TOKEN,
            attestation_tests::auto_request(&r.h, &id).await,
        )
        .await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let stored = card(&r.h, "fleet", "other").await;
    assert!(
        stored["text"].as_str().unwrap().contains(" ^c-"),
        "{stored:?}"
    );
    assert_eq!(answers(r.h.events()).await, before);
}

/// The literal scan stops after `LITERAL_BYTES`. An admitted append grows the
/// stored bytes and not the text the scan reads, so it must not use up the
/// budget that later matching cards need.
#[test]
fn an_admitted_append_does_not_spend_the_literal_byte_budget() {
    let refs: Vec<String> = (0..search::LITERAL_BYTES / 12 + 1)
        .map(|n| format!("\"ns/a/{n:06}\""))
        .collect();
    let stuffed = format!("---\nevidence: [{}]\n---\nplain words\n", refs.join(","));
    assert!(stuffed.len() > search::LITERAL_BYTES);
    let mut bodies = HashMap::new();
    let cards: Vec<_> = [("a", stuffed.as_str()), ("b", "match && here\n")]
        .into_iter()
        .enumerate()
        .map(|(i, (id, body))| {
            bodies.insert(format!("hash-{id}"), body.to_string());
            Card {
                scope: "fleet".into(),
                card_id: id.into(),
                revision: i as i64 + 1,
                content_hash: format!("hash-{id}"),
                trust: "agent-unreviewed".into(),
                text: body.into(),
                automated: false,
            }
        })
        .collect();
    let snap = Builder::from_cards(cards, 1).snapshot();
    let allowed = vec![true; snap.scopes().len()];
    let found = search::literal(&snap, &allowed, "&&", usize::MAX, |h| {
        Ok(bodies.get(h).cloned())
    })
    .unwrap();
    let ids: Vec<_> = found
        .hits
        .iter()
        .map(|(n, _)| snap.doc(*n).card_id.to_string())
        .collect();
    assert_eq!(ids, ["b"]);
    assert!(found.complete);
}
