use super::{tests::*, *};
use serde_json::json;

#[tokio::test]
async fn chains_resolve_unmatched_heads_and_keep_history_scoped_after_restart() {
    let mut h = Hub::with(proposal_tests::clean(), None);
    for (id, body) in [
        ("old", "---\nsuperseded_by: middle\n---\nneedle obsolete"),
        (
            "middle",
            "---\nsuperseded_by: newest\n---\nneedle intermediate",
        ),
        ("newest", "current answer"),
    ] {
        assert_eq!(h.call(ADMIN_TOKEN, json!({"action":"import","scope":"fleet","card_id":id,"text":body,"client_event_id":h.id()})).await.status, 201);
    }
    h.put(
        ADMIN_TOKEN,
        "principal:secret",
        "old",
        None,
        "needle hidden",
    )
    .await;
    for restart in [false, true] {
        if restart {
            drop(h.store.take());
            h.open(proposal_tests::clean(), None);
        }
        let r = h
            .call(
                NODE_TOKEN,
                json!({"action":"search","query":"needle","limit":1,"min_seq":h.events()}),
            )
            .await;
        assert_eq!(r.status, 200);
        assert_eq!(r.body["matches"][0]["card_id"], "newest");
        let chain = r.body["matches"][0]["lineage"].as_array().unwrap();
        assert_eq!(chain.len(), 3);
        assert!(chain.iter().all(|v| v["scope"] == "fleet"));
        let raw = h
            .call(
                NODE_TOKEN,
                json!({"action":"search","query":"needle","ranking":"raw","min_seq":h.events()}),
            )
            .await;
        assert_eq!(raw.body["matches"].as_array().unwrap().len(), 2);
    }
}

#[tokio::test]
async fn automation_demotes_before_limit_with_opt_out_and_credential_cap() {
    let h = Hub::with(scan::bundled(), None);
    h.put(
        NODE_TOKEN,
        "fleet",
        "z-human",
        None,
        "needle extra words less relevant",
    )
    .await;
    h.call(ADMIN_TOKEN, json!({"action":"grant","principal":"agent:dot","scope":"fleet","rights":["read","write"],"client_event_id":h.id()})).await;
    for (id, token, origin) in [
        ("a-timer", NODE_TOKEN, "scheduled"),
        ("b-worker", NODE_TOKEN, "worker"),
        ("c-principal", AGENT_TOKEN, "interactive"),
    ] {
        let r = h.call(token, json!({"action":"put","scope":"fleet","card_id":id,"text":"needle needle", "origin":origin,"client_event_id":h.id()})).await;
        assert_eq!(r.status, 201, "{r:?}");
    }
    let query = |extra: Json| {
        let mut q = json!({"action":"search","query":"needle","min_seq":h.events()});
        q.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        q
    };
    let r = h.call(NODE_TOKEN, query(json!({"limit":1}))).await;
    assert_eq!(r.body["matches"][0]["card_id"], "z-human");
    let all = h.call(NODE_TOKEN, query(json!({}))).await;
    assert_eq!(all.body["matches"].as_array().unwrap().len(), 4);
    assert!(
        all.body["matches"].as_array().unwrap()[1..]
            .iter()
            .all(|v| v["source_demoted"] == true)
    );
    let automation = h
        .call(NODE_TOKEN, query(json!({"sources":["automation"]})))
        .await;
    assert_ne!(automation.body["matches"][0]["card_id"], "z-human");
    let base = h
        .call(
            NODE_TOKEN,
            json!({"action":"card","scope":"fleet","card_id":"a-timer"}),
        )
        .await;
    let imported = h.call(ADMIN_TOKEN, json!({"action":"import","scope":"fleet","card_id":"a-timer", "base_revision":base.body["card"]["revision"], "base_epoch":h.epoch().await, "text":"needle needle", "origin":"scheduled","client_event_id":h.id()})).await;
    assert_eq!(imported.status, 201);
    let reviewed = h.call(NODE_TOKEN, query(json!({}))).await;
    let first = &reviewed.body["matches"][0];
    assert_eq!(first["card_id"], "a-timer");
    assert_eq!(first["trust"], "imported");
    assert_eq!(first["source_demoted"], false);
}

