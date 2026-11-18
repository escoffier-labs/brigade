//! A same-writer exact pair of `agent-unreviewed` cards applies automatically
//! (#2591). Every hub here uses the real scope `fleet`. The merge gate's own
//! writer rule decides, so each hold below is either the card_trust gate or a
//! code the merge gate already had.
use super::{merge_drift_tests::*, proposal_tests::*, ranking_gate_tests::Real, tests::*, *};
use serde_json::{Value as Json, json};

fn trust_gate(report: &Json) -> (Json, Json) {
    gate(report, "card_trust")
}
const DENIED: (&str, &str) = ("held", "card_trust_denied");

/// The pair the admin wrote and the admin applies: no reviewer, no trust change.
#[tokio::test]
async fn an_unreviewed_same_writer_exact_pair_applies_and_rolls_back_exactly() {
    let r = pair().await;
    let before = [
        card(&r.h, "fleet", KEEP).await,
        card(&r.h, "fleet", GONE).await,
    ];
    assert_eq!(before[0]["trust"], "agent-unreviewed");
    let opened = open(&r).await;
    let report = preflight_report(&r, &opened).await;
    assert!(preflight::all_satisfied(&report), "{report:?}");
    assert_eq!(
        trust_gate(&report),
        (json!("satisfied"), json!("card_trust_satisfied"))
    );
    assert_eq!(report["structural_merge_proof"], true);
    let id = opened.body["proposal_id"].clone();
    let applied = auto(&r, &opened).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let (keeper, retired) = (
        card(&r.h, "fleet", KEEP).await,
        card(&r.h, "fleet", GONE).await,
    );
    assert_eq!(retired["state"], "retired");
    assert_eq!(
        (&keeper["state"], &keeper["trust"]),
        (&json!("live"), &json!("agent-unreviewed"))
    );
    let aliases = proposal_metadata::list(keeper["text"].as_str().unwrap(), "aliases").unwrap();
    assert!(aliases.contains("fleet/dup-retired"), "{aliases:?}");
    r.h.set_clock(r.h.now() + 1);
    let undo = r.h.call(ADMIN_TOKEN, json!({"action":"rollback","proposal_id":id,"base_epoch":r.h.epoch().await,"client_event_id":r.h.id()})).await;
    assert_eq!(undo.body["state"], "applied", "{undo:?}");
    for (id, was) in [KEEP, GONE].into_iter().zip(&before) {
        let now = card(&r.h, "fleet", id).await;
        assert_eq!(
            (&now["text"], &now["state"], &now["trust"]),
            (&was["text"], &json!("live"), &json!("agent-unreviewed")),
            "{id}"
        );
        assert_eq!(now["text"], was["text"], "{id}");
    }
}

/// Another credential applying turns the same pair into a hold, because the
/// apply would rewrite each card's writer to the applier's.
#[tokio::test]
async fn the_same_writer_pair_is_held_when_another_principal_applies() {
    let r = pair().await;
    for id in [KEEP, GONE] {
        sql(&r, OTHER_WRITER, id);
    }
    let opened = open(&r).await;
    let body = r.body(&opened);
    // The writer itself applying is the one clean case.
    let own = r.evaluate_as(&body, "node", "node:one");
    assert_eq!(
        trust_gate(&own),
        (json!("satisfied"), json!("card_trust_satisfied"))
    );
    assert_eq!(own["structural_merge_proof"], true);
    for (class, principal) in [
        ("admin", ADMIN),
        ("node", "node:two"),
        ("admin", "node:one"),
    ] {
        let report = r.evaluate_as(&body, class, principal);
        assert_eq!(
            (
                trust_gate(&report),
                report["structural_merge_proof"].clone()
            ),
            ((json!(DENIED.0), json!(DENIED.1)), json!(false)),
            "{principal} {class}"
        );
        assert!(!preflight::all_satisfied(&report), "{principal} {class}");
    }
    // The ADMIN credential applies for real here, and it wrote neither card.
    let attempt = assert_not_applied(&r, &opened, "applier is not the writer").await;
    assert!(
        codes(&attempt).contains(&json!("card_trust_denied")),
        "{attempt:?}"
    );
}

