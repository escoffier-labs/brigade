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
        Value::Integer(1),
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
    assert!(shown.contains(r#""is_reply": true"#), "{shown}");
    assert!(shown.contains(r#""content_state": "sealed""#), "{shown}");
    let (_, shown) = message(&row, "2026-03-09T00:00:01+00:00");
    assert!(
        shown.ends_with(r#""envelope": "{\"from\":\"a\",\"v\":1}", "envelope_bytes": 18, "content_state": "content_expired"}"#),
        "{shown}"
    );
    row[20] = Value::Integer(0);
    row[17] = Value::Null;
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
        r.extend([
            Value::Null,
            Value::Null,
            Value::Integer(0),
            t("x"),
            Value::Null,
            Value::Null,
        ]);
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

struct Memory(rusqlite::Connection);
impl ReadTx for Memory {
    fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
        let mut statement = self.0.prepare(sql)?;
        let width = statement.column_count();
        let rows = statement.query_map(rusqlite::params_from_iter(params.iter()), |row| {
            (0..width).map(|i| row.get::<_, Value>(i)).collect()
        })?;
        rows.collect()
    }
}
impl WriteTx for Memory {
    fn execute(&self, sql: &str, params: &[Value]) -> rusqlite::Result<usize> {
        self.0
            .execute(sql, rusqlite::params_from_iter(params.iter()))
    }
}

#[test]
fn retention_deletes_a_bounded_batch_per_command_and_drains_the_backlog() {
    let tx = Memory(rusqlite::Connection::open_in_memory().unwrap());
    tx.0.execute_batch(
        "CREATE TABLE fleet_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, \
         reply_to INTEGER, is_reply INTEGER NOT NULL DEFAULT 0, acked_at TEXT, envelope TEXT, envelope_bytes INTEGER, content_state TEXT); \
         CREATE INDEX fleet_messages_created_at ON fleet_messages (created_at); \
         CREATE TABLE fleet_message_sends(id INTEGER PRIMARY KEY, sender TEXT, received_at TEXT);",
    )
    .unwrap();
    let policy = Policy {
        retention_days: 7,
        ..Policy::default()
    };
    let now = 400 * DAY_US;
    let old = iso_us(now - 30 * DAY_US);
    let fresh = iso_us(now - DAY_US);
    // Ten expired rows; each fresh row replies to one of them.
    for _ in 0..10 {
        tx.execute(
            "INSERT INTO fleet_messages(created_at) VALUES(?)",
            &[t(&old)],
        )
        .unwrap();
    }
    for parent in 1..=10 {
        tx.execute(
            "INSERT INTO fleet_messages(created_at,reply_to,is_reply) VALUES(?,?,1)",
            &[t(&fresh), Value::Integer(parent)],
        )
        .unwrap();
    }
    let count = |sql: &str| -> i64 { tx.0.query_row(sql, [], |r| r.get(0)).unwrap() };
    retain(&tx, now, &policy, PURGE_BATCH, 4).unwrap();
    assert_eq!(count("SELECT COUNT(*) FROM fleet_messages WHERE id<=10"), 6);
    // Only the children of the deleted parents lost their link.
    assert_eq!(
        count("SELECT COUNT(*) FROM fleet_messages WHERE reply_to IS NULL AND id>10"),
        4
    );
    assert_eq!(
        count(
            "SELECT COUNT(*) FROM fleet_messages AS c WHERE c.reply_to IS NOT NULL \
               AND NOT EXISTS (SELECT 1 FROM fleet_messages AS p WHERE p.id=c.reply_to)"
        ),
        0
    );
    retain(&tx, now, &policy, PURGE_BATCH, 4).unwrap();
    retain(&tx, now, &policy, PURGE_BATCH, 4).unwrap();
    // Once the backlog clears the rows match Python's single unbounded pass.
    assert_eq!(count("SELECT COUNT(*) FROM fleet_messages"), 10);
    assert_eq!(
        count("SELECT COUNT(*) FROM fleet_messages WHERE reply_to IS NULL"),
        10
    );
    assert_eq!(
        count("SELECT COUNT(*) FROM fleet_messages WHERE is_reply=1"),
        10
    );
}

#[test]
fn an_expired_row_without_a_tombstone_never_returns_its_ciphertext() {
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
        Value::Null,
        Value::Integer(0),
        t("2026-03-02T00:00:00+00:00"),
        t("2026-03-02T00:00:01+00:00"),
        t("node-b"),
        Value::Null,
        Value::Null,
        t("message"),
        t("not an envelope"),
        Value::Integer(15),
        t("sealed"),
        Value::Integer(1),
    ];
    let (_, shown) = message(&row, "2026-03-09T00:00:01+00:00");
    assert!(
        shown.ends_with(r#""kind": "message", "content_state": "content_expired"}"#),
        "{shown}"
    );
    assert!(!shown.contains("not an envelope"), "{shown}");
    // Before its lifetime ends the stored bytes are returned as they are.
    row[12] = t("2026-03-09T00:00:02+00:00");
    let (_, shown) = message(&row, "2026-03-09T00:00:01+00:00");
    assert!(shown.contains(r#""content_state": "sealed""#), "{shown}");
}

#[test]
fn a_list_keeps_only_the_fields_it_reads() {
    let pad = "x".repeat(1024 * 1024);
    let Json::Object(raw) = json!({
        "action": "list",
        "after": 3,
        "unacked": true,
        "wait_ms": 20000,
        "pad": pad,
        "more": [pad.clone(), pad.clone()],
    }) else {
        unreachable!()
    };
    let fields = ListFields::new(raw);
    assert_eq!(
        Json::Object(fields.raw().clone()),
        json!({"after": 3, "unacked": true, "wait_ms": 20000})
    );
    // A padded value in a field `list` reads fails its check either way, so
    // only a null stands in for it, and the refusal is the same.
    let Json::Object(raw) = json!({"after": pad, "unacked": [pad], "wait_ms": {"x": pad}}) else {
        unreachable!()
    };
    let fields = ListFields::new(raw);
    assert!(serde_json::to_string(fields.raw()).unwrap().len() < 64);
    let Json::Object(raw) = json!({"after": pad, "wait_ms": 5}) else {
        unreachable!()
    };
    assert!(integer(ListFields::new(raw).raw().get("after"), true).is_none());
    let Json::Object(raw) = json!({"action": "list"}) else {
        unreachable!()
    };
    assert!(ListFields::new(raw).raw().is_empty());
}
