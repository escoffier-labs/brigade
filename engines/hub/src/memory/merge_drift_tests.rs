//! Exact-duplicate merges auto-apply on a real scope under bounded drift (#2591).
//! Every hub here uses the real scope `fleet`, except the one test that pins
//! the fixture scope's lack of any relaxation.
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
const GROUP_TOO_LARGE: &str = "retrieval_eval_merge_group_too_large";
const DRIFTED: &str = "retrieval_eval_target_drifted";
const LINEAGE: &str = "retrieval_eval_lineage_exposed";
const WRITER: &str = "retrieval_eval_writer_changed";
const CROSS_WRITER: &str = "retrieval_eval_merge_cross_writer";
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
async fn merge_op(r: &Real, keep: &str, gone: &str) -> Json {
    let (k, o) = (
        card(&r.h, "fleet", keep).await,
        card(&r.h, "fleet", gone).await,
    );
    let pin = proposal_blocks::pins(k["text"].as_str().unwrap()).remove(0);
    json!({"type":"merge_exact_duplicates","scope":"fleet","card_id":keep,
        "base_revision":k["revision"],"block_id":pin["block_id"],"expected_block_hash":pin["hash"],
        "other_card":{"scope":"fleet","card_id":gone,"base_revision":o["revision"]}})
}
/// Submit `ops` and assert the proposal opens at `tier`.
async fn submit_ops(r: &Real, ops: Vec<Json>, tier: u8) -> Reply {
    let opened = submit(&r.h, ADMIN_TOKEN, ops).await;
    assert_eq!(
        (opened.status, &opened.body["tier"]),
        (201, &json!(tier)),
        "{opened:?}"
    );
    opened
}
/// One proposal that merges each of `retired` into `keeper`.
async fn open_ops(r: &Real, keeper: &str, retired: &[&str]) -> Reply {
    let mut ops = Vec::new();
    for id in retired {
        ops.push(merge_op(r, keeper, id).await);
    }
    submit_ops(r, ops, 0).await
}
async fn open(r: &Real) -> Reply {
    open_ops(r, KEEP, &[GONE]).await
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

/// Two trusted (imported) exact duplicates and one unrelated card.
async fn trusted() -> Real {
    let other = "Another topic entirely. ^c-cccccc\n";
    let r = Real::new(&[
        (KEEP, &dup("", "aaaaaa")),
        (GONE, &dup("", "bbbbbb")),
        ("other-card", other),
    ])
    .await;
    assert_eq!(card(&r.h, "fleet", KEEP).await["trust"], "imported");
    r
}
async fn auto(r: &Real, opened: &Reply) -> Reply {
    let request = attestation_tests::auto_request(&r.h, &opened.body["proposal_id"]).await;
    r.h.call(ADMIN_TOKEN, request).await
}
/// Every gate code an automatic attempt reported, passing or held.
fn codes(reply: &Reply) -> Vec<Json> {
    [
        &reply.body["preflight"]["checks"],
        &reply.body["auto_apply_gates"],
    ]
    .iter()
    .filter_map(|c| c.as_array())
    .flatten()
    .map(|c| c["code"].clone())
    .collect()
}
async fn preflight_report(r: &Real, opened: &Reply) -> Json {
    attestation_tests::report(&r.h, &opened.body["proposal_id"])
        .await
        .body
}
fn gate(report: &Json, name: &str) -> (Json, Json) {
    let checks = report["checks"].as_array().unwrap();
    let c = checks.iter().find(|c| c["gate"] == name).unwrap();
    (c["state"].clone(), c["code"].clone())
}
/// Rewrite the hub policy's memory section.
fn policy(r: &Real, edit: impl FnOnce(&mut Json)) {
    let db = rusqlite::Connection::open(&r.h.hub).unwrap();
    let raw: String = db
        .query_row("SELECT document FROM fleet_policy_revisions", [], |row| {
            row.get(0)
        })
        .unwrap();
    let mut doc: Json = serde_json::from_str(&raw).unwrap();
    edit(&mut doc["memory"]);
    db.execute(
        "UPDATE fleet_policy_revisions SET document=?1",
        [doc.to_string()],
    )
    .unwrap();
}
async fn assert_not_applied(r: &Real, opened: &Reply, why: &str) -> Reply {
    let attempt = auto(r, opened).await;
    assert_ne!(attempt.body["state"], "applied", "{why}: {attempt:?}");
    assert_eq!(card(&r.h, "fleet", GONE).await["state"], "live", "{why}");
    attempt
}

/// A clean merge of two trusted duplicates meets every gate, so the hub applies
/// it with no reviewer. The retired card is retired and the keeper carries its id
/// as an alias. The relaxations are spelled out in the report.
#[tokio::test]
async fn a_clean_merge_of_trusted_duplicates_applies_automatically() {
    let r = trusted().await;
    let opened = open(&r).await;
    assert_eq!(held(&r, &r.body(&opened)), None);
    let report = preflight_report(&r, &opened).await;
    assert!(preflight::all_satisfied(&report), "{report:?}");
    assert_eq!(report["changed_path_count"], 1);
    assert_eq!(report["structural_merge_proof"], true);
    assert_eq!(report["structural_anchor_proof"], false);
    let applied = auto(&r, &opened).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let (keeper, retired) = (
        card(&r.h, "fleet", KEEP).await,
        card(&r.h, "fleet", GONE).await,
    );
    assert_eq!(retired["state"], "retired");
    assert_eq!(
        (keeper["state"].clone(), keeper["trust"].clone()),
        (json!("live"), json!("imported"))
    );
    let aliases = proposal_metadata::list(keeper["text"].as_str().unwrap(), "aliases").unwrap();
    assert!(aliases.contains("fleet/dup-retired"), "{aliases:?}");
}

/// A real automatic apply is reversed by the exact inverse, restoring both cards.
#[tokio::test]
async fn the_compensation_inverse_of_an_automatic_merge_restores_both_cards_exactly() {
    let r = trusted().await;
    let before = [
        card(&r.h, "fleet", KEEP).await,
        card(&r.h, "fleet", GONE).await,
    ];
    let opened = open(&r).await;
    let id = opened.body["proposal_id"].clone();
    assert_eq!(auto(&r, &opened).await.body["state"], "applied");
    assert_eq!(card(&r.h, "fleet", GONE).await["state"], "retired");
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
            (&now["text"], &now["state"], &now["trust"]),
            (&was["text"], &json!("live"), &was["trust"])
        );
    }
}