#[test]
fn merge_aliases_cycles_missing_and_scope_boundaries_are_deterministic() {
    use index::{Builder, Card};
    let cards = [
        ("fleet", "a", 1, "---\nsuperseded_by: b\n---\nword"),
        ("fleet", "b", 2, "---\nsuperseded_by: a\n---\nword"),
        ("fleet", "merged", 3, "---\naliases: [duplicate]\n---\nword"),
        ("fleet", "duplicate", 1, "word"),
        (
            "fleet",
            "orphan",
            4,
            "---\nsuperseded_by: absent\n---\nword",
        ),
        ("principal:hidden", "absent", 5, "word"),
    ]
    .map(|(scope, id, revision, body)| Card {
        scope: scope.into(),
        card_id: id.into(),
        revision,
        text: body.into(),
        content_hash: id.into(),
        trust: "imported".into(),
        automated: false,
    });
    let snap = Builder::from_cards(cards, 5).snapshot();
    let allowed = [true, false];
    let hits = snap.top(snap.score(&allowed, &["word".into()]), index::TOP);
    let collapsed = ranking::collapse(&snap, &allowed, hits);
    let hits = collapsed.hits;
    let mut ids: Vec<_> = hits.iter().map(|(n, _)| &*snap.doc(*n).card_id).collect();
    ids.sort();
    assert_eq!(ids, ["b", "merged", "orphan"]);
}

#[test]
fn rrf_defaults_and_invalid_weights() {
    assert_eq!(
        ranking::Weights::default().json(),
        json!({"lexical":1.0,"dense":1.0})
    );
    assert_eq!(
        ranking::Weights::parse(Some(&json!({"dense":0.5})))
            .unwrap()
            .dense,
        0.5
    );
    for v in [
        json!({"dense":0}),
        json!({"lexical":-1}),
        json!({"dense":101}),
        json!({"dense":"1"}),
        json!({"typo":1}),
    ] {
        assert!(ranking::Weights::parse(Some(&v)).is_err());
    }
}

#[tokio::test]
async fn hybrid_weights_change_order_and_collapse_after_fusion_without_an_endpoint() {
    let h = Hub::with(proposal_tests::clean(), None);
    let old = h.call(ADMIN_TOKEN, json!({"action":"import","scope":"fleet","card_id":"a-old", "text":"---\nsuperseded_by: newest\n---\nneedle needle", "client_event_id":h.id()})).await;
    h.put(
        NODE_TOKEN,
        "fleet",
        "newest",
        None,
        "different current answer",
    )
    .await;
    let rival = h
        .put(NODE_TOKEN, "fleet", "rival", None, "needle extra words")
        .await;
    let matrix = dense::Matrix {
        dimensions: 2,
        segments: vec![Arc::new(dense::Segment {
            hashes: vec![
                old.body["event"]["content_hash"].as_str().unwrap().into(),
                rival.body["event"]["content_hash"].as_str().unwrap().into(),
            ],
            matrix: vec![0.0, 1.0, 1.0, 0.0],
        })],
        rows: 2,
        ..Default::default()
    };
    let mut request = search::parse(
        json!({"action":"search", "query":"needle"})
            .as_object()
            .unwrap(),
    )
    .unwrap();
    request.dense = Some((Arc::new(matrix), vec![1.0, 0.0]));
    let snap = h
        .store
        .as_ref()
        .unwrap()
        .index
        .wait(rev(&rival), MIN_SEQ_WAIT)
        .await;
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    for (lexical, dense, first) in [(10.0, 1.0, "newest"), (1.0, 10.0, "rival")] {
        request.weights = ranking::Weights { lexical, dense };
        let reply = search::run(
            &Conn(&db),
            &Credential::Admin,
            &request,
            &snap,
            &scan::Unavailable,
        )
        .unwrap();
        assert_eq!(reply.body["retrieval"]["mode"], "hybrid");
        assert_eq!(reply.body["matches"][0]["card_id"], first);
        assert_eq!(reply.body["matches"].as_array().unwrap().len(), 2);
        let head = reply.body["matches"]
            .as_array()
            .unwrap()
            .iter()
            .find(|hit| hit["card_id"] == "newest")
            .unwrap();
        assert_eq!(head["lineage"].as_array().unwrap().len(), 2);
        assert_eq!(head["ranks"]["lexical"], 1);
        assert_eq!(head["ranks"]["dense"], 2);
    }
}

