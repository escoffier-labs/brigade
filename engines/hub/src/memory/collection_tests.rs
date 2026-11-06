//! `collection:<name>` scopes (#2329): an imported note collection that no
//! node token reads or writes by default, that no wildcard grant matches, and
//! that opens only to a grant naming it.
use super::Reply;
use super::auth::RIGHTS;
use super::embedding::LEASE_MS;
use super::embedding_tests::{encoded, setup};
use super::scan::{self, Scan};
use super::tests::{ADMIN_TOKEN, AGENT_TOKEN, Fixed, Hub, NODE_TOKEN, code};
use serde_json::{Value as Json, json};
use std::sync::Arc;

const VAULT: &str = "collection:vault/cerebro";

fn clean_hub() -> Hub {
    Hub::with(
        Arc::new(Fixed(Scan::Clean {
            ruleset: "r1".into(),
        })),
        None,
    )
}
async fn grant(h: &Hub, principal: &str, scope: &str, rights: Json) -> Reply {
    h.call(
        ADMIN_TOKEN,
        json!({"action":"grant","principal":principal,"scope":scope,"rights":rights,
               "client_event_id":h.id()}),
    )
    .await
}
async fn import(h: &Hub, token: &str, scope: &str, card: &str, text: &str) -> Reply {
    h.call(
        token,
        json!({"action":"import","scope":scope,"card_id":card,"base_revision":null,
               "text":text,"client_event_id":h.id()}),
    )
    .await
}
async fn card(h: &Hub, token: &str, scope: Option<&str>, card: &str) -> Reply {
    let mut body = json!({"action":"card","card_id":card});
    if let Some(scope) = scope {
        body["scope"] = json!(scope);
    }
    h.call(token, body).await
}
/// `(scope, card_id)` for every match, after the index has caught up.
async fn search(h: &Hub, token: &str, scopes: Option<Json>) -> (u16, Vec<(String, String)>) {
    let seq = h.events();
    let mut body = json!({"action":"search","query":"rotation","min_seq":seq});
    if let Some(scopes) = scopes {
        body["scopes"] = scopes;
    }
    let reply = h.call(token, body).await;
    let found = reply.body["matches"]
        .as_array()
        .map(|m| {
            m.iter()
                .map(|m| {
                    (
                        m["scope"].as_str().unwrap_or("").to_string(),
                        m["card_id"].as_str().unwrap_or("").to_string(),
                    )
                })
                .collect()
        })
        .unwrap_or_default();
    (reply.status, found)
}
async fn log_scopes(h: &Hub, token: &str) -> Vec<String> {
    let reply = h
        .call(token, json!({"action":"log","after_seq":0,"limit":500}))
        .await;
    reply.body["events"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|e| e["scope"].as_str().map(String::from))
        .collect()
}
async fn readable(h: &Hub, token: &str) -> Json {
    h.call(
        token,
        json!({"action":"status","scopes":[VAULT, "fleet", "repo:example/one"]}),
    )
    .await
    .body["readable_scopes"]
        .clone()
}
fn refused(reply: &Reply) -> (u16, &str) {
    (reply.status, code(reply))
}
const DENIED: (u16, &str) = (403, "memory_grant_missing");

/// Seed one collection card and one fleet card, both imported by the admin.
async fn seeded() -> Hub {
    let h = clean_hub();
    let vault = import(&h, ADMIN_TOKEN, VAULT, "vault-note", "vault rotation notes").await;
    assert_eq!(vault.status, 201, "{vault:?}");
    assert_eq!(vault.body["event"]["trust"], "imported");
    let fleet = import(
        &h,
        ADMIN_TOKEN,
        "fleet",
        "fleet-note",
        "fleet rotation notes",
    )
    .await;
    assert_eq!(fleet.status, 201, "{fleet:?}");
    h
}

#[tokio::test]
async fn a_node_token_has_no_default_access_to_a_collection() {
    let h = seeded().await;
    let before = h.events();
    let put = h.put(NODE_TOKEN, VAULT, "added", None, "x").await;
    assert_eq!(refused(&put), DENIED);
    let imported = import(&h, NODE_TOKEN, VAULT, "added", "x").await;
    assert_eq!(refused(&imported), DENIED);
    assert_eq!(h.events(), before);
    assert_eq!(
        refused(&card(&h, NODE_TOKEN, Some(VAULT), "vault-note").await),
        DENIED
    );
    // Without a scope the collection card is not found, not ambiguous.
    assert_eq!(
        refused(&card(&h, NODE_TOKEN, None, "vault-note").await),
        (404, "memory_card_not_found")
    );
    assert_eq!(
        readable(&h, NODE_TOKEN).await,
        json!(["fleet", "repo:example/one"])
    );
    let (status, found) = search(&h, NODE_TOKEN, None).await;
    assert_eq!(status, 200);
    assert_eq!(found, vec![("fleet".into(), "fleet-note".into())]);
    assert_eq!(search(&h, NODE_TOKEN, Some(json!([VAULT]))).await.0, 403);
    let scopes = log_scopes(&h, NODE_TOKEN).await;
    assert!(
        !scopes.is_empty() && !scopes.iter().any(|s| s == VAULT),
        "{scopes:?}"
    );
    // The node's repository and fleet defaults are unchanged.
    assert_eq!(
        h.put(NODE_TOKEN, "repo:example/one", "kept", None, "x")
            .await
            .status,
        201
    );
}