/// Kill switch, policy, class listing, ownership and cooldown still gate a merge.
#[tokio::test]
async fn the_older_controls_still_hold_a_merge() {
    type Setup = fn(&Real);
    let cases: [(&str, &str, Setup); 4] = [
        ("auto_apply off", "auto_apply_disabled", |r| {
            policy(r, |m| m["auto_apply"] = json!(false))
        }),
        ("class unlisted", "class_disabled", |r| {
            policy(r, |m| m["tier0_op_classes"] = json!(["append_evidence"]))
        }),
        ("owner not hub", "owner_not_hub", |r| {
            policy(r, |m| m["curation_owner"] = json!("operator"))
        }),
        ("principal unlisted", "reflect_principal_disabled", |r| {
            policy(r, |m| m["reflect_principals"] = json!(["agent:dot"]))
        }),
    ];
    for (name, code, setup) in cases {
        let r = trusted().await;
        let opened = open(&r).await;
        setup(&r);
        let attempt = assert_not_applied(&r, &opened, name).await;
        assert!(
            codes(&attempt).contains(&json!(code)),
            "{name}: {attempt:?}"
        );
    }
    let r = trusted().await;
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
}

/// Both cards of the pair stay in the 24 hour cooldown.
#[tokio::test]
async fn either_card_of_the_pair_in_the_cooldown_holds_the_merge() {
    for id in [KEEP, GONE] {
        let r = trusted().await;
        let opened = open(&r).await;
        r.db.execute("INSERT INTO memory_events(seq,event_id,client_event_id,kind,principal,scope,card_id,revision,content_hash,trust,payload,created_at) SELECT 900001,'recent-apply','recent-apply','proposal.apply',writer,scope,card_id,900001,content_hash,trust,'{}',?2 FROM memory_cards WHERE scope='fleet' AND card_id=?1", rusqlite::params![id, ids::isoformat(r.h.now())]).unwrap();
        let report = preflight_report(&r, &opened).await;
        assert_eq!(
            gate(&report, "cooldown_history").1,
            json!("cooldown_active"),
            "{id}"
        );
        assert_not_applied(&r, &opened, id).await;
    }
}

/// Only one clean exact pair gets the relaxations. Anything else keeps the
/// blast cap and the attested evidence floors.
#[tokio::test]
async fn anything_but_one_exact_pair_keeps_the_blast_cap_and_evidence_floors() {
    let held_gates = |report: &Json, count: u64| {
        assert_eq!(
            gate(report, "delta"),
            (json!("held"), json!("blast_cap_exceeded"))
        );
        assert_ne!(
            gate(report, "evidence_floor").0,
            json!("satisfied"),
            "{report:?}"
        );
        assert_ne!(
            gate(report, "source_group_floor").0,
            json!("satisfied"),
            "{report:?}"
        );
        assert_eq!(report["changed_path_count"], count);
        assert_eq!(report["structural_merge_proof"], false);
    };
    // Two pairs in one proposal.
    let two = [
        (KEEP, dup("", "aaaaaa")),
        (GONE, dup("", "bbbbbb")),
        ("dup-keeper-2", "Second claim. ^c-cccccc\n".to_string()),
        ("dup-retired-2", "Second claim. ^c-dddddd\n".to_string()),
    ];
    let cards: Vec<_> = two.iter().map(|(id, text)| (*id, text.as_str())).collect();
    let r = Real::new(&cards).await;
    let ops = vec![
        merge_op(&r, KEEP, GONE).await,
        merge_op(&r, "dup-keeper-2", "dup-retired-2").await,
    ];
    let opened = submit_ops(&r, ops, 0).await;
    held_gates(&preflight_report(&r, &opened).await, 4);
    assert_not_applied(&r, &opened, "two pairs").await;
    // One pair beside another op class.
    let r = trusted().await;
    let other = card(&r.h, "fleet", "other-card").await;
    let pin = proposal_blocks::pins(other["text"].as_str().unwrap()).remove(0);
    let evidence = json!({"type":"append_evidence","scope":"fleet","card_id":"other-card",
        "base_revision":other["revision"],"block_id":pin["block_id"],
        "expected_block_hash":pin["hash"],"evidence_ids":["ns/one/two"]});
    // The alias excuse covers an all-merge proposal only, so beside another op
    // the keeper's new alias is lineage and the proposal opens at tier 2.
    let ops = vec![merge_op(&r, KEEP, GONE).await, evidence];
    let opened = submit_ops(&r, ops, 2).await;
    let report = preflight_report(&r, &opened).await;
    assert_eq!(
        gate(&report, "delta"),
        (json!("held"), json!("delta_requires_review"))
    );
    assert_eq!(report["changed_path_count"], 3);
    assert_eq!(report["structural_merge_proof"], false);
    assert_not_applied(&r, &opened, "mixed").await;
    // The proposal says the claims match. The live bytes of the retired card do not.
    let r = trusted().await;
    let opened = open(&r).await;
    let hash = card(&r.h, "fleet", GONE).await["content_hash"].clone();
    r.db.execute(
        "UPDATE memory_blobs SET bytes=CAST('Different claim. ^c-bbbbbb' || char(10) AS BLOB) WHERE content_hash=?1",
        [hash.as_str().unwrap()],
    )
    .unwrap();
    held_gates(&preflight_report(&r, &opened).await, 2);
    assert_not_applied(&r, &opened, "live bytes differ").await;
}