#[test]
fn unreviewed_automation_cannot_hide_interactive_cards_through_aliases() {
    for automated in [false, true] {
        for link in ["supersedes: human", "aliases: [human]"] {
            let body = format!("---\n{link}\n---\nneedle");
            let cards = [
                ("human", false, "needle"),
                ("timer", automated, body.as_str()),
            ]
            .map(|(id, automated, body)| index::Card {
                scope: "fleet".into(),
                card_id: id.into(),
                revision: 1,
                content_hash: id.into(),
                trust: if id == "human" {
                    "reviewed"
                } else {
                    "agent-unreviewed"
                }
                .into(),
                text: body.into(),
                automated,
            });
            let snap = index::Builder::from_cards(cards, 1).snapshot();
            let hits = snap.top(snap.score(&[true], &["needle".into()]), index::TOP);
            assert_eq!(ranking::collapse(&snap, &[true], hits).hits.len(), 2);
        }
    }
}

async fn apply_relation(h: &Hub, kind: &str, a: &str, ar: i64, b: &str, br: i64) {
    let proposal = proposal_tests::submit(h, ADMIN_TOKEN, vec![json!({"type":kind,"scope":"fleet","card_id":a,"base_revision":ar,"block_id":"c-abcdef","expected_block_hash":ids::sha256_hex(b"needle claim. ^c-abcdef"),"other_card":{"scope":"fleet","card_id":b,"base_revision":br}})]).await;
    assert_eq!(proposal.status, 201);
    let applied = proposal_tests::decide(h, ADMIN_TOKEN, &proposal.body["proposal_id"]).await;
    assert_eq!(applied.body["state"], "applied");
}

