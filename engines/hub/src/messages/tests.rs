use super::*;

#[test]
fn enqueued_at_reads_common_iso_forms_and_leaves_the_rest_to_python() {
    let utc = |s: &str| match stamp(s) {
        Stamp::Utc(us) => Some(iso_us(us)),
        _ => None,
    };
    assert_eq!(
        utc("2026-03-02T01:30:00+01:30").as_deref(),
        Some("2026-03-02T00:00:00+00:00")
    );
    assert_eq!(
        utc("2026-03-02T00:00:00.123Z").as_deref(),
        Some("2026-03-02T00:00:00.123000+00:00")
    );
    assert_eq!(
        utc("2026-03-01T23:00:00.000001-01:00").as_deref(),
        Some("2026-03-02T00:00:00.000001+00:00")
    );
    assert!(matches!(stamp("2026-03-02T00:00:00"), Stamp::Naive));
    for python in [
        "2026-03-02",
        "2026-03-02 00:00:00+00:00",
        "2026-03-02T00:00:00.1Z",
        "2026-02-30T00:00:00Z",
        "0001-01-01T00:00:00+01:00",
        "2026-03-02T00:00:00+00:00:30",
    ] {
        assert!(matches!(stamp(python), Stamp::Python), "{python}");
    }
}

#[test]
fn payload_digest_matches_python_json_dumps() {
    // hashlib.sha256(json.dumps(["node-b", "s", "bé\n", None, "normal", None,
    //   {"thread": "t"}], ensure_ascii=False).encode()).hexdigest()
    let fields = r#"["node-b", "s", "bé\n", null, "normal", null, {"thread": "t"}]"#;
    assert_eq!(
        payload_digest(
            ["node-b", "s", "bé\n"],
            None,
            "normal",
            None,
            &[("thread", "t".into())]
        ),
        sha256_hex(fields.as_bytes())
    );
}

#[test]
fn metadata_keeps_python_order_and_refusals() {
    let raw = |v: Json| v.as_object().unwrap().clone();
    let extras = metadata(
        &raw(json!({"kind": "question", "thread": "a/b#1", "sender_label": "bot"})),
        "node-a",
    )
    .unwrap();
    assert_eq!(
        extras,
        vec![
            ("sender_label", "bot".to_string()),
            ("thread", "a/b#1".into()),
            ("kind", "question".into())
        ]
    );
    let refusal = |v: Json, sender: &str| match metadata(&raw(v), sender) {
        Err(Fail::Error { message, .. }) => message,
        other => panic!("{other:?}"),
    };
    assert_eq!(
        refusal(json!({"sender_label": "x"}), "agent:bot"),
        "sender_label is only available to node principals"
    );
    assert_eq!(refusal(json!({"thread": "-x"}), "n"), "thread is invalid");
    assert_eq!(
        refusal(json!({"kind": "operator_question"}), "n"),
        "operator_question is reserved for hub-created operator questions"
    );
    assert!(
        metadata(&raw(json!({"kind": "message", "thread": null})), "n")
            .unwrap()
            .is_empty()
    );
}

#[test]
fn rows_project_expired_parents_and_sealed_tombstones() {
    let envelope = r#"{"ct":"x","from":"a","recipients":[],"v":1}"#;
    let mut row: Vec<Value> = vec![
        Value::Integer(7),
        t("node-a"),
        t("node-b"),
        t(""),
        t(""),
        Value::Null,
        t("normal"),
        t("c1"),
        Value::Null,
        Value::Integer(3),
        t("2026-03-02T00:00:00+00:00"),
        t("2026-03-02T00:00:01+00:00"),
        t("node-b"),
        Value::Null,
        Value::Null,
        t("message"),
        t(envelope),
        Value::Integer(envelope.len() as i64),
        t("sealed"),
        Value::Integer(1),
    ];
    let (id, shown) = message(&row, "2026-03-02T00:00:00+00:00");
    assert_eq!(id, 7);
    assert!(shown.contains(r#""reply_to": null"#), "{shown}");
    assert!(shown.contains(r#""content_state": "sealed""#), "{shown}");
    let (_, shown) = message(&row, "2026-03-09T00:00:01+00:00");
    assert!(
        shown.ends_with(r#""envelope": "{\"from\":\"a\",\"v\":1}", "envelope_bytes": 18, "content_state": "content_expired"}"#),
        "{shown}"
    );
    row[19] = Value::Integer(0);
    row[16] = Value::Null;
    let (_, shown) = message(&row, "");
    assert!(
        shown.contains(r#""reply_to": 3"#) && !shown.contains("envelope"),
        "{shown}"
    );
}

#[test]
fn page_stops_at_the_response_budget() {
    let row = |id: i64, body: &str| -> Vec<Value> {
        let mut r = vec![Value::Integer(id), t("a"), t("b"), t("s"), t(body)];
        r.extend([Value::Null, t("normal"), t("c")]);
        r.extend([Value::Null, Value::Null, t("x"), Value::Null, Value::Null]);
        r.extend([
            Value::Null,
            Value::Null,
            Value::Null,
            Value::Null,
            Value::Null,
        ]);
        r.extend([Value::Null, Value::Null]);
        r
    };
    // 65,536 three-byte characters escape to six bytes each.
    let big = "\u{20ac}".repeat(21_845);
    let rows: Vec<_> = (1..=100).map(|i| row(i, &big)).collect();
    let body = page(&rows, 0, "");
    assert!(body.len() <= MAX_LIST_RESPONSE_BYTES);
    let shown: Json = serde_json::from_str(&body).unwrap();
    let n = shown["messages"].as_array().unwrap().len();
    assert!(n > 1 && n < 100);
    assert_eq!(shown["next_cursor"], n);
    assert_eq!(page(&[], 9, ""), r#"{"messages": [], "next_cursor": 9}"#);
}

#[test]
fn policy_reads_the_environment_like_python() {
    let env = |pairs: &'static [(&'static str, &'static str)]| {
        move |k: &str| {
            pairs
                .iter()
                .find(|(name, _)| *name == k)
                .map(|(_, v)| v.to_string())
        }
    };
    let p = Policy::from_env(
        env(&[("BRIGADE_FLEET_MESSAGES_RETENTION_DAYS", "7")]),
        false,
    )
    .unwrap();
    assert_eq!((p.retention_days, p.require_sealed), (7, false));
    assert!(Policy::from_env(env(&[]), true).unwrap().require_sealed);
    assert!(
        Policy::from_env(
            env(&[("BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED", "yes")]),
            false
        )
        .is_err()
    );
    assert!(
        Policy::from_env(
            env(&[("BRIGADE_FLEET_MESSAGES_MAX_ENVELOPE_BYTES", "012")]),
            false
        )
        .is_err()
    );
    assert!(
        Policy::from_env(
            env(&[("BRIGADE_FLEET_MESSAGES_RETENTION_DAYS", "3651")]),
            false
        )
        .is_err()
    );
}
