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
const TARGETS: [&str; 5] = [
    "Claim.\n",
    "---\ntitle: T\nevidence: [\"a/b/c\"]\n---\nOne.\n\n- Two. ^c-abcdef\n",
    "---\ntitle: T\n---\nA < B.\n\nOne.\n",
    "---\nevidence: []\n---\nOne.\n\nTwo.\n",
    "One.\n\n- Two.\t\n",
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
    proposal_metadata::apply(text, &op, None).unwrap()
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

struct Real {
    h: Hub,
    db: Connection,
    hub: Connection,
}
impl Real {
    async fn new(cards: &[(&str, &str)]) -> Self {
        let h = Hub::with(clean(), None);
        attestation_tests::policy(&h);
        let hub = Connection::open(&h.hub).unwrap();
        let raw: String = hub
            .query_row("SELECT document FROM fleet_policy_revisions", [], |r| {
                r.get(0)
            })
            .unwrap();
        let mut policy: Json = serde_json::from_str(&raw).unwrap();
        policy["memory"]["tier0_op_classes"] = json!(["append_evidence", "anchor_blocks"]);
        hub.execute(
            "UPDATE fleet_policy_revisions SET document=?1",
            [policy.to_string()],
        )
        .unwrap();
        grant(&h, "fleet", &["read", "reflect"]).await;
        for (id, body) in cards {
            let put = h.call(ADMIN_TOKEN, json!({"action":"import","scope":"fleet","card_id":id,"text":body,"client_event_id":h.id()})).await;
            assert_eq!(put.status, 201, "{put:?}");
        }
        let db = Connection::open(h.memory()).unwrap();
        Self { h, db, hub }
    }
    /// Model a head written by an earlier gated automatic apply.
    fn auto_authored(&self, id: &str) {
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
    async fn evidence(&self, id: &str, refs: &[&str]) -> Reply {
        let current = card(&self.h, "fleet", id).await;
        // Evidence is appended through the card's first anchored block.
        let pin = proposal_blocks::pins(current["text"].as_str().unwrap()).remove(0);
        let op = json!({"type":"append_evidence","scope":"fleet","card_id":id,
            "base_revision":current["revision"],"block_id":pin["block_id"],
            "expected_block_hash":pin["hash"],"evidence_ids":refs});
        submit(&self.h, ADMIN_TOKEN, vec![op]).await
    }
    async fn anchor(&self, id: &str) -> Reply {
        let op = json!({"type":"anchor_blocks","scope":"fleet","card_id":id,"base_revision":self.revision(id).await});
        submit(&self.h, ADMIN_TOKEN, vec![op]).await
    }
    fn body(&self, opened: &Reply) -> Json {
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
    fn hold(
        &self,
        body: &Json,
        applier: &'static str,
        dense: Option<&str>,
    ) -> Option<&'static str> {
        let mut authority =
            ops::Authority::with_credentials(&Tx(&self.hub), &digest(ADMIN_TOKEN)).unwrap();
        authority.retrieval = Some(retrieval_eval::Live {
            applier,
            indexed_seq: self.h.events(),
            embedder_id: dense.map(Into::into),
            weights: json!({"lexical":1.0,"dense":1.0}),
        });
        retrieval_eval::hold(&Tx(&self.db), body, &authority, self.h.now()).unwrap()
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
    assert_eq!(r.hold(&merge, "admin", None), Some(PROJECTION));
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
