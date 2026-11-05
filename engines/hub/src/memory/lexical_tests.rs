//! S13b goldens: Python wrote `tests/lexical-golden.json` and `pytables`.
use super::{
    index::{Builder, Card, Change},
    pytables, search, text,
};
use serde_json::Value as Json;
use std::collections::HashMap;

const GOLDEN: &str = include_str!("../../tests/lexical-golden.json");
fn golden() -> Json {
    serde_json::from_str(GOLDEN).unwrap()
}
fn s(v: &Json) -> &str {
    v.as_str().unwrap()
}

#[test]
fn tables_and_parameters_match_the_golden() {
    let g = golden();
    assert_eq!(s(&g["unicode"]), pytables::UNICODE);
    assert_eq!(g["k1"].as_f64(), Some(super::index::K1));
    assert_eq!(g["b"].as_f64(), Some(super::index::B));
    assert_eq!(g["digits"].as_i64(), Some(4));
}

#[test]
fn tokenizer_lower_and_single_line_match_python() {
    let g = golden();
    for case in g["tokenize"].as_array().unwrap() {
        let input = s(&case["text"]);
        assert_eq!(text::py_lower(input), s(&case["lower"]), "{input:?}");
        let tokens: Vec<String> = case["tokens"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| s(t).to_string())
            .collect();
        assert_eq!(text::tokenize(input), tokens, "{input:?}");
    }
    for case in g["single_line"].as_array().unwrap() {
        assert_eq!(text::single_line(s(&case["text"])), s(&case["line"]));
    }
}

#[test]
fn search_fields_match_python() {
    for case in golden()["fields"].as_array().unwrap() {
        let id = s(&case["card_id"]);
        let body = s(&case["text"]);
        let f = text::fields(body, id);
        assert_eq!(f.title, s(&case["title"]), "{id}");
        assert_eq!(f.summary, s(&case["summary"]), "{id}");
        let tags: Vec<&str> = case["tags"].as_array().unwrap().iter().map(s).collect();
        assert_eq!(f.tags, tags, "{id}");
        let full = text::card_text(&f, body);
        assert_eq!(full, s(&case["card_text"]), "{id}");
        assert_eq!(
            text::tokenize(&full).len() as i64,
            case["length"].as_i64().unwrap(),
            "{id}"
        );
    }
}

#[test]
fn rounding_matches_python_round() {
    for pair in golden()["round"].as_array().unwrap() {
        let (x, want) = (pair[0].as_f64().unwrap(), pair[1].as_f64().unwrap());
        assert_eq!(text::round4(x), want, "round({x}, 4)");
    }
}

#[test]
fn score_json_is_python_float_repr() {
    for x in [0.0, 0.0001, 0.5, 1.0, 2.2712, 12.3456, 1234.5678, 0.0312] {
        assert_eq!(
            serde_json::to_string(&serde_json::json!(x)).unwrap(),
            crate::wire::py_float(x)
        );
    }
}

/// Every golden corpus, built the way the writer builds it: each card as a
/// delta, then the retirements, so removal and statistics are exercised.
#[test]
fn scorer_matches_python_goldens() {
    for (name, corpus) in golden()["corpora"].as_object().unwrap() {
        let mut builder = Builder::empty();
        let mut texts = HashMap::new();
        let mut gone = Vec::new();
        for (i, card) in corpus["cards"].as_array().unwrap().iter().enumerate() {
            let hash = format!("hash-{name}-{i}");
            texts.insert(hash.clone(), s(&card["text"]).to_string());
            builder.apply(
                vec![Change::Live(Card {
                    scope: s(&card["scope"]).into(),
                    card_id: s(&card["card_id"]).into(),
                    revision: i as i64 + 1,
                    content_hash: hash,
                    trust: "agent-unreviewed".into(),
                    text: s(&card["text"]).into(),
                })],
                i as i64 + 1,
            );
            if s(&card["state"]) == "retired" {
                gone.push(Change::Gone {
                    scope: s(&card["scope"]).into(),
                    card_id: s(&card["card_id"]).into(),
                });
            }
        }
        builder.apply(gone, 1000);
        let snap = builder.snapshot();
        for q in corpus["queries"].as_array().unwrap() {
            let query = s(&q["query"]);
            let readable: Vec<&str> = q["readable"].as_array().unwrap().iter().map(s).collect();
            let allowed: Vec<bool> = snap
                .scopes()
                .iter()
                .map(|scope| readable.contains(&scope.as_str()))
                .collect();
            let tokens = text::tokenize(query);
            let hits = if tokens.is_empty() {
                search::literal(&snap, &allowed, query, |h| Ok(texts.get(h).cloned()))
                    .unwrap()
                    .hits
            } else {
                snap.score(&allowed, &tokens)
            };
            let got: Vec<Json> = snap
                .top(hits, usize::MAX)
                .into_iter()
                .map(|(n, score)| {
                    let d = snap.doc(n);
                    serde_json::json!([snap.scopes()[d.scope as usize], &*d.card_id, score])
                })
                .collect();
            assert_eq!(&Json::Array(got), &q["expected"], "{name}: {query:?}");
        }
    }
}