/// `agent-unreviewed` merges stay out of automatic apply.
#[tokio::test]
async fn an_unreviewed_pair_still_fails_the_card_trust_gate() {
    let r = pair().await;
    let opened = open(&r).await;
    let report = preflight_report(&r, &opened).await;
    assert_eq!(
        gate(&report, "card_trust"),
        (json!("held"), json!("card_trust_denied"))
    );
    assert_eq!(
        gate(&report, "delta"),
        (json!("satisfied"), json!("delta_satisfied"))
    );
    assert_not_applied(&r, &opened, "agent-unreviewed").await;
}

/// The relaxations are for merges alone. Evidence needs attested proof and two
/// changed cards still exceed the cap.
#[tokio::test]
async fn other_op_classes_are_as_strict_as_before() {
    let cards = [
        ("a", "---\ntitle: Alpha\n---\nClaim one. ^c-abcdef\n"),
        ("b", "---\ntitle: Beta\n---\nClaim two. ^c-fedcba\n"),
    ];
    let r = Real::new(&cards).await;
    let evidence = |c: &Json, id: &str| {
        let pin = proposal_blocks::pins(c["text"].as_str().unwrap()).remove(0);
        json!({"type":"append_evidence","scope":"fleet","card_id":id,
            "base_revision":c["revision"],"block_id":pin["block_id"],
            "expected_block_hash":pin["hash"],"evidence_ids":["ns/one/two"]})
    };
    let (a, b) = (
        card(&r.h, "fleet", "a").await,
        card(&r.h, "fleet", "b").await,
    );
    let opened = submit_ops(&r, vec![evidence(&a, "a")], 0).await;
    let report = preflight_report(&r, &opened).await;
    assert_eq!(gate(&report, "delta").0, json!("satisfied"));
    assert_eq!(
        gate(&report, "evidence_floor"),
        (json!("held"), json!("proof_binding_incomplete"))
    );
    assert_eq!(
        gate(&report, "evidence_authority"),
        (json!("held"), json!("evidence_authority_unbound"))
    );
    assert_eq!(report["structural_merge_proof"], false);
    let opened = submit_ops(&r, vec![evidence(&a, "a"), evidence(&b, "b")], 0).await;
    let report = preflight_report(&r, &opened).await;
    assert_eq!(
        gate(&report, "delta"),
        (json!("held"), json!("blast_cap_exceeded"))
    );
    assert_eq!(report["changed_path_count"], 2);
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

/// The resolver compares names loosely: case folded, with spaces and runs of
/// hyphens alike. A link it would follow to the retired card dangles after the
/// merge, so every such spelling holds it, in a body link or a front-matter value.
#[tokio::test]
async fn a_loosely_spelled_link_to_the_retired_id_holds_the_merge() {
    let forms = [
        ("capitalized wiki link", "See [[Dup-Retired]].\n"),
        ("upper case wiki link", "See [[DUP-RETIRED]].\n"),
        ("spaced wiki link", "See [[dup retired]].\n"),
        ("mixed spaced wiki link", "See [[Dup  Retired|old]].\n"),
        ("doubled hyphen", "See [[dup--retired]].\n"),
        ("padded", "See [[ dup-retired ]].\n"),
        ("cased scope prefix", "See [[Fleet/Dup-Retired]].\n"),
        ("cased path", "See [[Cards/Dup-Retired.MD]].\n"),
        ("cased markdown link", "See [old](Cards/DUP-RETIRED.md).\n"),
        ("spaced markdown link", "See [old](<dup retired>).\n"),
        (
            "cased reference definition",
            "See [old].\n\n[old]: <./Cards/Dup-Retired.md>\n",
        ),
        (
            "cased front matter value",
            "---\nrelated: Dup-Retired\n---\nBody.\n",
        ),
        (
            "cased superseded_by",
            "---\nsuperseded_by: DUP-RETIRED\n---\nBody.\n",
        ),
        (
            "spaced front matter list",
            "---\nrelated: [\"Dup Retired\"]\n---\nBody.\n",
        ),
        (
            "cased front matter path",
            "---\nrelated: Cards/Dup-Retired.md\n---\nBody.\n",
        ),
    ];
    for (name, text) in forms {
        assert_eq!(naming_card_holds(text).await, Some(INBOUND), "{name}");
    }
    // Looser matching still means the same card. Another id never matches.
    for (name, text) in [
        ("keeper in other case", "See [[Dup-Keeper]].\n"),
        ("longer name", "See [[dup retired extra]].\n"),
        ("shorter name", "See [[Dup]].\n"),
        (
            "cased front matter keeper",
            "---\nrelated: DUP-KEEPER\n---\nB.\n",
        ),
    ] {
        assert_eq!(naming_card_holds(text).await, None, "{name}");
    }
}

/// A clean trusted merge reaches the apply, so the recheck under the writer lock
/// is what stops a link that lands after the preparation read.
#[tokio::test]
async fn a_link_added_between_preflight_and_apply_is_caught_at_apply() {
    let mut r = trusted().await;
    // The linker exists but names nothing, so the first read finds no link.
    link(&r, "Nothing to see.\n").await;
    let opened = open(&r).await;
    assert_eq!(held(&r, &r.body(&opened)), None);
    let report = preflight_report(&r, &opened).await;
    assert!(preflight::all_satisfied(&report), "{report:?}");
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
    let reply = auto(&r, &opened).await;
    assert_eq!(reply.body["state"], "pending", "{reply:?}");
    assert_eq!(reply.body["reason"], "auto_apply_held", "{reply:?}");
    assert_eq!(codes(&reply), vec![json!(INBOUND)], "{reply:?}");
    assert_eq!(card(&r.h, "fleet", GONE).await["state"], "live");
    assert_eq!(held(&r, &r.body(&opened)), Some(INBOUND));
}

/// The alias the merge adds is not lineage. A link that existed before it is.
#[tokio::test]
async fn only_lineage_that_existed_before_the_merge_routes_to_review() {
    let older = "---\nsupersedes: [older]\n---\n";
    let aliased = "---\naliases: [\"fleet/earlier-duplicate\"]\n---\n";
    let pairs = [
        (
            "both declare lineage",
            dup(older, "aaaaaa"),
            dup(older, "bbbbbb"),
        ),
        (
            "keeper declares lineage",
            dup(older, "aaaaaa"),
            dup("", "bbbbbb"),
        ),
        (
            "retired card already has an alias",
            dup("", "aaaaaa"),
            dup(aliased, "bbbbbb"),
        ),
    ];
    for (name, keeper, retired) in pairs {
        let r = Real::new(&[(KEEP, &keeper), (GONE, &retired)]).await;
        let op = merge_op(&r, KEEP, GONE).await;
        let opened = submit(&r.h, ADMIN_TOKEN, vec![op]).await;
        assert_eq!(opened.body["tier"], 2, "{name}: {opened:?}");
    }
    // Another live card that names the keeper as a predecessor.
    let r = trusted().await;
    link(&r, "---\nsupersedes: [dup-keeper]\n---\nBody.\n").await;
    let op = merge_op(&r, KEEP, GONE).await;
    let opened = submit(&r.h, ADMIN_TOKEN, vec![op]).await;
    assert_eq!(opened.body["tier"], 2, "{opened:?}");
    // The same link arriving after tiering is caught by the preflight backstop.
    let r = trusted().await;
    let body = r.body(&open(&r).await);
    assert_eq!(held(&r, &body), None);
    link(&r, "---\nsupersedes: [dup-keeper]\n---\nBody.\n").await;
    assert_eq!(held(&r, &body), Some(LINEAGE));
    // A forged after text that smuggles in a lineage link is not excused by the
    // merge rule. Lineage review judges the after text, less only the alias the
    // merge adds, so it names the link before the recomputed keeper text does.
    let r = trusted().await;
    let body = r.body(&open(&r).await);
    let forged = edit(&body, KEEP, |t| {
        t.replacen("aliases: [", "aliases: [\"fleet/older\",", 1)
    });
    assert_eq!(held(&r, &forged), Some(LINEAGE));
}

/// Pre-existing lineage that only a JSON spelling shows. `literal_eval` reads
/// `fleet\/victim` as an id no card has, while the merge decodes it to
/// `fleet/victim` and writes it back plain, which would add a live link to
/// `victim`. Both the keeper's and the retired card's before text hide it.
#[tokio::test]
async fn lineage_hidden_by_a_json_escape_is_still_lineage_for_the_merge() {
    let escaped = "---\naliases: [\"fleet\\/victim\"]\n---\n";
    let plain = "---\naliases: [\"fleet/victim\"]\n---\n";
    let own = "---\naliases: [\"fleet\\/dup-retired\"]\n---\n";
    let cases = [
        (
            "escaped alias on the keeper",
            dup(escaped, "aaaaaa"),
            dup("", "bbbbbb"),
        ),
        (
            "escaped alias on the retired card",
            dup("", "aaaaaa"),
            dup(escaped, "bbbbbb"),
        ),
        (
            "plain alias on the keeper",
            dup(plain, "aaaaaa"),
            dup("", "bbbbbb"),
        ),
        // An escaped alias naming the retired card itself is no new link, but the
        // two parsers still disagree about it, so a reviewer decides.
        (
            "escaped alias to the retired id",
            dup(own, "aaaaaa"),
            dup("", "bbbbbb"),
        ),
    ];
    for (name, keeper, retired) in cases {
        let r = Real::new(&[
            (KEEP, &keeper),
            (GONE, &retired),
            ("victim", "Another topic entirely. ^c-cccccc\n"),
        ])
        .await;
        let op = merge_op(&r, KEEP, GONE).await;
        let opened = submit(&r.h, ADMIN_TOKEN, vec![op]).await;
        assert_eq!(opened.body["tier"], 2, "{name}: {opened:?}");
        let attempt = auto(&r, &opened).await;
        assert_ne!(attempt.body["state"], "applied", "{name}: {attempt:?}");
        assert_eq!(card(&r.h, "fleet", GONE).await["state"], "live", "{name}");
    }
    // The same escape that slips into a tier 0 body after tiering is held at
    // preflight and apply, by the lineage backstop.
    let r = trusted().await;
    let body = r.body(&open(&r).await);
    let forged = retext(&body, KEEP, &dup(escaped, "aaaaaa"));
    assert_eq!(held(&r, &forged), Some(LINEAGE));
}

/// Every lineage key is read twice, as JSON by the merge and as a Python literal
/// by search and review. A `\/` escape in any of them, not only the aliases the
/// merge rewrites, decodes to a card for one reader and to nothing for the other,
/// so the pair goes to a reviewer. A forged tier 0 body is held by the backstop.
#[tokio::test]
async fn lineage_hidden_by_an_escape_in_any_lineage_key_is_lineage_for_the_merge() {
    let lists = |key: &str| format!("---\n{key}: [\"fleet\\/victim\"]\n---\n");
    let scalar = |key: &str| format!("---\n{key}: \"fleet\\/victim\"\n---\n");
    let mut cases = Vec::new();
    for key in ["supersedes", "merged_from", "aliases"] {
        cases.push((key, lists(key)));
    }
    cases.push(("superseded_by", scalar("superseded_by")));
    for (key, front) in cases {
        assert!(
            merge_gate::lineage_ambiguous(&dup(&front, "aaaaaa"), "fleet"),
            "{key}"
        );
        for side in ["keeper", "retired"] {
            let (keeper, retired) = match side {
                "keeper" => (dup(&front, "aaaaaa"), dup("", "bbbbbb")),
                _ => (dup("", "aaaaaa"), dup(&front, "bbbbbb")),
            };
            let r = Real::new(&[
                (KEEP, &keeper),
                (GONE, &retired),
                ("victim", "Another topic entirely. ^c-cccccc\n"),
            ])
            .await;
            let op = merge_op(&r, KEEP, GONE).await;
            let opened = submit(&r.h, ADMIN_TOKEN, vec![op]).await;
            assert_eq!(opened.body["tier"], 2, "{key} on the {side}: {opened:?}");
            let attempt = auto(&r, &opened).await;
            assert_ne!(attempt.body["state"], "applied", "{key} on the {side}");
            assert_eq!(card(&r.h, "fleet", GONE).await["state"], "live");
        }
        let r = trusted().await;
        let body = r.body(&open(&r).await);
        let forged = retext(&body, KEEP, &dup(&front, "aaaaaa"));
        assert_eq!(held(&r, &forged), Some(LINEAGE), "{key}");
    }
    // Control: the same keys spelled plainly and naming nothing are no ambiguity.
    for key in ["supersedes", "merged_from", "aliases"] {
        let plain = format!("---\n{key}: [\"fleet/victim\"]\n---\n");
        assert!(!merge_gate::lineage_ambiguous(&plain, "fleet"), "{key}");
    }
    assert!(!merge_gate::lineage_ambiguous(
        "---\nsuperseded_by: fleet/victim\n---\n",
        "fleet"
    ));
    assert!(!merge_gate::lineage_ambiguous(&dup("", "aaaaaa"), "fleet"));
}

/// An alias the keeper already lists in plain form is not one the merge adds,
/// even when it names the retired id. The subtraction of added aliases must not
/// excuse it, so the pair goes to review whatever the before text says.
#[tokio::test]
async fn a_plain_alias_to_the_retired_id_that_was_already_there_is_lineage() {
    let plain = "---\naliases: [\"fleet/dup-retired\"]\n---\n";
    let r = Real::new(&[
        (KEEP, &dup(plain, "aaaaaa")),
        (GONE, &dup("", "bbbbbb")),
        ("victim", "Another topic entirely. ^c-cccccc\n"),
    ])
    .await;
    let op = merge_op(&r, KEEP, GONE).await;
    let opened = submit(&r.h, ADMIN_TOKEN, vec![op]).await;
    assert_eq!(opened.body["tier"], 2, "{opened:?}");
    let attempt = auto(&r, &opened).await;
    assert_ne!(attempt.body["state"], "applied", "{attempt:?}");
    assert_eq!(card(&r.h, "fleet", GONE).await["state"], "live");
    // A tier 0 body that forges the alias into the after text alone is held by
    // the backstop, which judges the keeper's after text less only added aliases.
    let r = trusted().await;
    let body = r.body(&open(&r).await);
    assert_eq!(held(&r, &body), None);
    let forged = edit(&body, KEEP, |after| {
        after.replacen("---\n", &format!("---\naliases: [\"fleet/{GONE}\"]\n"), 1)
    });
    assert_ne!(held(&r, &forged), None);
}

/// The older gates a merge relaxes all pass for one trusted exact pair, so the
/// merge gate alone is left to decide the apply.
async fn relaxed_gates_pass(r: &Real, opened: &Reply, why: &str) {
    let report = preflight_report(r, opened).await;
    for name in [
        "delta",
        "evidence_floor",
        "source_group_floor",
        "card_trust",
    ] {
        let (state, _) = gate(&report, name);
        assert_eq!(state, json!("satisfied"), "{why}: {name}: {report:?}");
    }
    assert_eq!(report["structural_merge_proof"], true, "{why}");
}
/// An automatic attempt that ends held for the merge gate rule `code`. The
/// retrieval check names it in the report, the proposal stays pending with
/// `auto_apply_held`, and every card the proposal would retire stays live.
async fn held_by_merge_gate(r: &Real, opened: &Reply, retired: &[&str], code: &str, why: &str) {
    let report = preflight_report(r, opened).await;
    assert_eq!(
        gate(&report, "retrieval_eval"),
        (json!("unverifiable"), json!(code)),
        "{why}: {report:?}"
    );
    assert!(!preflight::all_satisfied(&report), "{why}");
    let attempt = auto(r, opened).await;
    assert_eq!(attempt.body["state"], "pending", "{why}: {attempt:?}");
    assert_eq!(
        attempt.body["reason"], "auto_apply_held",
        "{why}: {attempt:?}"
    );
    assert!(codes(&attempt).contains(&json!(code)), "{why}: {attempt:?}");
    for id in retired {
        assert_eq!(
            card(&r.h, "fleet", id).await["state"],
            "live",
            "{why}: {id}"
        );
    }
}
/// A churn written by the admin leaves the cards agent-unreviewed. Both read as
/// imported again, so the pair is the trusted one the auto-apply route admits.
fn reimport(r: &Real) {
    r.db.execute("UPDATE memory_cards SET trust='imported'", [])
        .unwrap();
}

/// The three merge gate rules a card's history can break stay in force once the
/// older preflight gates stop holding a clean merge.
#[tokio::test]
async fn a_revived_id_or_a_copy_by_edit_as_keeper_ends_held_on_a_real_scope() {
    let (old, victim) = ("attacker-old", "victim");
    let (old_text, victim_text) = (dup("", "aaaaaa"), dup("", "bbbbbb"));
    // The attacker retires the card made first and writes it again.
    let r = Real::new(&[(old, &old_text), (victim, &victim_text)]).await;
    retire(&r, old).await;
    rewrite(&r, old, &old_text).await;
    reimport(&r);
    let opened = open_ops(&r, old, &[victim]).await;
    relaxed_gates_pass(&r, &opened, "revived id").await;
    held_by_merge_gate(&r, &opened, &[victim], RETIRES_OLDER, "revived id").await;
    // The attacker edits an unrelated old card into a copy of the victim.
    let own = "Something unrelated. ^c-zzzzzz\n";
    let r = Real::new(&[(old, own), (victim, &victim_text)]).await;
    rewrite(&r, old, &old_text).await;
    reimport(&r);
    let opened = open_ops(&r, old, &[victim]).await;
    relaxed_gates_pass(&r, &opened, "copy by edit").await;
    held_by_merge_gate(&r, &opened, &[victim], RETIRES_OLDER, "copy by edit").await;
    // Control: the victim as keeper retires the copy and applies automatically.
    let r = Real::new(&[(old, own), (victim, &victim_text)]).await;
    rewrite(&r, old, &old_text).await;
    reimport(&r);
    let opened = open_ops(&r, victim, &[old]).await;
    let applied = auto(&r, &opened).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    assert_eq!(card(&r.h, "fleet", old).await["state"], "retired");
}

/// A link that differs from the retired id only in case or spacing still holds.
#[tokio::test]
async fn a_case_folded_inbound_link_ends_held_on_a_real_scope() {
    for (name, text) in [
        ("capitalized wiki link", "See [[Dup-Retired]].\n"),
        ("spaced wiki link", "See [[dup retired]].\n"),
        ("cased path", "See [[Cards/Dup-Retired.MD]].\n"),
        (
            "cased front matter value",
            "---\nrelated: DUP-RETIRED\n---\nBody.\n",
        ),
    ] {
        let r = trusted().await;
        link(&r, text).await;
        let opened = open(&r).await;
        relaxed_gates_pass(&r, &opened, name).await;
        held_by_merge_gate(&r, &opened, &[GONE], INBOUND, name).await;
    }
}

/// The proposal card limit already refuses a ninth card on submit, so a group
/// larger than eight can only reach the gate as a stored body the hub did not
/// build. The body is grown in the store, and the automatic attempt must hold.
#[tokio::test]
async fn a_group_larger_than_eight_ends_held_on_a_real_scope() {
    let r = trusted().await;
    let opened = open(&r).await;
    let mut body = r.body(&opened);
    let cards = body["cards"].as_array().unwrap().clone();
    let op = body["ops"][0].clone();
    let retired = cards.iter().find(|c| c["card_id"] == GONE).unwrap().clone();
    for i in 0..8 {
        let id = format!("extra-{i}");
        let mut card = retired.clone();
        card["card_id"] = json!(id);
        body["cards"].as_array_mut().unwrap().push(card);
        let mut more = op.clone();
        more["other_card"]["card_id"] = json!(id);
        body["ops"].as_array_mut().unwrap().push(more);
    }
    r.db.execute(
        "UPDATE memory_proposals SET body=?1 WHERE proposal_id=?2",
        rusqlite::params![
            body.to_string(),
            opened.body["proposal_id"].as_str().unwrap()
        ],
    )
    .unwrap();
    held_by_merge_gate(&r, &opened, &[GONE], GROUP_TOO_LARGE, "nine retired").await;
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

/// The before-text rule drops only the alias the merge itself adds. A trusted
/// pair that already sits in the published lineage graph still goes to review,
/// whether the card at the end of the edge is the keeper or the retired one.
#[tokio::test]
async fn a_trusted_merge_pair_in_the_published_lineage_graph_still_routes_to_review() {
    let r = trusted().await;
    let body = r.body(&open(&r).await);
    let hold = |snapshot| r.hold_as(&body, "admin", ADMIN, None, Some(snapshot));
    // The alias the merge adds is not in the published graph, so it is no exposure.
    assert_eq!(held(&r, &body), None);
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

/// Retire a card as the admin and read nothing back but the outcome.
async fn retire(r: &Real, id: &str) {
    let rev: i64 =
        r.db.query_row(
            "SELECT revision FROM memory_cards WHERE card_id=?1",
            [id],
            |row| row.get(0),
        )
        .unwrap();
    let epoch = r.h.epoch().await;
    let reply =
        r.h.call(
            ADMIN_TOKEN,
            json!({"action":"retire","scope":"fleet","card_id":id,"base_revision":rev,
                   "base_epoch":epoch,"client_event_id":r.h.id()}),
        )
        .await;
    assert_eq!(reply.status, 200, "{reply:?}");
}
/// Write a card again over its current head, retired or live.
async fn rewrite(r: &Real, id: &str, text: &str) {
    let rev: i64 =
        r.db.query_row(
            "SELECT revision FROM memory_cards WHERE card_id=?1",
            [id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(
        r.h.put(ADMIN_TOKEN, "fleet", id, Some(rev), text)
            .await
            .status,
        201
    );
}

/// An id is dated from its current incarnation. An attacker who retires the
/// card they made first and writes it again with a victim's exact text must not
/// be able to pass as the older card and retire the victim's.
#[tokio::test]
async fn reviving_an_older_id_does_not_backdate_it() {
    let (old, victim) = ("attacker-old", "victim");
    let (old_text, victim_text) = (dup("", "aaaaaa"), dup("", "bbbbbb"));
    let r = Real::untrusted(&[(old, &old_text), (victim, &victim_text)]).await;
    // Before the churn the attacker's card really is the older one.
    let honest = r.body(&open_ops(&r, old, &[victim]).await);
    assert_eq!(held(&r, &honest), None);
    retire(&r, old).await;
    rewrite(&r, old, &old_text).await;
    let exploit = open_ops(&r, old, &[victim]).await;
    assert_eq!(held(&r, &r.body(&exploit)), Some(RETIRES_OLDER));
    // The victim's card, as keeper, may retire the revived copy.
    let r = Real::untrusted(&[(old, &old_text), (victim, &victim_text)]).await;
    retire(&r, old).await;
    rewrite(&r, old, &old_text).await;
    let reversed = open_ops(&r, victim, &[old]).await;
    assert_eq!(held(&r, &r.body(&reversed)), None);
}

/// A card that never left `live` keeps its first write as its start, even when
/// the duplicate it outranks was retired and written again later.
#[tokio::test]
async fn a_keeper_that_never_left_live_passes_a_newer_duplicate() {
    let (keeper_text, copy_text) = (dup("", "aaaaaa"), dup("", "bbbbbb"));
    let r = Real::untrusted(&[(KEEP, &keeper_text), (GONE, &copy_text)]).await;
    assert_eq!(held(&r, &r.body(&open(&r).await)), None);
    retire(&r, GONE).await;
    rewrite(&r, GONE, &copy_text).await;
    assert_eq!(held(&r, &r.body(&open(&r).await)), None);
}

/// Dating follows the latest write of the id, so a card written again after
/// the keeper existed is the newer copy, however early its first revision.
#[tokio::test]
async fn a_card_revived_after_the_keeper_is_the_newer_copy() {
    let (first, second) = (dup("", "aaaaaa"), dup("", "bbbbbb"));
    let r = Real::untrusted(&[(GONE, &first), (KEEP, &second)]).await;
    // GONE was written first, so it is the older card and is held.
    let before = r.body(&open(&r).await);
    assert_eq!(held(&r, &before), Some(RETIRES_OLDER));
    retire(&r, GONE).await;
    rewrite(&r, GONE, &first).await;
    assert_eq!(held(&r, &r.body(&open(&r).await)), None);
}

/// A card revived before the keeper was written is still the older one.
#[tokio::test]
async fn a_card_revived_before_the_keeper_was_written_is_still_older() {
    let (first, second) = (dup("", "aaaaaa"), dup("", "bbbbbb"));
    let r = Real::untrusted(&[(GONE, &first)]).await;
    retire(&r, GONE).await;
    rewrite(&r, GONE, &first).await;
    assert_eq!(
        r.h.put(ADMIN_TOKEN, "fleet", KEEP, None, &second)
            .await
            .status,
        201
    );
    let held_older = r.body(&open(&r).await);
    assert_eq!(held(&r, &held_older), Some(RETIRES_OLDER));
    // The earlier card as keeper may retire the later copy.
    let reversed = open_ops(&r, GONE, &[KEEP]).await;
    assert_eq!(held(&r, &r.body(&reversed)), None);
}

/// A card is dated from when its current claim body first appeared. Editing
/// an old card into a copy of a newer one must not leave it looking older.
#[tokio::test]
async fn editing_an_old_card_into_a_copy_does_not_backdate_it() {
    let (old, victim) = ("attacker-old", "victim");
    let own = "Something unrelated. ^c-zzzzzz\n";
    let (copy, original) = (dup("", "aaaaaa"), dup("", "bbbbbb"));
    let r = Real::untrusted(&[(old, own), (victim, &original)]).await;
    rewrite(&r, old, &copy).await;
    let exploit = open_ops(&r, old, &[victim]).await;
    assert_eq!(held(&r, &r.body(&exploit)), Some(RETIRES_OLDER));
    // The victim's card, as keeper, may retire the edited copy.
    let r = Real::untrusted(&[(old, own), (victim, &original)]).await;
    rewrite(&r, old, &copy).await;
    let reversed = open_ops(&r, victim, &[old]).await;
    assert_eq!(held(&r, &r.body(&reversed)), None);
}

/// A body edited away and back is dated from the return, not from the first
/// time the text appeared, or an old card could be flipped into a copy later.
#[tokio::test]
async fn editing_a_body_away_and_back_dates_it_from_the_return() {
    let (first, second) = (dup("", "aaaaaa"), dup("", "bbbbbb"));
    let r = Real::untrusted(&[(KEEP, &first), (GONE, &second)]).await;
    assert_eq!(held(&r, &r.body(&open(&r).await)), None);
    rewrite(&r, KEEP, "Something unrelated. ^c-zzzzzz\n").await;
    rewrite(&r, KEEP, &first).await;
    assert_eq!(held(&r, &r.body(&open(&r).await)), Some(RETIRES_OLDER));
}

/// Front-matter rewrites leave the claim body alone, so a genuine original that
/// later gained aliases or evidence is still older than a newer duplicate.
#[tokio::test]
async fn an_original_that_gained_aliases_and_evidence_is_still_older() {
    let (first, second) = (dup("", "aaaaaa"), dup("", "bbbbbb"));
    let r = Real::untrusted(&[(KEEP, &first), (GONE, &second)]).await;
    let aliased = dup("---\naliases: [\"older-name\"]\n---\n", "aaaaaa");
    rewrite(&r, KEEP, &aliased).await;
    let both = dup(
        "---\naliases: [\"older-name\"]\nevidence: [\"ns/kind/one\"]\n---\n",
        "aaaaaa",
    );
    rewrite(&r, KEEP, &both).await;
    assert_eq!(held(&r, &r.body(&open(&r).await)), None);
}

/// One keeper may retire at most eight cards, so the work per proposal stays
/// bounded. The size check comes before any card is read.
#[tokio::test]
async fn a_group_of_more_than_eight_retired_cards_is_held() {
    let r = pair().await;
    let body = r.body(&open(&r).await);
    let grown = |extra: usize| {
        let mut out = body.clone();
        let cards = out["cards"].as_array().unwrap().clone();
        let op = out["ops"][0].clone();
        let retired = cards.iter().find(|c| c["card_id"] == GONE).unwrap().clone();
        for i in 0..extra {
            let id = format!("extra-{i}");
            let mut card = retired.clone();
            card["card_id"] = json!(id);
            out["cards"].as_array_mut().unwrap().push(card);
            let mut more = op.clone();
            more["other_card"]["card_id"] = json!(id);
            out["ops"].as_array_mut().unwrap().push(more);
        }
        out
    };
    assert_eq!(held(&r, &grown(8)), Some(GROUP_TOO_LARGE));
    assert_ne!(held(&r, &grown(7)), Some(GROUP_TOO_LARGE));
}

/// The head's own revision row must exist. Older rows alone do not show when
/// the head was written.
#[tokio::test]
async fn a_head_without_its_revision_row_holds_the_merge() {
    let text = dup("", "aaaaaa");
    let r = Real::untrusted(&[(KEEP, &text), (GONE, &dup("", "bbbbbb"))]).await;
    rewrite(&r, KEEP, &text).await;
    let body = r.body(&open(&r).await);
    assert_eq!(held(&r, &body), None);
    sql(
        &r,
        "DELETE FROM memory_card_revisions WHERE card_id=?1 AND revision=(SELECT revision FROM memory_cards WHERE card_id=?1)",
        KEEP,
    );
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

const OTHER_WRITER: &str =
    "UPDATE memory_cards SET writer='node:one',writer_class='node' WHERE card_id=?1";

/// An automatic merge retires a card, so it may only retire a card the applier
/// wrote. A trusted pair written by two credentials is held from automatic
/// apply whichever card is the foreign one, and stays an open proposal that a
/// reviewer can accept. Without this, any reflect principal could plant a card
/// with common claim text and later retire another principal's newer copy.
#[tokio::test]
async fn a_cross_writer_trusted_pair_is_held_for_review_and_a_reviewer_can_accept_it() {
    for trust in ["imported", "reviewed"] {
        for (side, foreign) in [("retired", GONE), ("keeper", KEEP)] {
            let why = format!("{trust} pair, foreign {side}");
            let r = trusted().await;
            r.db.execute("UPDATE memory_cards SET trust=?1", [trust])
                .unwrap();
            sql(&r, OTHER_WRITER, foreign);
            let opened = open(&r).await;
            assert_eq!(held(&r, &r.body(&opened)), Some(CROSS_WRITER), "{why}");
            // The relaxations are off, so the report cannot read as one clean change.
            let report = preflight_report(&r, &opened).await;
            assert_eq!(report["structural_merge_proof"], false, "{why}");
            assert_eq!(
                gate(&report, "delta"),
                (json!("held"), json!("blast_cap_exceeded")),
                "{why}"
            );
            held_by_merge_gate(&r, &opened, &[GONE], CROSS_WRITER, &why).await;
            // The proposal is still open, not refused, so a reviewer can accept it.
            let decided = decide(&r.h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
            assert_eq!(decided.body["state"], "applied", "{why}: {decided:?}");
            assert_eq!(card(&r.h, "fleet", GONE).await["state"], "retired", "{why}");
        }
    }
}

/// One credential wrote both cards, but it is not the one applying. The apply
/// would rewrite each card's writer to the applier's, so it is held. The same
/// credential applying its own pair is the one clean case.
#[tokio::test]
async fn a_same_writer_pair_is_held_when_the_applier_is_another_credential() {
    let r = trusted().await;
    for id in [KEEP, GONE] {
        sql(&r, OTHER_WRITER, id);
    }
    let opened = open(&r).await;
    let body = r.body(&opened);
    assert_eq!(r.hold_as(&body, "node", "node:one", None, None), None);
    for (class, principal) in [
        ("admin", ADMIN),
        ("node", "node:two"),
        ("admin", "node:one"),
    ] {
        let hold = r.hold_as(&body, class, principal, None, None);
        assert_eq!(hold, Some(CROSS_WRITER), "{principal} {class}");
    }
    // The ADMIN credential applies here, and it did not write either card.
    held_by_merge_gate(
        &r,
        &opened,
        &[GONE],
        CROSS_WRITER,
        "applier is not the writer",
    )
    .await;
}

/// The writer is read from the live heads, so a proposal body cannot claim one.
#[tokio::test]
async fn the_writer_of_a_merge_pair_comes_from_the_live_heads() {
    let r = trusted().await;
    let opened = open(&r).await;
    let mut body = r.body(&opened);
    assert_eq!(held(&r, &body), None);
    for card in body["cards"].as_array_mut().unwrap() {
        card["writer"] = json!("node:one");
        card["writer_class"] = json!("node");
    }
    body["proposer"] = json!("node:one");
    assert_eq!(held(&r, &body), None);
    sql(&r, OTHER_WRITER, GONE);
    assert_eq!(held(&r, &body), Some(CROSS_WRITER));
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

/// Lineage review reads the keeper's before text, so a before text that is not
/// the live one is a forgery, whatever hash it carries.
#[tokio::test]
async fn a_forged_keeper_before_text_is_held() {
    let r = trusted().await;
    let body = r.body(&open(&r).await);
    assert_eq!(held(&r, &body), None);
    let mut forged = body.clone();
    let keeper = forged["cards"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|c| c["card_id"] == KEEP)
        .unwrap();
    keeper["before_text"] = json!("Forged claim. ^c-aaaaaa\n");
    assert_eq!(held(&r, &forged), Some(SHAPE));
}

/// The one scope that is not real. Its proposals skip the merge gate and take
/// the stored fixture report, so an exact pair there gets no relaxation.
#[tokio::test]
async fn an_exact_pair_on_the_fixture_scope_keeps_the_blast_cap() {
    use super::retrieval_eval::FIXTURE_SCOPE;
    let r = Real::fixture(&[(KEEP, &dup("", "aaaaaa")), (GONE, &dup("", "bbbbbb"))]).await;
    let (k, o) = (
        card(&r.h, FIXTURE_SCOPE, KEEP).await,
        card(&r.h, FIXTURE_SCOPE, GONE).await,
    );
    let pin = proposal_blocks::pins(k["text"].as_str().unwrap()).remove(0);
    let op = json!({"type":"merge_exact_duplicates","scope":FIXTURE_SCOPE,"card_id":KEEP,
        "base_revision":k["revision"],"block_id":pin["block_id"],"expected_block_hash":pin["hash"],
        "other_card":{"scope":FIXTURE_SCOPE,"card_id":GONE,"base_revision":o["revision"]}});
    let opened = submit_ops(&r, vec![op], 0).await;
    let report = preflight_report(&r, &opened).await;
    assert_eq!(
        gate(&report, "delta"),
        (json!("held"), json!("blast_cap_exceeded")),
        "{report:?}"
    );
    assert_ne!(
        gate(&report, "evidence_floor").0,
        json!("satisfied"),
        "{report:?}"
    );
    assert_eq!(report["changed_path_count"], 2);
    assert_eq!(report["structural_merge_proof"], false);
    assert!(!preflight::all_satisfied(&report));
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
/// keeper. Only trusted cards form edges. With an untrusted keeper a card that
/// later reuses the retired id gains no edge to it, and a trusted keeper does.
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
    // Control: a trusted keeper's alias is a live edge once the id is reused.
    // The merge gate admits trusted pairs and does not look ahead to that write.
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
