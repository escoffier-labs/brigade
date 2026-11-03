use pcre2::bytes::RegexBuilder;
use serde::{Deserialize, Serialize};
use std::io;

const PROTOCOL: u32 = 1;

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
struct Response {
    protocol: u32,
    rules: Vec<Matches>,
}

fn scan(request: Request) -> Result<Response, Box<dyn std::error::Error>> {
    if request.protocol != PROTOCOL {
        return Err("unsupported guard protocol".into());
    }
    // Python owns Unicode matching. ASCII has identical byte/character units.
    if !request.text.is_ascii() {
        return Err("Unicode requires Python matching".into());
    }
    let mut rules = Vec::new();
    for rule in request.rules {
        // Python flags: IGNORECASE=2, MULTILINE=8, DOTALL=16, UNICODE=32,
        // VERBOSE=64, ASCII=256. Reject all unknown flags.
        if rule.flags & !(2 | 8 | 16 | 32 | 64 | 256) != 0 || !rule.pattern.is_ascii() {
            return Err("unsupported Python regex flags or pattern".into());
        }
        let regex = RegexBuilder::new()
            .caseless(rule.flags & 2 != 0)
            .multi_line(rule.flags & 8 != 0)
            .dotall(rule.flags & 16 != 0)
            .extended(rule.flags & 64 != 0)
            .jit(true)
            .build(&rule.pattern)?;
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
                    text: text.into(),
                    rules: vec![]
                })
                .is_err()
            );
        }
    }
}