/// Cards written by two credentials stay a review, whichever card is foreign,
/// and a person can still accept the open proposal.
#[tokio::test]
async fn a_cross_writer_unreviewed_pair_is_held_and_stays_reviewable() {
    for (side, foreign) in [("retired", GONE), ("keeper", KEEP)] {
        let r = pair().await;
        sql(&r, OTHER_WRITER, foreign);
        let opened = open(&r).await;
        // The merge gate's own writer hold comes first for an unreviewed pair.
        assert_eq!(held(&r, &r.body(&opened)), Some(WRITER), "{side}");
        let report = preflight_report(&r, &opened).await;
        assert_eq!(report["structural_merge_proof"], false, "{side}");
        assert_eq!(
            trust_gate(&report),
            (json!(DENIED.0), json!(DENIED.1)),
            "{side}"
        );
        held_by_merge_gate(&r, &opened, &[GONE], WRITER, side).await;
        let decided = decide(&r.h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
        assert_eq!(decided.body["state"], "applied", "{side}: {decided:?}");
        assert_eq!(
            card(&r.h, "fleet", GONE).await["state"],
            "retired",
            "{side}"
        );
    }
}

/// A flagged card or a mixed pair never gets the relaxation.
#[tokio::test]
async fn flagged_and_mixed_trust_pairs_stay_held() {
    let cases = [
        ("flagged pair", "UPDATE memory_cards SET trust='flagged'"),
        (
            "flagged retired",
            "UPDATE memory_cards SET trust='flagged' WHERE card_id='dup-retired'",
        ),
        (
            "reviewed keeper",
            "UPDATE memory_cards SET trust='reviewed' WHERE card_id='dup-keeper'",
        ),
        (
            "reviewed retired",
            "UPDATE memory_cards SET trust='reviewed' WHERE card_id='dup-retired'",
        ),
    ];
    for (name, statement) in cases {
        // Changed after the proposal opens, so the live trust no longer matches.
        let r = pair().await;
        let opened = open(&r).await;
        r.db.execute(statement, []).unwrap();
        let report = preflight_report(&r, &opened).await;
        assert_eq!(report["structural_merge_proof"], false, "{name}");
        assert_eq!(trust_gate(&report).0, json!("held"), "{name}: {report:?}");
        assert_not_applied(&r, &opened, name).await;
        // Changed before it opens, so the proposal itself carries the trust.
        let r = pair().await;
        r.db.execute(statement, []).unwrap();
        let reply = submit(&r.h, ADMIN_TOKEN, vec![merge_op(&r, KEEP, GONE).await]).await;
        assert_eq!(reply.status, 201, "{name}: {reply:?}");
        let report = preflight_report(&r, &reply).await;
        assert!(!preflight::all_satisfied(&report), "{name}: {report:?}");
        assert_not_applied(&r, &reply, name).await;
    }
}

/// Several pairs, a larger group and a pair beside another op keep every
/// relaxation off, so the unreviewed cards stay out of automatic apply.
#[tokio::test]
async fn only_a_single_exact_pair_qualifies() {
    let cards = [
        (KEEP, dup("", "aaaaaa")),
        (GONE, dup("", "bbbbbb")),
        ("dup-keeper-2", "Second claim. ^c-cccccc\n".to_string()),
        ("dup-retired-2", "Second claim. ^c-dddddd\n".to_string()),
    ];
    let refs: Vec<_> = cards.iter().map(|(id, t)| (*id, t.as_str())).collect();
    // Two pairs in one proposal.
    let r = Real::untrusted(&refs).await;
    let ops = vec![
        merge_op(&r, KEEP, GONE).await,
        merge_op(&r, "dup-keeper-2", "dup-retired-2").await,
    ];
    let opened = submit(&r.h, ADMIN_TOKEN, ops).await;
    assert_eq!(opened.body["tier"], 0, "{opened:?}");
    let report = preflight_report(&r, &opened).await;
    assert_eq!(trust_gate(&report), (json!(DENIED.0), json!(DENIED.1)));
    assert_eq!(report["structural_merge_proof"], false);
    assert_not_applied(&r, &opened, "two pairs").await;
    // A pair beside append_evidence on another card.
    let other = card(&r.h, "fleet", "dup-keeper-2").await;
    let pin = proposal_blocks::pins(other["text"].as_str().unwrap()).remove(0);
    let evidence = json!({"type":"append_evidence","scope":"fleet","card_id":"dup-keeper-2",
        "base_revision":other["revision"],"block_id":pin["block_id"],
        "expected_block_hash":pin["hash"],"evidence_ids":["ns/one/two"]});
    let ops = vec![merge_op(&r, KEEP, GONE).await, evidence];
    let opened = submit(&r.h, ADMIN_TOKEN, ops).await;
    assert_eq!(opened.body["tier"], 0, "{opened:?}");
    let report = preflight_report(&r, &opened).await;
    assert_eq!(trust_gate(&report), (json!(DENIED.0), json!(DENIED.1)));
    assert_eq!(report["structural_merge_proof"], false);
    assert_not_applied(&r, &opened, "pair beside evidence").await;
    // One keeper retiring seven cards, the most a proposal carries. A group of
    // nine is refused at submit, before any gate sees it.
    let texts: Vec<(String, String)> = (0..9)
        .map(|i| (format!("big-{i}"), dup("", &format!("{i:06}"))))
        .collect();
    let refs: Vec<_> = texts
        .iter()
        .map(|(i, t)| (i.as_str(), t.as_str()))
        .collect();
    let r = Real::untrusted(&refs).await;
    let ids: Vec<_> = texts.iter().map(|(id, _)| id.as_str()).collect();
    let mut ops = Vec::new();
    for id in &ids[1..] {
        ops.push(merge_op(&r, ids[0], id).await);
    }
    let refused = submit(&r.h, ADMIN_TOKEN, ops).await;
    assert_eq!(refused.status, 422, "{refused:?}");
    let opened = open_ops(&r, ids[0], &ids[1..8]).await;
    let report = preflight_report(&r, &opened).await;
    assert_eq!(trust_gate(&report), (json!(DENIED.0), json!(DENIED.1)));
    assert_eq!(report["structural_merge_proof"], false);
    let attempt = auto(&r, &opened).await;
    assert_ne!(attempt.body["state"], "applied", "{attempt:?}");
    assert_eq!(card(&r.h, "fleet", "big-1").await["state"], "live");
}

/// Evidence and anchoring on an unreviewed card keep their card_trust hold.
#[tokio::test]
async fn evidence_and_anchoring_on_unreviewed_cards_stay_denied() {
    let r = Real::untrusted(&[(
        "a",
        "---\ntitle: Alpha\n---\nClaim one. ^c-abcdef\n\nClaim two.\n",
    )])
    .await;
    let opened = r.evidence("a", &["ns/one/two"]).await;
    assert_eq!(opened.body["tier"], 0, "{opened:?}");
    let report = preflight_report(&r, &opened).await;
    assert_eq!(trust_gate(&report), (json!(DENIED.0), json!(DENIED.1)));
    r.auto_authored("a");
    let opened = r.anchor("a").await;
    assert_eq!(opened.body["tier"], 0, "{opened:?}");
    let report = preflight_report(&r, &opened).await;
    assert_eq!(trust_gate(&report), (json!(DENIED.0), json!(DENIED.1)));
    assert!(!preflight::all_satisfied(&report));
}

/// The other merge gate rules and the older controls still hold the pair.
#[tokio::test]
async fn the_merge_gate_and_older_controls_still_hold_an_unreviewed_pair() {
    // A copy never retires the card it copied.
    let r = pair().await;
    let reversed = open_ops(&r, GONE, &[KEEP]).await;
    assert_eq!(held(&r, &r.body(&reversed)), Some(RETIRES_OLDER));
    assert_not_applied(&r, &reversed, "retires older").await;
    // A live card naming the retired one would dangle.
    let r = pair().await;
    let linked = r.h.put(
        ADMIN_TOKEN,
        "fleet",
        "linker",
        None,
        "See [[dup-retired]].\n",
    );
    assert_eq!(linked.await.status, 201);
    let opened = open(&r).await;
    assert_eq!(held(&r, &r.body(&opened)), Some(INBOUND));
    assert_not_applied(&r, &opened, "inbound link").await;
    // Policy listing, the master switch and the kill switch.
    let r = pair().await;
    let opened = open(&r).await;
    policy(&r, |m| m["tier0_op_classes"] = json!(["append_evidence"]));
    let attempt = assert_not_applied(&r, &opened, "class unlisted").await;
    assert!(
        codes(&attempt).contains(&json!("class_disabled")),
        "{attempt:?}"
    );
    let r = pair().await;
    let opened = open(&r).await;
    let off =
        r.h.call(
            ADMIN_TOKEN,
            json!({"action":"auto_apply_switch","enabled":false,"client_event_id":r.h.id()}),
        )
        .await;
    assert_eq!(off.status, 200, "{off:?}");
    let attempt = assert_not_applied(&r, &opened, "kill switch").await;
    assert!(
        codes(&attempt).contains(&json!("auto_apply_killed")),
        "{attempt:?}"
    );
    // Either card of the pair in the cooldown.
    let r = pair().await;
    let opened = open(&r).await;
    r.db.execute("INSERT INTO memory_events(seq,event_id,client_event_id,kind,principal,scope,card_id,revision,content_hash,trust,payload,created_at) SELECT 900001,'recent-apply','recent-apply','proposal.apply',writer,scope,card_id,900001,content_hash,trust,'{}',?2 FROM memory_cards WHERE scope='fleet' AND card_id=?1", rusqlite::params![KEEP, ids::isoformat(r.h.now())]).unwrap();
    let report = preflight_report(&r, &opened).await;
    assert_eq!(
        gate(&report, "cooldown_history").1,
        json!("cooldown_active")
    );
    assert_not_applied(&r, &opened, "cooldown").await;
}
