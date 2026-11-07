use super::{tests::*, *};
use serde_json::json;

#[tokio::test]
async fn chains_resolve_unmatched_heads_and_keep_history_scoped_after_restart() {
    let mut h = Hub::new();
    for (id, body) in [
        ("old", "---\nsuperseded_by: middle\n---\nneedle obsolete"),
        (
            "middle",
            "---\nsuperseded_by: newest\n---\nneedle intermediate",
        ),
        ("newest", "current answer"),
    ] {
        assert_eq!(h.put(NODE_TOKEN, "fleet", id, None, body).await.status, 201);
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
            h.open(std::sync::Arc::new(scan::Unavailable), None);
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
    let h = Hub::new();
    let old = h
        .put(
            NODE_TOKEN,
            "fleet",
            "a-old",
            None,
            "---\nsuperseded_by: newest\n---\nneedle needle",
        )
        .await;
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
    let cards = [
        ("human", false, "needle"),
        ("timer", true, "---\naliases: [human]\n---\nneedle"),
    ]
    .map(|(id, automated, body)| index::Card {
        scope: "fleet".into(),
        card_id: id.into(),
        revision: 1,
        content_hash: id.into(),
        trust: "agent-unreviewed".into(),
        text: body.into(),
        automated,
    });
    let snap = index::Builder::from_cards(cards, 1).snapshot();
    let hits = snap.top(snap.score(&[true], &["needle".into()]), index::TOP);
    assert_eq!(ranking::collapse(&snap, &[true], hits).hits.len(), 2);
}
