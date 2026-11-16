//! Anchoring keeps original bytes and uses the existing proposal journal.
use super::{proposal_blocks::anchor_blocks, proposal_tests::*, tests::*, *};

#[test]
fn deterministic_suffixes_preserve_bytes_and_reserve_existing_ids() {
    let original = "---\nevidence: []\n---\nFirst claim.\n\n- Second claim. ^c-abcdef\n";
    let anchored = anchor_blocks(original, "a").unwrap();
    let digest = ids::sha256_hex(b"a\x000\x00First claim.\x000");
    let expected = original.replacen(
        "First claim.",
        &format!("First claim. ^c-{}", &digest[..6]),
        1,
    );
    assert_eq!(anchored, expected);
    assert_eq!(anchor_blocks(original, "a").unwrap(), anchored);
    assert_eq!(anchor_blocks(&anchored, "a").unwrap(), anchored);
    assert_eq!(
        super::proposal_blocks::strip_anchors(&anchored),
        original.replace(" ^c-abcdef", "")
    );
    assert!(anchored.ends_with("- Second claim. ^c-abcdef\n"));
    let first = super::proposal_blocks::pins(&anchored)[0]["block_id"]
        .as_str()
        .unwrap()
        .to_string();
    let colliding = format!("First claim.\n\nKeep. ^{first}\n");
    let collision_result = anchor_blocks(&colliding, "a").unwrap();
    assert_eq!(collision_result.matches(&format!("^{first}")).count(), 1);
    assert!(collision_result.ends_with(&format!("Keep. ^{first}\n")));
    let reserved = format!("---\nclaim_anchor_ids: [\"{first}\"]\n---\nFirst claim.\n");
    let result = anchor_blocks(&reserved, "a").unwrap();
    assert!(!result.contains(&format!(" ^{first}")));
    assert!(anchor_blocks("A. ^c-abcdef\n\nB. ^c-abcdef\n", "a").is_err());
}

#[tokio::test]
async fn anchoring_is_tier_zero_and_rollback_restores_exact_bytes() {
    let h = Hub::with(clean(), None);
    let original = "---\nevidence: []\n---\nSee [[missing]].\n\n- Keep this. ^c-abcdef\n";
    let put = h.put(ADMIN_TOKEN, "fleet", "a", None, original).await;
    let before = card(&h, "fleet", "a").await;
    let operation =
        json!({"type":"anchor_blocks","scope":"fleet","card_id":"a","base_revision":rev(&put)});
    let opened = submit(&h, ADMIN_TOKEN, vec![operation]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(opened.body["tier"], 0, "{opened:?}");
    // No local policy can replace the hub's missing authority signals.
    let automatic = h.call(ADMIN_TOKEN, json!({"action":"auto_apply","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_ne!(automatic.body["state"], "applied", "{automatic:?}");
    assert_eq!(card(&h, "fleet", "a").await["text"], before["text"]);
    let applied = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let after = card(&h, "fleet", "a").await;
    assert_eq!(
        after["text"],
        anchor_blocks(before["text"].as_str().unwrap(), "a").unwrap()
    );
    let rollback = h.call(ADMIN_TOKEN,json!({"action":"rollback","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(rollback.status, 201, "{rollback:?}");
    let restored = decide(&h, ADMIN_TOKEN, &rollback.body["proposal_id"]).await;
    assert_eq!(restored.body["state"], "applied", "{restored:?}");
    assert_eq!(card(&h, "fleet", "a").await["text"], before["text"]);
}
