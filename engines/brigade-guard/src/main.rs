use pcre2::bytes::Regex;
use serde::{Deserialize, Serialize};
use std::io;

const PROTOCOL: u32 = 1;

/// `ascii` is the #2212 contract and stays the default, so a request without
/// `mode` is answered exactly as before. `utf` turns on PCRE2 UTF and UCP so
/// `\d`, `\s`, `\w`, `\b` and caseless matching use Unicode properties.
#[derive(Deserialize, Default, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
enum Mode {
    #[default]
    Ascii,
    Utf,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Rule {
    id: String,
    pattern: String,
    flags: u32,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    protocol: u32,
    #[serde(default)]
    mode: Mode,
    text: String,
    rules: Vec<Rule>,
}

#[derive(Serialize)]
struct Span {
    byte_start: usize,
    byte_end: usize,
    start: usize,
    end: usize,
}

#[derive(Serialize)]
struct Matches {
    id: String,
    spans: Vec<Span>,
}

#[derive(Serialize)]
struct Engine {
    pcre2_version: String,
    unicode_version: String,
}

#[derive(Serialize)]
struct Response {
    protocol: u32,
    /// Reported in UTF mode only, so ASCII responses keep their exact bytes.
    #[serde(skip_serializing_if = "Option::is_none")]
    engine: Option<Engine>,
    rules: Vec<Matches>,
}

/// Read a string from the linked library, not the compile-time header, so a
/// system PCRE2 reports its own version.
fn pcre2_config_string(what: u32) -> Result<String, Box<dyn std::error::Error>> {
    brigade_guard::config_string(what)
}

fn engine() -> Result<Engine, Box<dyn std::error::Error>> {
    Ok(Engine {
        pcre2_version: pcre2_config_string(pcre2_sys::PCRE2_CONFIG_VERSION)?,
        unicode_version: pcre2_config_string(pcre2_sys::PCRE2_CONFIG_UNICODE_VERSION)?,
    })
}

/// UTF mode spans in both units. Offsets advance monotonically, so character
/// offsets are counted once across the subject. An empty match is refused:
/// PCRE2 iteration would step one byte into a multibyte character, and no
/// default rule can match empty.
fn utf_spans(regex: &Regex, text: &str) -> Result<Vec<Span>, Box<dyn std::error::Error>> {
    let mut spans = Vec::new();
    let (mut position, mut counted_byte, mut counted_chars) = (0, 0, 0);
    while position <= text.len() {
        let Some(found) = regex.find_at(text.as_bytes(), position)? else {
            break;
        };
        if found.start() == found.end() {
            return Err("empty match in UTF mode".into());
        }
        let start = counted_chars + text[counted_byte..found.start()].chars().count();
        let end = start + text[found.start()..found.end()].chars().count();
        spans.push(Span {
            byte_start: found.start(),
            byte_end: found.end(),
            start,
            end,
        });
        (position, counted_byte, counted_chars) = (found.end(), found.end(), end);
    }
    Ok(spans)
}

fn scan(request: Request) -> Result<Response, Box<dyn std::error::Error>> {
    if request.protocol != PROTOCOL {
        return Err("unsupported guard protocol".into());
    }
    let utf = request.mode == Mode::Utf;
    // ASCII mode keeps #2212's contract: Python owns non-ASCII subjects there,
    // and byte and character offsets coincide.
    if !utf && !request.text.is_ascii() {
        return Err("Unicode requires UTF mode or Python matching".into());
    }
    // Reported so a caller can compare PCRE2's Unicode database with its own.
    let engine = if utf { Some(engine()?) } else { None };
    let mut rules = Vec::new();
    for rule in request.rules {
        let regex = brigade_guard::compile(&rule.pattern, rule.flags, utf)?;
        if utf {
            let spans = utf_spans(&regex, &request.text)?;
            rules.push(Matches { id: rule.id, spans });
            continue;
        }
        let mut spans = Vec::new();
        for found in regex.find_iter(request.text.as_bytes()) {
            let found = found?;
            if found.start() != found.end() {
                spans.push(Span {
                    byte_start: found.start(),
                    byte_end: found.end(),
                    start: found.start(),
                    end: found.end(),
                });
            }
        }
        rules.push(Matches { id: rule.id, spans });
    }
    Ok(Response {
        protocol: PROTOCOL,
        engine,
        rules,
    })
}

fn main() {
    let result = (|| -> Result<(), Box<dyn std::error::Error>> {
        let request = serde_json::from_reader(io::stdin().lock())?;
        let response = scan(request)?;
        serde_json::to_writer(io::stdout().lock(), &response)?;
        Ok(())
    })();
    if result.is_err() {
        // Never echo input, patterns, or matched private material.
        eprintln!("brigade-guard: native matching failed");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lookbehind_named_backreference_and_conditional() {
        let result = scan(Request {
            protocol: PROTOCOL,
            mode: Mode::Ascii,
            text: "'abc' abc".into(),
            rules: vec![Rule {
                id: "custom".into(),
                pattern: r"(?<=(?P<q>'))abc(?(q)(?=(?P=q))|$)".into(),
                flags: 0,
            }],
        })
        .unwrap();
        assert_eq!(result.rules[0].spans.len(), 1);
        assert_eq!(result.rules[0].spans[0].start, 1);
        assert_eq!(result.rules[0].spans[0].byte_end, 4);
    }

    #[test]
    fn rejects_unknown_contract_and_unicode() {
        for (protocol, text) in [(2, "abc"), (1, "é")] {
            assert!(
                scan(Request {
                    protocol,
                    mode: Mode::Ascii,
                    text: text.into(),
                    rules: vec![]
                })
                .is_err()
            );
        }
    }

    /// The pcre2-sys release this crate pins vendors PCRE2 10.46. Bump these
    /// together with the `=` pins in Cargo.toml and the version in README.md.
    const PINNED_PCRE2_VERSION: &str = "10.46 2025-08-27";
    const PINNED_UNICODE_VERSION: &str = "16.0.0";

    #[test]
    fn linked_pcre2_is_the_pinned_bundled_release() {
        // Fails when a build links a system libpcre2 (the root
        // .cargo/config.toml forces the vendored copy) or when a crate bump
        // changes the vendored release without updating the pin above.
        let engine = engine().unwrap();
        assert_eq!(engine.pcre2_version, PINNED_PCRE2_VERSION);
        assert_eq!(engine.unicode_version, PINNED_UNICODE_VERSION);
    }

    fn request(json: &str) -> Request {
        serde_json::from_str(json).unwrap()
    }

    #[test]
    fn ascii_requests_and_responses_keep_the_2212_shape() {
        let rules = r#"[{"id":"lh","pattern":"local\\w+","flags":2}]"#;
        let implicit = format!(r#"{{"protocol":1,"text":"a LOCALhost b","rules":{rules}}}"#);
        let explicit =
            format!(r#"{{"protocol":1,"mode":"ascii","text":"a LOCALhost b","rules":{rules}}}"#);
        let implicit = serde_json::to_string(&scan(request(&implicit)).unwrap()).unwrap();
        let explicit = serde_json::to_string(&scan(request(&explicit)).unwrap()).unwrap();
        assert_eq!(implicit, explicit);
        assert_eq!(
            implicit,
            r#"{"protocol":1,"rules":[{"id":"lh","spans":[{"byte_start":2,"byte_end":11,"start":2,"end":11}]}]}"#
        );
        let unknown = r#"{"protocol":1,"mode":"latin1","text":"a","rules":[]}"#;
        assert!(serde_json::from_str::<Request>(unknown).is_err());
    }

    #[test]
    fn utf_mode_counts_characters_and_uses_unicode_classes() {
        // \w and \d follow Unicode properties; astral characters are one
        // character but four bytes.
        let result = scan(request(
            r#"{"protocol":1,"mode":"utf","text":"😀 é1 λ٣ x","rules":[{"id":"w","pattern":"\\w+","flags":0},{"id":"d","pattern":"\\d","flags":0}]}"#,
        ))
        .unwrap();
        let words: Vec<_> = result.rules[0]
            .spans
            .iter()
            .map(|s| (s.start, s.end, s.byte_start, s.byte_end))
            .collect();
        assert_eq!(words, vec![(2, 4, 5, 8), (5, 7, 9, 13), (8, 9, 14, 15)]);
        let digits: Vec<_> = result.rules[1]
            .spans
            .iter()
            .map(|s| (s.start, s.end))
            .collect();
        assert_eq!(digits, vec![(3, 4), (6, 7)]);
        let engine = result.engine.unwrap();
        assert!(engine.pcre2_version.starts_with("10."));
        assert_eq!(engine.unicode_version.split('.').count(), 3);
    }

    #[test]
    fn utf_mode_refuses_empty_matches_and_the_ascii_flag() {
        for rule in [
            r#"{"id":"e","pattern":"x*","flags":0}"#,
            r#"{"id":"a","pattern":"x","flags":256}"#,
        ] {
            let json = format!(r#"{{"protocol":1,"mode":"utf","text":"é x","rules":[{rule}]}}"#);
            assert!(scan(request(&json)).is_err());
        }
    }
}