#[tokio::test]
async fn wildcard_grants_never_match_a_collection() {
    let h = seeded().await;
    for principal in ["node-1", "agent:dot"] {
        for pattern in ["*", "repo:*"] {
            let g = grant(&h, principal, pattern, json!(RIGHTS)).await;
            assert_eq!(g.status, 200, "{g:?}");
        }
    }
    for token in [NODE_TOKEN, AGENT_TOKEN] {
        let before = h.events();
        assert_eq!(
            refused(&h.put(token, VAULT, "added", None, "x").await),
            DENIED
        );
        assert_eq!(
            refused(&import(&h, token, VAULT, "added", "x").await),
            DENIED
        );
        assert_eq!(h.events(), before);
        assert_eq!(
            refused(&card(&h, token, Some(VAULT), "vault-note").await),
            DENIED
        );
        assert_eq!(
            readable(&h, token).await,
            json!(["fleet", "repo:example/one"])
        );
        let (status, found) = search(&h, token, None).await;
        assert_eq!(status, 200);
        assert!(found.iter().all(|(s, _)| s != VAULT), "{found:?}");
        assert_eq!(search(&h, token, Some(json!([VAULT]))).await.0, 403);
        assert!(!log_scopes(&h, token).await.iter().any(|s| s == VAULT));
    }
    // The same wildcard grant does open a repository scope.
    assert_eq!(
        import(&h, AGENT_TOKEN, "repo:example/one", "wild", "x")
            .await
            .status,
        201
    );
}

#[tokio::test]
async fn an_explicit_grant_opens_read_import_and_put() {
    let h = seeded().await;
    assert_eq!(
        grant(&h, "node-1", VAULT, json!(["read", "import"]))
            .await
            .status,
        200
    );
    let named = card(&h, NODE_TOKEN, Some(VAULT), "vault-note").await;
    assert_eq!(named.status, 200, "{named:?}");
    assert_eq!(named.body["card"]["text"], "vault rotation notes\n");
    let found = card(&h, NODE_TOKEN, None, "vault-note").await;
    assert_eq!(found.body["card"]["scope"], VAULT);
    assert_eq!(
        readable(&h, NODE_TOKEN).await,
        json!([VAULT, "fleet", "repo:example/one"])
    );
    let (_, all) = search(&h, NODE_TOKEN, None).await;
    assert!(
        all.contains(&(VAULT.into(), "vault-note".into())),
        "{all:?}"
    );
    let (status, only) = search(&h, NODE_TOKEN, Some(json!([VAULT]))).await;
    assert_eq!(
        (status, only),
        (200, vec![(VAULT.into(), "vault-note".into())])
    );
    assert!(log_scopes(&h, NODE_TOKEN).await.iter().any(|s| s == VAULT));
    let added = import(&h, NODE_TOKEN, VAULT, "added", "added rotation").await;
    assert_eq!(added.status, 201, "{added:?}");
    assert_eq!(added.body["event"]["trust"], "imported");
    // Read and import do not imply write.
    assert_eq!(
        refused(&h.put(NODE_TOKEN, VAULT, "put", None, "x").await),
        DENIED
    );
    assert_eq!(
        grant(&h, "node-1", VAULT, json!(["read", "import", "write"]))
            .await
            .status,
        200
    );
    let put = h.put(NODE_TOKEN, VAULT, "put", None, "x").await;
    assert_eq!(put.status, 201, "{put:?}");
    assert_eq!(put.body["event"]["trust"], "agent-unreviewed");
    // The grant names one collection only.
    let other = "collection:other";
    assert_eq!(
        refused(&import(&h, NODE_TOKEN, other, "x", "x").await),
        DENIED
    );
    assert_eq!(
        refused(&card(&h, NODE_TOKEN, Some(other), "x").await),
        DENIED
    );
    // A message principal opens the same way, with only the rights it holds.
    assert_eq!(
        refused(&card(&h, AGENT_TOKEN, Some(VAULT), "vault-note").await),
        DENIED
    );
    assert_eq!(
        grant(&h, "agent:dot", VAULT, json!(["read"])).await.status,
        200
    );
    assert_eq!(
        card(&h, AGENT_TOKEN, Some(VAULT), "vault-note")
            .await
            .status,
        200
    );
    assert_eq!(
        refused(&import(&h, AGENT_TOKEN, VAULT, "y", "y").await),
        DENIED
    );
    // Revoking the rights closes it again at once.
    assert_eq!(grant(&h, "node-1", VAULT, json!([])).await.status, 200);
    assert_eq!(
        refused(&card(&h, NODE_TOKEN, Some(VAULT), "vault-note").await),
        DENIED
    );
}