// Store-level behavior through `Store::handle`.
use super::tests::{ADMIN_TOKEN, AGENT_TOKEN, Hub, NODE_TOKEN, code, rev};
use serde_json::json;
use std::time::{Duration, Instant};

async fn search(h: &Hub, token: &str, body: Json) -> super::Reply {
    let mut body = body;
    body["action"] = json!("search");
    h.call(token, body).await
}
fn ids(r: &super::Reply) -> Vec<(String, String)> {
    r.body["matches"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| (s(&m["scope"]).to_string(), s(&m["card_id"]).to_string()))
        .collect()
}
fn pair(scope: &str, card: &str) -> (String, String) {
    (scope.into(), card.into())
}
fn index(h: &Hub) -> std::sync::Arc<super::index::Index> {
    h.store.as_ref().unwrap().index().clone()
}
async fn until(what: &str, mut check: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !check() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

#[tokio::test]
async fn search_scores_the_visible_corpus_and_follows_retirement() {
    let h = Hub::new();
    let a = h
        .put(NODE_TOKEN, "fleet", "deploy", None, "deploy pipeline")
        .await;
    h.put(
        NODE_TOKEN,
        "repo:one",
        "deploy",
        None,
        "deploy deploy notes",
    )
    .await;
    let last = h
        .put(NODE_TOKEN, "repo:two", "notes", None, "pipeline notes")
        .await;
    h.call(
        ADMIN_TOKEN,
        json!({"action":"grant","principal":"agent:dot","scope":"repo:one","rights":["read"],"client_event_id":h.id()}),
    )
    .await;
    let seq = last.body["event"]["seq"].as_i64().unwrap() + 1;
    let all = search(&h, NODE_TOKEN, json!({"query":"deploy","min_seq":seq})).await;
    assert_eq!(all.status, 200, "{:?}", all.body);
    assert_eq!(
        ids(&all),
        vec![pair("repo:one", "deploy"), pair("fleet", "deploy")]
    );
    assert_eq!(all.body["indexed_seq"].as_i64(), Some(seq));
    assert_eq!(
        all.body["retrieval"],
        json!({"mode":"lexical","degraded":false,"stale":false})
    );
    let first = &all.body["matches"][0];
    assert_eq!(first["path"], json!("cards/deploy.md"));
    assert_eq!(first["ranks"], json!({"lexical": 1}));
    assert_eq!(first["trust"], json!("agent-unreviewed"));
    // The principal reads repo:one only: its statistics cover that scope, so
    // its score equals a node search restricted to repo:one.
    let agent = search(&h, AGENT_TOKEN, json!({"query":"deploy"})).await;
    let scoped = search(
        &h,
        NODE_TOKEN,
        json!({"query":"deploy","scopes":["repo:one"]}),
    )
    .await;
    assert_eq!(ids(&agent), vec![pair("repo:one", "deploy")]);
    assert_eq!(agent.body["matches"], scoped.body["matches"]);
    assert_ne!(
        agent.body["matches"][0]["score"],
        all.body["matches"][0]["score"]
    );
    let refused = search(
        &h,
        AGENT_TOKEN,
        json!({"query":"deploy","scopes":["fleet"]}),
    )
    .await;
    assert_eq!(
        (refused.status, code(&refused)),
        (403, "memory_grant_missing")
    );
    // Retire, then revive with new text: the old revision leaves the index.
    let epoch = h.epoch().await;
    let retired = h
        .call(
            NODE_TOKEN,
            json!({"action":"retire","scope":"fleet","card_id":"deploy","base_revision":rev(&a),"base_epoch":epoch,"client_event_id":h.id()}),
        )
        .await;
    let gone = search(
        &h,
        NODE_TOKEN,
        json!({"query":"deploy","min_seq":retired.body["event"]["seq"]}),
    )
    .await;
    assert_eq!(ids(&gone), vec![pair("repo:one", "deploy")]);
    let revived = h
        .put(
            NODE_TOKEN,
            "fleet",
            "deploy",
            Some(rev(&retired)),
            "fresh text",
        )
        .await;
    let back = search(
        &h,
        NODE_TOKEN,
        json!({"query":"fresh","min_seq":revived.body["event"]["seq"]}),
    )
    .await;
    assert_eq!(ids(&back), vec![pair("fleet", "deploy")]);
    assert_eq!(
        back.body["matches"][0]["revision"].as_i64(),
        Some(rev(&revived))
    );
    // Status reports the same watermark.
    let status = h.call(NODE_TOKEN, json!({"action":"status"})).await;
    assert_eq!(status.body["indexed_seq"], back.body["indexed_seq"]);
}

#[tokio::test]
async fn min_seq_past_the_index_answers_stale_after_the_wait() {
    let h = Hub::new();
    let put = h.put(NODE_TOKEN, "fleet", "card", None, "word").await;
    let seq = put.body["event"]["seq"].as_i64().unwrap();
    let started = Instant::now();
    let r = search(&h, NODE_TOKEN, json!({"query":"word","min_seq":seq + 100})).await;
    let waited = started.elapsed();
    assert!(waited >= super::MIN_SEQ_WAIT, "{waited:?}");
    assert!(waited < super::MIN_SEQ_WAIT * 4, "{waited:?}");
    assert_eq!(r.body["retrieval"]["stale"], json!(true));
    assert_eq!(ids(&r), vec![pair("fleet", "card")]);
}

#[tokio::test]
async fn an_index_failure_after_commit_answers_stale_then_rebuilds() {
    let h = Hub::new();
    h.put(NODE_TOKEN, "fleet", "before", None, "alpha").await;
    let i = index(&h);
    until("first publication", || i.current().indexed_seq > 0).await;
    let (loaded, wait_loaded) = std::sync::mpsc::channel();
    let (release, wait_release) = std::sync::mpsc::channel();
    *i.gate.lock().unwrap() = Some((loaded, wait_release));
    i.fail.store(1, std::sync::atomic::Ordering::SeqCst);
    // The write commits and is answered; only its publication fails.
    let failed = h
        .put(NODE_TOKEN, "fleet", "during", None, "alpha beta")
        .await;
    assert_eq!(failed.status, 201, "{:?}", failed.body);
    until("failure", || i.metrics.lock().unwrap().failures == 1).await;
    let stale = search(&h, NODE_TOKEN, json!({"query":"alpha"})).await;
    assert_eq!(stale.body["retrieval"]["stale"], json!(true));
    assert_eq!(ids(&stale), vec![pair("fleet", "before")]);
    // The rebuild read the store. A write that commits now comes after its
    // watermark and must be replayed onto the new segments.
    wait_loaded.recv_timeout(Duration::from_secs(10)).unwrap();
    let late = h
        .put(NODE_TOKEN, "fleet", "late", None, "alpha gamma")
        .await;
    let late_seq = late.body["event"]["seq"].as_i64().unwrap();
    assert!(i.current().stale);
    release.send(()).unwrap();
    until("rebuild", || i.metrics.lock().unwrap().rebuilds == 1).await;
    let fresh = search(&h, NODE_TOKEN, json!({"query":"alpha","min_seq":late_seq})).await;
    assert_eq!(fresh.body["retrieval"]["stale"], json!(false));
    let mut got = ids(&fresh);
    got.sort();
    assert_eq!(
        got,
        vec![
            pair("fleet", "before"),
            pair("fleet", "during"),
            pair("fleet", "late")
        ]
    );
    // The new main segment, plus one delta for the replayed tail.
    assert_eq!(i.current().segments(), 2);
}

#[tokio::test]
async fn deltas_past_ten_percent_rebuild_without_losing_writes() {
    let h = Hub::new();
    let i = index(&h);
    let count = super::index::REBUILD_FLOOR + 8;
    let mut last = 0;
    for n in 0..count {
        let r = h
            .put(
                NODE_TOKEN,
                "fleet",
                &format!("card-{n:03}"),
                None,
                &format!("shared token{n}"),
            )
            .await;
        last = r.body["event"]["seq"].as_i64().unwrap();
    }
    // The segment cap can start a rebuild partway through the writes, so wait until the index has
    // compacted after the last one rather than for the first rebuild alone.
    until("rebuild", || {
        i.metrics.lock().unwrap().rebuilds >= 1 && i.current().segments() < 16
    })
    .await;
    let r = search(
        &h,
        NODE_TOKEN,
        json!({"query":"shared","min_seq":last,"limit":20}),
    )
    .await;
    assert_eq!(r.body["match_count"].as_u64(), Some(count as u64));
    let one = search(&h, NODE_TOKEN, json!({"query":"token7"})).await;
    assert_eq!(ids(&one), vec![pair("fleet", "card-007")]);
}

#[tokio::test]
async fn restart_rebuilds_the_same_answers_from_the_store() {
    let mut h = Hub::new();
    for (scope, card, body) in [
        ("fleet", "a", "---\ntitle: Same\n---\nshared words\n"),
        ("fleet", "a-b", "---\ntitle: Same\n---\nshared words\n"),
        ("repo:one", "b", "words only"),
    ] {
        h.put(NODE_TOKEN, scope, card, None, body).await;
    }
    let seq = h.events();
    let before = search(
        &h,
        NODE_TOKEN,
        json!({"query":"shared words","min_seq":seq}),
    )
    .await;
    // The a and a-b tie breaks on the path string: cards/a-b.md first.
    assert_eq!(
        ids(&before),
        vec![
            pair("fleet", "a-b"),
            pair("fleet", "a"),
            pair("repo:one", "b")
        ]
    );
    h.store.take();
    h.open(std::sync::Arc::new(super::scan::Unavailable), None);
    let after = search(&h, NODE_TOKEN, json!({"query":"shared words"})).await;
    assert_eq!(after.body, before.body);
    assert_eq!(index(&h).current().indexed_seq, seq);
}

#[tokio::test]
async fn search_validation_and_literal_queries() {
    let h = Hub::new();
    h.put(NODE_TOKEN, "fleet", "colons", None, "path::to -> x")
        .await;
    let seq = h.events();
    for body in [
        json!({}),
        json!({"query": 5}),
        json!({"query": " \t "}),
        json!({"query": "x".repeat(257)}),
        json!({"query": "x", "limit": 0}),
        json!({"query": "x", "limit": 21}),
        json!({"query": "x", "mode": "dense"}),
        json!({"query": "x", "query_source": "push"}),
        json!({"query": "x", "scopes": []}),
        json!({"query": "x", "scopes": ["Other"]}),
        json!({"query": "x", "sources": ["people"]}),
        json!({"query": "x", "min_seq": -1}),
        json!({"query": "x", "extra": 1}),
    ] {
        let r = search(&h, NODE_TOKEN, body.clone()).await;
        assert_eq!(
            (r.status, code(&r)),
            (400, "memory_query_invalid"),
            "{body}"
        );
    }
    // 256 characters after normalization is the bound, and newlines collapse.
    let long = format!("{}\n\n{}", "y".repeat(100), "z".repeat(155));
    let ok = search(&h, NODE_TOKEN, json!({"query": long})).await;
    assert_eq!(ok.status, 200);
    assert_eq!(ok.body["query"].as_str().unwrap().chars().count(), 256);
    let literal = search(&h, NODE_TOKEN, json!({"query":"::","min_seq":seq})).await;
    assert_eq!(ids(&literal), vec![pair("fleet", "colons")]);
    assert_eq!(literal.body["matches"][0]["score"], json!(0.0));
    // A message principal with no grant reads nothing.
    let none = search(&h, AGENT_TOKEN, json!({"query":"path"})).await;
    assert_eq!((none.status, code(&none)), (403, "memory_grant_missing"));
    let modes = search(
        &h,
        NODE_TOKEN,
        json!({"query":"path","mode":"hybrid","query_source":"prompt","sources":["automation"]}),
    )
    .await;
    assert_eq!(modes.body["retrieval"]["mode"], json!("lexical"));
}

/// Recorded for S13b: rebuild time and peak resident memory at 20,000
/// synthetic cards, retained snapshots included. Run alone:
/// `cargo test --release rebuild_time_and_peak -- --ignored --nocapture`.
#[cfg(target_os = "linux")]
#[test]
#[ignore]
fn rebuild_time_and_peak_memory_at_20000_cards() {
    fn status(key: &str) -> u64 {
        let text = std::fs::read_to_string("/proc/self/status").unwrap();
        let line = text.lines().find(|l| l.starts_with(key)).unwrap();
        line.split_whitespace().nth(1).unwrap().parse().unwrap()
    }
    let dir = super::tests::temp_dir();
    let path = dir.join("fleet-hub-memory.db");
    let db = rusqlite::Connection::open(&path).unwrap();
    super::schema::migrate(&db, 0).unwrap();
    db.execute_batch("BEGIN").unwrap();
    // A deterministic vocabulary of 8,000 words, about 1.5 KB per card.
    let mut seed: u64 = 0x2545_f491_4f6c_dd1d;
    let mut next = move || {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        seed
    };
    let mut bytes = 0;
    for n in 0..20_000 {
        let words: Vec<String> = (0..200).map(|_| format!("w{}", next() % 8000)).collect();
        let text = format!(
            "---\ntitle: Card {n}\ntags: [t{}, t{}]\n---\n{}\n",
            next() % 50,
            next() % 50,
            words.join(" ")
        );
        bytes += text.len();
        let hash = super::ids::sha256_hex(text.as_bytes());
        db.execute(
            "INSERT INTO memory_blobs VALUES(?1,?2,?3,'t') ON CONFLICT DO NOTHING",
            rusqlite::params![hash, text.as_bytes(), text.len() as i64],
        )
        .unwrap();
        db.execute(
            "INSERT INTO memory_cards VALUES('fleet',?1,?2,?3,'live','agent-unreviewed','node-1','node',NULL,NULL,NULL,'t','t')",
            rusqlite::params![format!("card-{n}"), n + 1, hash],
        )
        .unwrap();
    }
    db.execute_batch("COMMIT").unwrap();
    let _ = std::fs::write("/proc/self/clear_refs", "5");
    let before = status("VmRSS:");
    let started = Instant::now();
    let first = super::index::Index::open(&db, path.clone()).unwrap();
    let build = started.elapsed();
    let held = first.current();
    // A second full build while the first snapshot is still held, as a
    // background rebuild does.
    let started = Instant::now();
    let second = super::index::load(&db).unwrap().snapshot();
    let rebuild = started.elapsed();
    let peak = status("VmHWM:");
    let started = Instant::now();
    let allowed = vec![true];
    let hits = second.score(&allowed, &text::tokenize("w17 w4242 card"));
    let query = started.elapsed();
    println!(
        "cards=20000 text_mb={:.1} build_s={:.3} rebuild_s={:.3} rss_before_mb={:.1} peak_mb={:.1} peak_delta_mb={:.1} query_ms={:.2} hits={} held_docs={}",
        bytes as f64 / 1e6,
        build.as_secs_f64(),
        rebuild.as_secs_f64(),
        before as f64 / 1024.0,
        peak as f64 / 1024.0,
        (peak - before) as f64 / 1024.0,
        query.as_secs_f64() * 1000.0,
        hits.len(),
        held.documents()
    );
    drop((held, second, first));
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn table_lookup_agrees_with_every_code_point() {
    type Check = fn(char) -> bool;
    let tables: [(&[u32], Check); 2] = [
        (pytables::WORD, text::is_word),
        (pytables::PRINTABLE, text::is_printable),
    ];
    for (table, check) in tables {
        let mut expect = vec![false; 0x11_0000];
        for pair in table.chunks(2) {
            for cp in pair[0]..=pair[1] {
                expect[cp as usize] = true;
            }
        }
        for cp in 0..0x11_0000u32 {
            if let Some(c) = char::from_u32(cp) {
                assert_eq!(check(c), expect[cp as usize], "{cp:#x}");
            }
        }
    }
}

fn nested(depth: usize) -> String {
    format!("{}0{}", "[".repeat(depth), "]".repeat(depth))
}

#[test]
fn literal_nesting_stops_at_cpythons_limit() {
    use text::{Lit, MAX_NESTING, literal_eval};
    assert_eq!(MAX_NESTING, 200);
    assert!(matches!(literal_eval(&nested(200)), Some(Lit::List(_))));
    assert_eq!(literal_eval(&nested(201)), None);
    // Brackets and parentheses count together, as in the tokenizer.
    let mixed = format!("[{}1{}]", "(".repeat(199), ")".repeat(199));
    assert!(literal_eval(&mixed).is_some());
    let over = format!("[{}1{}]", "(".repeat(200), ")".repeat(200));
    assert_eq!(literal_eval(&over), None);
    // Far past the limit the parser stops at once instead of recursing.
    assert_eq!(literal_eval(&nested(200_000)), None);
    let card = format!("---\ntags: {}\n---\nbody\n", nested(200_000));
    let f = text::fields(&card, "deep");
    assert_eq!(f.tags.len(), 1);
    assert!(f.tags[0].starts_with("[[[["));
}

#[test]
fn front_matter_keys_keep_dict_order_and_parse_in_linear_time() {
    // A repeated key keeps its first position and takes the last value, like a Python dict.
    let data = text::front_matter("---\ntitle: a\ntags: [x]\ntitle: b\n---\nbody\n");
    let keys: Vec<&str> = data.iter().map(|(k, _)| k.as_str()).collect();
    assert_eq!(keys, vec!["title", "tags"]);
    assert!(matches!(&data[0].1, text::Value::Str(s) if s == "b"));
    // 80,000 distinct keys stay under the card size limit and must not parse quadratically.
    let mut card = String::from("---\n");
    for n in 0..80_000 {
        card.push_str(&format!("k{n}: v\n"));
    }
    card.push_str("title: last\n---\nbody\n");
    let started = std::time::Instant::now();
    let f = text::fields(&card, "wide");
    assert_eq!(f.title, "last");
    assert!(
        started.elapsed() < std::time::Duration::from_secs(10),
        "{:?}",
        started.elapsed()
    );
}

#[test]
fn radix_integers_convert_to_decimal_within_pythons_digit_limit() {
    use text::{Lit, literal_eval};
    let int = |s: &str| match literal_eval(s) {
        Some(Lit::List(items)) => items.into_iter().map(|i| i.repr()).collect::<Vec<_>>(),
        other => panic!("{s}: {other:?}"),
    };
    assert_eq!(
        int("[0x1F, 0o17, 0b101, 0X_ff, -0x10, -0x0, 0b_1_0, 0x00]"),
        ["31", "15", "5", "255", "-16", "0", "2", "0"]
    );
    assert_eq!(
        int("[0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF]"),
        ["340282366920938463463374607431768211455"]
    );
    for bad in [
        "[0x]", "[0b2]", "[0x1__2]", "[0x1_]", "[0x1e+5]", "[0o8]", "[0x_]",
    ] {
        assert_eq!(literal_eval(bad), None, "{bad}");
    }
    // 16**3571 - 1 has 4,300 digits; one more hex digit is past the limit,
    // which Python parses but cannot turn back into text.
    let at = format!("[0x{}]", "f".repeat(3571));
    assert_eq!(int(&at)[0].len(), 4300);
    let past = format!("[0x{}]", "f".repeat(3572));
    assert_eq!(literal_eval(&past), None);
    // A huge literal is refused by its size, before any conversion.
    let started = Instant::now();
    assert_eq!(literal_eval(&format!("[0b{}]", "1".repeat(400_000))), None);
    assert!(started.elapsed() < Duration::from_secs(1));
    let decimal = |n: usize| format!("[{}]", "1".repeat(n));
    assert!(literal_eval(&decimal(4300)).is_some());
    assert_eq!(literal_eval(&decimal(4301)), None);
}

/// Codex P1: a put whose front matter nests far past the limit committed,
/// then overflowed the writer's stack while indexing, and again at every
/// restart. It must index as raw text and survive a restart.
#[tokio::test]
async fn a_deeply_nested_card_indexes_and_survives_restart() {
    let mut h = Hub::new();
    let text = format!(
        "---\ntags: {}{}\n---\nsurvivor\n",
        "[".repeat(200_000),
        "]".repeat(200_000)
    );
    assert!(text.len() < super::ids::MAX_TEXT_BYTES);
    let put = h.put(NODE_TOKEN, "fleet", "deep", None, &text).await;
    assert_eq!(put.status, 201, "{:?}", put.body);
    let seq = put.body["event"]["seq"].as_i64().unwrap();
    let found = search(&h, NODE_TOKEN, json!({"query":"survivor","min_seq":seq})).await;
    assert_eq!(found.body["retrieval"]["stale"], json!(false));
    assert_eq!(ids(&found), vec![pair("fleet", "deep")]);
    let tags = found.body["matches"][0]["tags"][0].as_str().unwrap();
    assert_eq!(tags.len(), 400_000);
    h.store.take();
    h.open(std::sync::Arc::new(super::scan::Unavailable), None);
    let after = search(&h, NODE_TOKEN, json!({"query":"survivor"})).await;
    assert_eq!(after.body, found.body);
}

/// Claude #2: a literal query's work is bounded however large the corpus.
#[test]
fn literal_queries_read_a_bounded_part_of_a_large_corpus() {
    let cards = (0..20_000).map(|n| Card {
        scope: "fleet".into(),
        card_id: format!("card-{n:05}"),
        revision: n + 1,
        content_hash: format!("h{n}"),
        trust: "agent-unreviewed".into(),
        text: "path::to -> thing\n".into(),
    });
    let snap = Builder::from_cards(cards, 1).snapshot();
    let (mut reads, mut bytes) = (0, 0);
    let small = search::literal(&snap, &[true], "::", |_| {
        reads += 1;
        Ok(Some("path::to -> thing\n".into()))
    })
    .unwrap();
    assert_eq!(reads, search::LITERAL_DOCS);
    assert_eq!(small.hits.len(), search::LITERAL_DOCS);
    assert!(!small.complete);
    // Large cards stop the scan at the byte budget first.
    let big = "x -> y ".repeat(64 * 1024);
    reads = 0;
    let large = search::literal(&snap, &[true], "->", |_| {
        reads += 1;
        bytes += big.len();
        Ok(Some(big.clone()))
    })
    .unwrap();
    assert!(!large.complete);
    assert!(bytes < search::LITERAL_BYTES + big.len(), "{bytes}");
    assert!(reads < search::LITERAL_DOCS, "{reads}");
    // A corpus under both bounds is scanned in full.
    let few = Builder::from_cards(
        (0..10).map(|n| Card {
            scope: "fleet".into(),
            card_id: format!("c{n}"),
            revision: n + 1,
            content_hash: format!("h{n}"),
            trust: "agent-unreviewed".into(),
            text: "a :: b\n".into(),
        }),
        1,
    )
    .snapshot();
    let all = search::literal(&few, &[true], "::", |_| Ok(Some("a :: b\n".into()))).unwrap();
    assert!(all.complete);
    assert_eq!(all.hits.len(), 10);
}

#[tokio::test]
async fn a_truncated_literal_scan_is_reported() {
    let h = Hub::new();
    let mut last = 0;
    for n in 0..search::LITERAL_DOCS + 5 {
        let r = h
            .put(NODE_TOKEN, "fleet", &format!("c{n:04}"), None, "a :: b")
            .await;
        last = r.body["event"]["seq"].as_i64().unwrap();
    }
    let r = search(&h, NODE_TOKEN, json!({"query":"::","min_seq":last})).await;
    assert_eq!(r.body["retrieval"]["truncated"], json!(true));
    assert_eq!(
        r.body["match_count"].as_u64(),
        Some(search::LITERAL_DOCS as u64)
    );
    let words = search(&h, NODE_TOKEN, json!({"query":"a"})).await;
    assert!(words.body["retrieval"].get("truncated").is_none());
}

fn card(n: usize) -> Card {
    Card {
        scope: "fleet".into(),
        card_id: format!("card-{n:05}"),
        revision: n as i64 + 1,
        content_hash: format!("h{n}"),
        trust: "agent-unreviewed".into(),
        text: format!("word{n}\n"),
    }
}

/// Claude #4: many small commits and mostly-retire workloads also rebuild.
#[test]
fn rebuild_triggers_on_segment_count_and_dead_documents() {
    use super::index::{MAX_DELTA_SEGMENTS, REBUILD_FLOOR};
    // Segments: a large main segment, so the delta document share stays small.
    let mut b = Builder::from_cards((0..10_000).map(card), 1);
    for k in 0..MAX_DELTA_SEGMENTS {
        b.apply(vec![Change::Live(card(20_000 + k))], k as i64 + 2);
        assert!(!b.wants_rebuild(), "after {} deltas", k + 1);
    }
    b.apply(vec![Change::Live(card(30_000))], 100);
    assert_eq!(b.snapshot().segments(), MAX_DELTA_SEGMENTS + 2);
    assert!(b.wants_rebuild());
    // Dead documents: retirements add no segment and no delta document.
    let total = 4 * REBUILD_FLOOR;
    let mut b = Builder::from_cards((0..total).map(card), 1);
    let gone = |n: usize| Change::Gone {
        scope: "fleet".into(),
        card_id: format!("card-{n:05}"),
    };
    b.apply((0..total / 4).map(gone).collect(), 2);
    assert_eq!(b.snapshot().segments(), 1);
    assert!(!b.wants_rebuild(), "exactly a quarter dead");
    b.apply(vec![gone(total / 4)], 3);
    assert!(b.wants_rebuild());
    // A small index never rebuilds for its dead documents alone.
    let mut b = Builder::from_cards((0..10).map(card), 1);
    b.apply((0..9).map(gone).collect(), 2);
    assert!(!b.wants_rebuild());
}

#[tokio::test]
async fn many_small_commits_rebuild_past_the_segment_cap() {
    let h = Hub::new();
    let i = index(&h);
    let mut last = 0;
    for n in 0..super::index::MAX_DELTA_SEGMENTS + 4 {
        let r = h
            .put(
                NODE_TOKEN,
                "fleet",
                &format!("c{n:03}"),
                None,
                &format!("shared t{n}"),
            )
            .await;
        last = r.body["event"]["seq"].as_i64().unwrap();
    }
    until("rebuild", || i.metrics.lock().unwrap().rebuilds >= 1).await;
    let r = search(
        &h,
        NODE_TOKEN,
        json!({"query":"shared","min_seq":last,"limit":20}),
    )
    .await;
    assert_eq!(
        r.body["match_count"].as_u64(),
        Some(super::index::MAX_DELTA_SEGMENTS as u64 + 4)
    );
    assert!(i.current().segments() <= super::index::MAX_DELTA_SEGMENTS);
}

/// Claude #7: a failed rebuild retries on its own, so a retired card leaves
/// search with no further write.
#[tokio::test]
async fn a_failed_rebuild_retries_without_a_new_commit() {
    let h = Hub::new();
    let put = h
        .put(NODE_TOKEN, "fleet", "secret", None, "withdrawn words")
        .await;
    h.put(NODE_TOKEN, "fleet", "other", None, "other words")
        .await;
    let i = index(&h);
    let seq = h.events();
    until("publication", || i.current().indexed_seq >= seq).await;
    // The retire's publication fails, then the first two loads fail.
    i.fail_loads.store(2, std::sync::atomic::Ordering::SeqCst);
    i.fail.store(1, std::sync::atomic::Ordering::SeqCst);
    let epoch = h.epoch().await;
    let retired = h
        .call(
            NODE_TOKEN,
            json!({"action":"retire","scope":"fleet","card_id":"secret","base_revision":rev(&put),"base_epoch":epoch,"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(retired.status, 200, "{:?}", retired.body);
    let retired_seq = retired.body["event"]["seq"].as_i64().unwrap();
    until("rebuild after two failed loads", || {
        i.metrics.lock().unwrap().rebuilds == 1
    })
    .await;
    assert_eq!(i.metrics.lock().unwrap().rebuild_failures, 2);
    let r = search(&h, NODE_TOKEN, json!({"query":"withdrawn words"})).await;
    assert_eq!(r.body["retrieval"]["stale"], json!(false));
    assert_eq!(r.body["indexed_seq"].as_i64(), Some(retired_seq));
    assert_eq!(ids(&r), vec![pair("fleet", "other")]);
}

#[test]
fn rebuild_backoff_doubles_to_its_cap() {
    use super::index::{RETRY_FIRST, RETRY_MAX};
    let waits: Vec<Duration> = (1..=12).map(super::index::backoff).collect();
    assert_eq!(waits[0], RETRY_FIRST);
    assert_eq!(waits[1], RETRY_FIRST * 2);
    assert_eq!(waits[2], RETRY_FIRST * 4);
    assert_eq!(*waits.last().unwrap(), RETRY_MAX);
    assert!(waits.windows(2).all(|w| w[0] <= w[1]));
}

/// Claude #8: the raw guard counts bytes as sent, apart from the
/// 256-character limit after normalization.
#[tokio::test]
async fn the_raw_query_guard_is_bytes_and_apart_from_the_normalized_limit() {
    let h = Hub::new();
    h.put(NODE_TOKEN, "fleet", "c", None, "a b").await;
    let spaced = format!("a{}b", " ".repeat(search::RAW_QUERY_BYTES - 2));
    let ok = search(&h, NODE_TOKEN, json!({"query": spaced})).await;
    assert_eq!(ok.status, 200, "{:?}", ok.body);
    assert_eq!(ok.body["query"], json!("a b"));
    let over = format!("a{}b", " ".repeat(search::RAW_QUERY_BYTES - 1));
    let refused = search(&h, NODE_TOKEN, json!({"query": over})).await;
    assert_eq!(
        (refused.status, code(&refused)),
        (400, "memory_query_invalid")
    );
    let raw_message = refused.body["error"].as_str().unwrap().to_string();
    assert!(raw_message.contains("4096 bytes"), "{raw_message}");
    // 1,366 three-byte characters are under 4,096 characters but over 4,096 bytes.
    let wide = "\u{3042}".repeat(1366);
    let wide = search(&h, NODE_TOKEN, json!({"query": wide})).await;
    assert_eq!(wide.body["error"].as_str(), Some(raw_message.as_str()));
    let long = search(&h, NODE_TOKEN, json!({"query": "x".repeat(257)})).await;
    let normalized = long.body["error"].as_str().unwrap();
    assert!(normalized.contains("256 characters"), "{normalized}");
    assert_ne!(normalized, raw_message);
}