#[tokio::test]
async fn proposal_generated_scoped_links_collapse_after_decide_and_push_falls_back() {
    for kind in ["supersede_card", "merge_exact_duplicates"] {
        let h = Hub::with(proposal_tests::clean(), None);
        let a = h
            .put(ADMIN_TOKEN, "fleet", "x", None, "needle claim. ^c-abcdef")
            .await;
        let b = h
            .put(ADMIN_TOKEN, "fleet", "y", None, "needle claim. ^c-abcdef")
            .await;
        apply_relation(&h, kind, "x", rev(&a), "y", rev(&b)).await;
        if kind == "merge_exact_duplicates" {
            // Merge retires Y. First verify the real post-decision search,
            // then revive Y to prove the stored scoped alias resolves it.
            let merged = h
                .call(
                    ADMIN_TOKEN,
                    json!({"action":"search","query":"needle","min_seq":h.events()}),
                )
                .await;
            assert_eq!(merged.body["matches"].as_array().unwrap().len(), 1);
            let retired = proposal_tests::card(&h, "fleet", "y").await;
            assert_eq!(retired["state"], "retired");
            let revived = h
                .put(
                    ADMIN_TOKEN,
                    "fleet",
                    "y",
                    retired["revision"].as_i64(),
                    "needle claim. ^c-abcdef",
                )
                .await;
            assert_eq!(revived.status, 201);
        }
        let q = json!({"action":"search","query":"needle","min_seq":h.events()});
        let r = h.call(ADMIN_TOKEN, q.clone()).await;
        assert_eq!(r.body["matches"].as_array().unwrap().len(), 1);
        assert_eq!(
            r.body["matches"][0]["card_id"],
            if kind == "supersede_card" { "y" } else { "x" }
        );
        assert_eq!(
            r.body["matches"][0]["lineage"].as_array().unwrap().len(),
            2,
            "{kind}: chain {:?}",
            r.body["matches"][0]["lineage"]
        );
        // X was reviewed by decide, Y remains unreviewed. Neither its id nor
        // its text may displace or appear in X's push result.
        let pushed = h
            .store
            .as_ref()
            .unwrap()
            .search_for(&Credential::Admin, q.as_object().unwrap(), true)
            .await;
        assert_eq!(pushed.body["matches"].as_array().unwrap().len(), 1);
        assert_eq!(pushed.body["matches"][0]["card_id"], "x");
        assert_eq!(
            pushed.body["matches"][0]["lineage"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(pushed.body["matches"][0]["lineage"][0]["card_id"], "x");
    }
}

#[test]
fn scope_trust_terminal_heads_recency_and_snapshot_replay() {
    use index::{Builder, Card, Change};
    let card = |id: &str, seq, trust: &str, body: &str| Card {
        scope: "repo:example/app".into(),
        card_id: id.into(),
        revision: seq,
        content_hash: format!("h{seq}-{id}"),
        trust: trust.into(),
        text: body.into(),
        automated: false,
    };
    let mut b = Builder::from_cards(
        [
            card("victim", 1, "reviewed", "needle"),
            card(
                "spoof",
                9,
                "agent-unreviewed",
                "---\nsupersedes: victim\naliases: [victim]\n---\nneedle",
            ),
            card(
                "a",
                2,
                "reviewed",
                "---\nsupersedes: repo:example/app/victim\n---\nneedle",
            ),
            card(
                "b",
                3,
                "policy-promoted",
                "---\naliases: [repo:example/app/victim]\n---\nneedle",
            ),
            card(
                "foreign",
                4,
                "reviewed",
                "---\nsuperseded_by: repo:other/app/a\n---\nneedle",
            ),
            card("flagged", 5, "flagged", "needle"),
            card(
                "safe",
                6,
                "reviewed",
                "---\nsuperseded_by: repo:example/app/flagged\n---\nneedle",
            ),
        ],
        9,
    );
    let old = b.snapshot();
    let search = |s: &index::Snapshot| {
        let hits = s.top(s.score(&[true], &["needle".into()]), index::TOP);
        let r = ranking::collapse(s, &[true], hits);
        let mut ids: Vec<_> = r
            .hits
            .iter()
            .map(|(n, _)| s.doc(*n).card_id.to_string())
            .collect();
        ids.sort();
        ids
    };
    let flagged = old
        .live(&[true])
        .into_iter()
        .find(|n| &*old.doc(*n).card_id == "flagged")
        .unwrap();
    assert!(
        !ranking::collapse(&old, &[true], vec![(flagged, 1.0)])
            .chains
            .contains_key(&flagged)
    );
    assert_eq!(
        search(&old),
        ["a", "b", "flagged", "foreign", "safe", "spoof"]
    );
    b.apply(
        vec![Change::Gone {
            scope: "repo:example/app".into(),
            card_id: "b".into(),
        }],
        10,
    );
    assert!(!search(&b.snapshot()).contains(&"b".into()));
    assert!(search(&old).contains(&"b".into()));
    b.apply(
        vec![
            Change::Live(card(
                "a",
                11,
                "reviewed",
                "---\nsuperseded_by: repo:example/app/victim\n---\nneedle",
            )),
            Change::Live(card(
                "victim",
                12,
                "reviewed",
                "---\nsuperseded_by: repo:example/app/a\n---\nneedle",
            )),
        ],
        12,
    );
    let snap = b.snapshot();
    let hit = snap
        .live(&[true])
        .into_iter()
        .find(|n| &*snap.doc(*n).card_id == "a")
        .unwrap();
    let cycle = ranking::collapse(&snap, &[true], vec![(hit, 1.0)]);
    assert_eq!(&*snap.doc(cycle.hits[0].0).card_id, "victim");
    assert_eq!(snap.doc(cycle.hits[0].0).head_seq, 12);
}

#[test]
fn collapse_reads_only_hit_components_and_empty_queries_read_nothing() {
    let cards = (0..5000).map(|n| index::Card {
        scope: "fleet".into(),
        card_id: format!("card-{n}"),
        revision: n + 1,
        content_hash: format!("h{n}"),
        trust: "reviewed".into(),
        text: "unrelated".into(),
        automated: false,
    });
    let snap = index::Builder::from_cards(cards, 5000).snapshot();
    let mut calls = 0;
    let r = ranking::collapse_eligible(&snap, &[true], vec![], |_| {
        calls += 1;
        Ok(true)
    })
    .unwrap();
    assert_eq!((r.visited, calls), (0, 0));
    let r = ranking::collapse_eligible(&snap, &[true], vec![(0, 1.0)], |_| {
        calls += 1;
        Ok(true)
    })
    .unwrap();
    assert_eq!((r.visited, calls), (1, 1));
}

#[tokio::test]
async fn policy_and_corpus_bindings_are_opt_in_and_cover_the_whole_scope() {
    let h = Hub::with(proposal_tests::clean(), None);
    h.put(ADMIN_TOKEN, "fleet", "a", None, "needle").await;
    let normal = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"search","query":"needle","min_seq":h.events()}),
        )
        .await;
    assert!(normal.body.get("policy_revision").is_none());
    assert!(normal.body.get("corpus_identity").is_none());
    let q = json!({"action":"search","query":"needle","eval":true,"min_seq":h.events(),"scopes":["fleet"]});
    let eval = h.call(ADMIN_TOKEN, q).await;
    assert!(eval.body.get("policy_revision").is_some());
    assert_eq!(eval.body["corpus_identity"]["fleet"]["live_count"], 1);
    let before = eval.body["corpus_identity"]["fleet"]["digest"].clone();
    h.put(ADMIN_TOKEN, "fleet", "extra", None, "other").await;
    let after=h.call(ADMIN_TOKEN,json!({"action":"search","query":"needle","eval":true,"min_seq":h.events(),"scopes":["fleet"]})).await;
    assert_eq!(after.body["corpus_identity"]["fleet"]["live_count"], 2);
    assert_ne!(after.body["corpus_identity"]["fleet"]["digest"], before);
    assert_eq!(after.body["lineage_visited"], 1);
}

#[test]
fn separate_terminal_heads_keep_their_own_matched_member_scores() {
    let cards = [
        ("root", 1, "needle"),
        ("a", 2, "---\nsupersedes: root\n---\nneedle"),
        ("b", 3, "---\nsupersedes: root\n---\nneedle"),
    ]
    .map(|(id, revision, text)| index::Card {
        scope: "fleet".into(),
        card_id: id.into(),
        revision,
        text: text.into(),
        trust: "reviewed".into(),
        automated: false,
        content_hash: id.into(),
    });
    let snap = index::Builder::from_cards(cards, 3).snapshot();
    let r = ranking::collapse(&snap, &[true], vec![(1, 2.0), (2, 1.0)]);
    assert_eq!(r.hits, [(1, 2.0), (2, 1.0)]);
    assert_eq!(r.sources[&1], 1);
    assert_eq!(r.sources[&2], 2);
    assert_eq!(r.chains[&1].len(), 2);
    assert_eq!(r.chains[&2].len(), 2);
    let only_a = ranking::collapse(&snap, &[true], vec![(1, 2.0)]);
    assert_eq!(only_a.hits, [(1, 2.0)]);
}
