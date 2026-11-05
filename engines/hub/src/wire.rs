//! Python-exact response bytes, string rules and clock shared by native families.
//! The Python hub is the oracle: `json.dumps` spacing and ASCII escapes,
//! `str.strip()`, `float.__repr__` and `datetime.fromtimestamp().isoformat()`.
use rusqlite::types::Value;

/// Python's `str.isspace()` for the characters `str.strip()` and `str.split()`
/// use: Rust's White_Space set plus U+001C..U+001F.
pub fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}
/// Python's str.strip().
pub fn py_trim(s: &str) -> &str {
    s.trim_matches(py_space)
}
/// A C0 or C1 control character, DEL included (`fleet_hub._contains_controls`).
pub fn controls(s: &str) -> bool {
    s.chars()
        .any(|c| c < '\u{20}' || ('\u{7f}'..='\u{9f}').contains(&c))
}
pub fn quote(s: &str) -> String {
    let encoded = serde_json::to_string(s).unwrap();
    let mut out = String::new();
    for c in encoded.chars() {
        // json.dumps(ensure_ascii=True) escapes everything outside ' '..'~', DEL included.
        if c.is_ascii() && c != '\x7f' {
            out.push(c);
        } else {
            let mut buf = [0; 2];
            for unit in c.encode_utf16(&mut buf) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}
pub fn object(fields: &[(&str, String)]) -> String {
    format!(
        "{{{}}}",
        fields
            .iter()
            .map(|(k, v)| format!("{}: {v}", quote(k)))
            .collect::<Vec<_>>()
            .join(", ")
    )
}
pub fn array(items: impl IntoIterator<Item = String>) -> String {
    format!("[{}]", items.into_iter().collect::<Vec<_>>().join(", "))
}
pub fn value(v: &Value) -> String {
    match v {
        Value::Text(s) => quote(s),
        Value::Null => "null".into(),
        _ => unreachable!(),
    }
}
pub fn text(s: &str) -> Value {
    Value::Text(s.into())
}
pub fn string(v: &Value) -> &str {
    if let Value::Text(s) = v { s } else { "" }
}
pub fn valid_node(s: &str) -> bool {
    s != "unknown"
        && !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}
/// `float.__repr__`: shortest round-trip digits, exponent outside [1e-4, 1e16).
pub fn py_float(f: f64) -> String {
    if f.is_nan() {
        return "NaN".into();
    }
    if f.is_infinite() {
        return if f > 0.0 { "Infinity" } else { "-Infinity" }.into();
    }
    let a = f.abs();
    if a != 0.0 && !(1e-4..1e16).contains(&a) {
        let formatted = format!("{f:e}");
        let (mantissa, exponent) = formatted.split_once('e').unwrap();
        let exponent: i32 = exponent.parse().unwrap();
        let sign = if exponent < 0 { '-' } else { '+' };
        return format!("{mantissa}e{sign}{:02}", exponent.abs());
    }
    let mut out = format!("{f}");
    if !out.contains('.') {
        out.push_str(".0");
    }
    out
}
/// `hub._now_epoch()`: `datetime.now(timezone.utc).timestamp()`. With the
/// acceptance clock, the file value is rounded to microseconds the way the
/// pure-Python datetime the Python upstream runs under does it.
pub fn epoch(clock: Option<&str>) -> f64 {
    epoch_us(clock) as f64 / 1e6
}
/// `epoch` in whole microseconds, the resolution `datetime` keeps.
pub fn epoch_us(clock: Option<&str>) -> i64 {
    let file = clock
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| s.trim().parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v >= 0.0 && *v < 253402300799.0);
    match file {
        Some(v) => {
            let (seconds, micros) = split(v);
            seconds * 1_000_000 + micros
        }
        None => {
            let d = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap();
            d.as_secs() as i64 * 1_000_000 + i64::from(d.subsec_micros())
        }
    }
}
/// `datetime.fromtimestamp`: modf, then round the fraction half to even.
fn split(v: f64) -> (i64, i64) {
    let whole = v.trunc();
    let mut micros = ((v - whole) * 1e6).round_ties_even() as i64;
    let mut seconds = whole as i64;
    if micros >= 1_000_000 {
        seconds += 1;
        micros -= 1_000_000;
    } else if micros < 0 {
        seconds -= 1;
        micros += 1_000_000;
    }
    (seconds, micros)
}
/// `datetime.fromtimestamp(v, timezone.utc).isoformat()`.
pub fn iso(v: f64) -> String {
    let (seconds, micros) = split(v);
    iso_parts(seconds, micros)
}
/// `isoformat()` of a UTC datetime held as microseconds since the epoch.
pub fn iso_us(us: i64) -> String {
    iso_parts(us.div_euclid(1_000_000), us.rem_euclid(1_000_000))
}
/// Days since 1970-01-01 for a proleptic Gregorian date (Hinnant's days_from_civil).
pub fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (month + 9) % 12;
    let doy = (153 * mp + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}
fn iso_parts(seconds: i64, micros: i64) -> String {
    let days = seconds.div_euclid(86400);
    let rest = seconds.rem_euclid(86400);
    // Howard Hinnant's civil_from_days.
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    let fraction = if micros == 0 {
        String::new()
    } else {
        format!(".{micros:06}")
    };
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}{fraction}+00:00",
        rest / 3600,
        rest / 60 % 60,
        rest % 60
    )
}
pub fn now(clock: Option<&str>) -> String {
    iso(epoch(clock))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn clock_epoch_carries_rounded_microseconds_into_seconds() {
        let dir = std::env::temp_dir().join(format!("hub-clock-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let clock = dir.join("clock");
        let at = |value: &str| {
            std::fs::write(&clock, value).unwrap();
            now(Some(clock.to_str().unwrap()))
        };
        // Python: datetime.fromtimestamp(1.9999996, timezone.utc).isoformat()
        assert_eq!(at("1.9999996"), "1970-01-01T00:00:02+00:00");
        assert_eq!(at("1.25"), "1970-01-01T00:00:01.250000+00:00");
        assert_eq!(at("1700000000"), "2023-11-14T22:13:20+00:00");
        std::fs::write(&clock, "1767225600.25").unwrap();
        assert_eq!(epoch(Some(clock.to_str().unwrap())), 1767225600.25);
        let _ = std::fs::remove_dir_all(&dir);
    }
    #[test]
    fn iso_matches_python_calendar() {
        assert_eq!(iso(0.0), "1970-01-01T00:00:00+00:00");
        assert_eq!(iso(951782400.5), "2000-02-29T00:00:00.500000+00:00");
        assert_eq!(iso(1767225600.0 + 900.0), "2026-01-01T00:15:00+00:00");
        assert_eq!(iso(4102444799.999999), "2099-12-31T23:59:59.999999+00:00");
        assert_eq!(iso_us(-1), "1969-12-31T23:59:59.999999+00:00");
        assert_eq!(days_from_civil(2000, 2, 29) * 86400, 951782400);
        assert_eq!(days_from_civil(1970, 1, 1), 0);
    }
    #[test]
    fn python_float_repr() {
        assert_eq!(py_float(1767226500.0), "1767226500.0");
        assert_eq!(py_float(1767226500.25), "1767226500.25");
        assert_eq!(py_float(0.1), "0.1");
        assert_eq!(py_float(1e16), "1e+16");
        assert_eq!(py_float(1.5e-5), "1.5e-05");
        assert_eq!(py_float(0.0001), "0.0001");
        assert_eq!(py_float(9999999999999998.0), "9999999999999998.0");
    }
    #[test]
    fn python_strip_controls_and_ascii_escapes() {
        assert_eq!(py_trim("\u{1c}n1\u{1f} "), "n1");
        assert_eq!(py_trim("\u{85}x\u{3000}"), "x");
        assert_eq!(quote("a\u{7f}b"), r#""a\u007fb""#);
        assert!(controls("a\u{9f}") && controls("\t") && !controls("é"));
        assert_eq!(
            object(&[("label", quote("café 🎉"))]),
            r#"{"label": "caf\u00e9 \ud83c\udf89"}"#
        );
    }
}