#[tokio::test]
async fn the_admin_needs_no_grant_and_grants_name_one_collection() {
    let h = seeded().await;
    let put = h.put(ADMIN_TOKEN, VAULT, "admin-put", None, "x").await;
    assert_eq!(put.status, 201, "{put:?}");
    assert_eq!(
        card(&h, ADMIN_TOKEN, Some(VAULT), "vault-note")
            .await
            .status,
        200
    );
    let (status, found) = search(&h, ADMIN_TOKEN, Some(json!([VAULT]))).await;
    assert_eq!(
        (status, found),
        (200, vec![(VAULT.into(), "vault-note".into())])
    );
    assert!(log_scopes(&h, ADMIN_TOKEN).await.iter().any(|s| s == VAULT));
    for bad in ["collection:*", "collection:", "collection"] {
        let r = grant(&h, "node-1", bad, json!(["read"])).await;
        assert_eq!(r.status, 400, "{bad}: {r:?}");
    }
    let named = grant(&h, "node-1", VAULT, json!(["read"])).await;
    assert_eq!(named.body["grant"]["scope"], VAULT, "{named:?}");
}

/// One embed lease once every earlier lease has expired, so each call sees
/// every job that is still waiting. Returns the reply and its sorted hashes.
async fn lease(h: &Hub, token: &str, embedder: &str) -> (Reply, Vec<String>) {
    h.set_clock(h.now() + LEASE_MS + 1);
    let reply = h
        .call(
            token,
            json!({"action":"embed_lease","embedder_id":embedder,"max":4,"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(reply.status, 200, "{reply:?}");
    let mut hashes: Vec<String> = reply.body["jobs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|j| j["content_hash"].as_str().unwrap().to_string())
        .collect();
    hashes.sort();
    (reply, hashes)
}

#[tokio::test]
async fn embed_leases_skip_a_collection_until_a_grant_names_it() {
    let mut h = Hub::with(scan::bundled(), None);
    let embedder = setup(&mut h);
    let vault_text = "vault rotation body";
    let mut hashes = Vec::new();
    for (scope, card, text) in [
        (VAULT, "vault-note", vault_text),
        ("repo:example/one", "repo-note", "repo rotation body"),
    ] {
        let put = h.put(ADMIN_TOKEN, scope, card, None, text).await;
        assert_eq!(put.status, 201, "{put:?}");
        hashes.push(
            put.body["event"]["content_hash"]
                .as_str()
                .unwrap()
                .to_string(),
        );
    }
    let (vault, repo) = (hashes[0].clone(), hashes[1].clone());
    // `*` and `repo:*` with `embed` lease the repository card, never the
    // collection's, for a node and a message principal alike.
    for principal in ["node-1", "agent:dot"] {
        for pattern in ["*", "repo:*"] {
            let g = grant(&h, principal, pattern, json!(["embed"])).await;
            assert_eq!(g.status, 200, "{g:?}");
        }
    }
    for token in [NODE_TOKEN, AGENT_TOKEN] {
        let (reply, leased) = lease(&h, token, &embedder).await;
        assert_eq!(leased, vec![repo.clone()], "{token}");
        assert!(!reply.body.to_string().contains(vault_text), "{token}");
    }
    // An embed grant naming the collection opens it to that principal only.
    let named = grant(&h, "node-1", VAULT, json!(["embed"])).await;
    assert_eq!(named.status, 200, "{named:?}");
    let (_, agent) = lease(&h, AGENT_TOKEN, &embedder).await;
    assert_eq!(agent, vec![repo.clone()]);
    let (reply, leased) = lease(&h, NODE_TOKEN, &embedder).await;
    let mut both = vec![vault.clone(), repo];
    both.sort();
    assert_eq!(leased, both);
    // The lease holder completes the collection card's vector.
    let vectors: Vec<_> = leased
        .iter()
        .map(|hash| json!({"content_hash": hash, "vector": encoded(&[1.0, 0.0])}))
        .collect();
    let submit = h
        .call(
            NODE_TOKEN,
            json!({"action":"embed_submit","lease_id":reply.body["lease_id"],
                   "model_digest":"fake-digest","vectors":vectors,"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(submit.status, 200, "{submit:?}");
    let stored: i64 = rusqlite::Connection::open(h.memory())
        .unwrap()
        .query_row(
            "SELECT count(*) FROM memory_vectors WHERE content_hash=?1",
            [&vault],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(stored, 1);
}
