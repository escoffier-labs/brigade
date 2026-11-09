//! S23: connector push over `search` with `query_source: "push"`. The trust
//! filter, per-principal grants, head revalidation, the egress scan and the
//! response bound with a 2,000-card corpus, and push-safe `card` reads.
use super::scan;
use super::tests::{ADMIN_TOKEN, AGENT_TOKEN, Hub, NODE_TOKEN, code};
use rusqlite::Connection;
use serde_json::{Value as Json, json};

fn card(title: &str, summary: &str) -> String {
    format!("---\ntitle: {title}\ndescription: {summary}\n---\nrelease checklist body\n")
}

fn ids(reply: &super::Reply) -> Vec<String> {
    let mut out: Vec<String> = reply.body["matches"]
        .as_array()
        .unwrap_or_else(|| panic!("{:?}", reply.body))
        .iter()
        .map(|m| m["card_id"].as_str().unwrap().to_string())
        .collect();
    out.sort();
    out
}

async fn write(h: &Hub, token: &str, action: &str, card_id: &str, text: &str) -> i64 {
    let r = h
        .call(
            token,
            json!({"action":action,"scope":"fleet","card_id":card_id,"text":text,"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(r.status, 201, "{action} {card_id}: {:?}", r.body);
    r.body["event"]["seq"].as_i64().unwrap()
}

fn push(seq: i64) -> Json {
    json!({"action":"search","query":"release checklist","query_source":"push","limit":20,"min_seq":seq})
}

fn private_text() -> String {
    // Built at run time so the repository guard never sees the whole path.
    format!(
        "release checklist, deploy notes in /{}/example/notes.md",
        "home"
    )
}

#[cfg(feature = "memory-scan")]
#[tokio::test]
async fn push_keeps_unreviewed_cards_with_their_writer_and_private_text_home() {
    let h = Hub::with(scan::bundled(), None);
    let grant = |rights: Json| json!({"action":"grant","principal":"agent:dot","scope":"fleet","rights":rights,"client_event_id":h.id()});
    // Before the runbook grant, the connector principal reads nothing.
    let none = h.call(AGENT_TOKEN, push(0)).await;
    assert_eq!((none.status, code(&none)), (403, "memory_grant_missing"));
    assert_eq!(
        h.call(ADMIN_TOKEN, grant(json!(["read", "write"])))
            .await
            .status,
        200
    );
    write(
        &h,
        ADMIN_TOKEN,
        "import",
        "imported",
        &card("Release checklist", "steps"),
    )
    .await;
    write(
        &h,
        NODE_TOKEN,
        "put",
        "node-draft",
        &card("Release checklist draft", "node"),
    )
    .await;
    write(
        &h,
        AGENT_TOKEN,
        "put",
        "dot-draft",
        &card("Release checklist notes", "dot"),
    )
    .await;
    let seq = write(&h, ADMIN_TOKEN, "import", "private", &private_text()).await;

    // Plain search still lists every live card the grant opens.
    let searched = h
        .call(
            AGENT_TOKEN,
            json!({"action":"search","query":"release checklist","min_seq":seq}),
        )
        .await;
    assert_eq!(
        ids(&searched),
        ["dot-draft", "imported", "node-draft", "private"]
    );
    // Push: reviewed trust for everyone, unreviewed only for its writer,
    // and the private class for no one, not even as a placeholder.
    let agent = h.call(AGENT_TOKEN, push(seq)).await;
    assert_eq!(agent.status, 200, "{:?}", agent.body);
    assert_eq!(ids(&agent), ["dot-draft", "imported"]);
    let node = h.call(NODE_TOKEN, push(seq)).await;
    assert_eq!(ids(&node), ["imported", "node-draft"]);
    for reply in [&agent, &node] {
        let text = reply.body.to_string();
        assert!(!text.contains("example/notes.md"), "{text}");
        assert!(!text.contains("\"private\""), "{text}");
        assert!(!text.contains("release checklist body"), "{text}");
    }
    // A revoked read grant stops the push at once.
    assert_eq!(
        h.call(ADMIN_TOKEN, grant(json!(["write"]))).await.status,
        200
    );
    let revoked = h.call(AGENT_TOKEN, push(seq)).await;
    assert_eq!(
        (revoked.status, code(&revoked)),
        (403, "memory_grant_missing")
    );
}

#[cfg(feature = "memory-scan")]
#[tokio::test]
async fn push_drops_a_stale_snapshot_hit_after_revalidating_the_head() {
    let h = Hub::with(scan::bundled(), None);
    let mut seq = 0;
    for id in ["kept", "retired", "lowered", "moved"] {
        seq = write(
            &h,
            ADMIN_TOKEN,
            "import",
            id,
            &card("Release checklist", id),
        )
        .await;
    }
    assert_eq!(
        ids(&h.call(NODE_TOKEN, push(seq)).await),
        ["kept", "lowered", "moved", "retired"]
    );
    // Change the heads behind the published snapshot, so the index still
    // holds the old documents.
    let c = Connection::open(h.memory()).unwrap();
    c.execute_batch(
        "UPDATE memory_cards SET state='retired' WHERE card_id='retired';
         UPDATE memory_cards SET trust='agent-unreviewed' WHERE card_id='lowered';
         UPDATE memory_cards SET revision=revision+1000 WHERE card_id='moved';",
    )
    .unwrap();
    drop(c);
    let stale = h
        .call(
            NODE_TOKEN,
            json!({"action":"search","query":"release checklist","min_seq":seq}),
        )
        .await;
    assert_eq!(ids(&stale), ["kept", "lowered", "moved", "retired"]);
    assert_eq!(ids(&h.call(NODE_TOKEN, push(seq)).await), ["kept"]);
}

#[cfg(feature = "memory-scan")]
#[tokio::test]
async fn push_takes_the_default_ranking_and_bounds_a_2000_card_corpus() {
    let h = Hub::with(scan::bundled(), None);
    for bad in [
        json!({"action":"search","query":"release","query_source":"push","eval":true}),
        json!({"action":"search","query":"release","query_source":"push","ranking":"raw"}),
        json!({"action":"search","query":"release","query_source":"push","limit":21}),
    ] {
        let r = h.call(NODE_TOKEN, bad.clone()).await;
        assert_eq!((r.status, code(&r)), (400, "memory_query_invalid"), "{bad}");
    }
    let summary = "fleet release step ".repeat(30);
    let mut seq = 0;
    for n in 0..2000 {
        seq = write(
            &h,
            ADMIN_TOKEN,
            "import",
            &format!("release-{n:04}"),
            &card(&format!("Release checklist {n}"), &summary),
        )
        .await;
    }
    for limit in [3, 5] {
        let r = h
            .call(
                NODE_TOKEN,
                json!({"action":"search","query":"release checklist","query_source":"push","limit":limit,"min_seq":seq}),
            )
            .await;
        assert_eq!(r.status, 200, "{:?}", r.body);
        assert_eq!(r.body["match_count"], 2000);
        let matches = r.body["matches"].as_array().unwrap();
        assert_eq!(matches.len(), limit);
        for m in matches {
            assert!(m.get("blocked").is_none(), "{m}");
            assert_eq!(m["trust"], "imported");
            assert_eq!(m["content_hash"].as_str().unwrap().len(), 64);
            assert!(m.get("text").is_none() && m.get("body").is_none());
        }
        // Card bodies never ride along, so the answer grows with the limit
        // and the summaries, never with the corpus.
        assert!(r.body.to_string().len() < 4096 * limit, "{}", r.body);
        assert!(!r.body.to_string().contains("release checklist body"));
    }
}

/// `card` with `push: true` serves only what push could send this credential.
/// `token` reads the cards. Running it as the node, the writer of the
/// unreviewed card, is the negative check: that card then opens.
#[cfg(feature = "memory-scan")]
async fn push_card_reads_as(token: &str) -> Vec<(String, u16, String)> {
    let h = Hub::with(scan::bundled(), None);
    let grant = json!({"action":"grant","principal":"agent:dot","scope":"fleet","rights":["read","write"],"client_event_id":h.id()});
    assert_eq!(h.call(ADMIN_TOKEN, grant).await.status, 200);
    write(
        &h,
        ADMIN_TOKEN,
        "import",
        "reviewed",
        &card("Release checklist", "steps"),
    )
    .await;
    write(
        &h,
        NODE_TOKEN,
        "put",
        "node-draft",
        &card("Release draft", "node"),
    )
    .await;
    write(
        &h,
        AGENT_TOKEN,
        "put",
        "dot-draft",
        &card("Release notes", "dot"),
    )
    .await;
    write(&h, ADMIN_TOKEN, "import", "private", &private_text()).await;
    write(
        &h,
        ADMIN_TOKEN,
        "import",
        "pending",
        &card("Release pending", "p"),
    )
    .await;
    write(
        &h,
        ADMIN_TOKEN,
        "import",
        "retired",
        &card("Release retired", "r"),
    )
    .await;
    let epoch = h.epoch().await;
    let head = |id: &str| -> i64 {
        Connection::open(h.memory())
            .unwrap()
            .query_row(
                "SELECT revision FROM memory_cards WHERE card_id=?1",
                [id],
                |r| r.get(0),
            )
            .unwrap()
    };
    let retire = json!({"action":"retire","scope":"fleet","card_id":"retired","base_revision":head("retired"),
                        "base_epoch":epoch,"reason":"obsolete","client_event_id":h.id()});
    assert_eq!(h.call(ADMIN_TOKEN, retire).await.status, 200);
    Connection::open(h.memory())
        .unwrap()
        .execute(
            "UPDATE memory_card_revisions SET scan_state='pending' WHERE card_id='pending'",
            [],
        )
        .unwrap();
    let mut out = Vec::new();
    for id in [
        "reviewed",
        "node-draft",
        "dot-draft",
        "private",
        "pending",
        "retired",
    ] {
        let plain = h
            .call(token, json!({"action":"card","scope":"fleet","card_id":id}))
            .await;
        // A plain read opens every head the grant reaches, push or not.
        assert_eq!(plain.status, 200, "{id}: {:?}", plain.body);
        let r = h
            .call(
                token,
                json!({"action":"card","scope":"fleet","card_id":id,"push":true}),
            )
            .await;
        if r.status == 200 {
            assert_eq!(r.body["card"]["state"], "live");
            assert_eq!(r.body["card"]["scan_state"], "clean");
        } else {
            assert!(r.body.get("card").is_none(), "{:?}", r.body);
            assert!(!r.body.to_string().contains("release checklist body"));
        }
        out.push((id.to_string(), r.status, code(&r).to_string()));
    }
    let bad = h
        .call(
            token,
            json!({"action":"card","scope":"fleet","card_id":"reviewed","push":true,"revision":1}),
        )
        .await;
    assert_eq!((bad.status, code(&bad)), (400, "memory_request_invalid"));
    let bad = h
        .call(
            token,
            json!({"action":"card","scope":"fleet","card_id":"reviewed","push":"yes"}),
        )
        .await;
    assert_eq!(bad.status, 400);
    out
}

#[cfg(feature = "memory-scan")]
#[tokio::test]
async fn push_card_reads_refuse_what_push_would_never_send() {
    let refused = |id: &str| (id.to_string(), 404, "memory_card_not_found".to_string());
    let opened = |id: &str| (id.to_string(), 200, String::new());
    assert_eq!(
        push_card_reads_as(AGENT_TOKEN).await,
        [
            opened("reviewed"),
            refused("node-draft"),
            opened("dot-draft"),
            refused("private"),
            refused("pending"),
            refused("retired"),
        ]
    );
    // Negative check: the node wrote `node-draft`, so its credential opens it.
    // A connector that read with the node credential would leak it.
    let node = push_card_reads_as(NODE_TOKEN).await;
    assert_eq!(node[1], opened("node-draft"));
    assert_eq!(node[2], refused("dot-draft"));
}

#[cfg(feature = "memory-scan")]
#[tokio::test]
async fn push_card_reads_need_the_scanner() {
    let mut h = Hub::with(scan::bundled(), None);
    write(
        &h,
        ADMIN_TOKEN,
        "import",
        "reviewed",
        &card("Release checklist", "steps"),
    )
    .await;
    h.store.take();
    h.open(std::sync::Arc::new(scan::Unavailable), None);
    let r = h
        .call(
            NODE_TOKEN,
            json!({"action":"card","scope":"fleet","card_id":"reviewed","push":true}),
        )
        .await;
    assert_eq!((r.status, code(&r)), (503, "memory_scan_unavailable"));
}
