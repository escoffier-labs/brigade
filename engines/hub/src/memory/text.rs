//! The local scorer's text rules, copied from Python (S13b). Python is the
//! oracle: `str.lower()`, the `re` `\w+` tokenizer, `str.isprintable()` and
//! `memory_cmd._card_search_fields` with its front-matter reader. The Unicode
//! classes come from `pytables`, which `tests/lexical_golden.py` writes from
//! Python itself, so Rust's own Unicode version never decides a token.
use super::pytables as t;
use crate::wire::{py_float, py_space, py_trim};

fn in_ranges(table: &[u32], c: char) -> bool {
    let c = c as u32;
    // Inclusive (start, end) pairs. An odd count of values below `c` puts it
    // after a start and at or before that start's end; an even count leaves
    // it in only when it is the next start itself.
    let below = table.partition_point(|v| *v < c);
    below % 2 == 1 || table.get(below) == Some(&c)
}
/// Python's `\w` for one character.
pub fn is_word(c: char) -> bool {
    in_ranges(t::WORD, c)
}
/// Python's `str.isprintable()` for one character.
pub fn is_printable(c: char) -> bool {
    in_ranges(t::PRINTABLE, c)
}
fn lower_char(c: char, out: &mut String) {
    let cp = c as u32;
    let pairs = t::LOWER.len() / 2;
    let (mut lo, mut hi) = (0, pairs);
    while lo < hi {
        let mid = (lo + hi) / 2;
        match t::LOWER[mid * 2].cmp(&cp) {
            std::cmp::Ordering::Less => lo = mid + 1,
            std::cmp::Ordering::Greater => hi = mid,
            std::cmp::Ordering::Equal => {
                out.push(char::from_u32(t::LOWER[mid * 2 + 1]).unwrap_or(c));
                return;
            }
        }
    }
    match t::LOWER_MULTI.iter().find(|(from, _)| *from == cp) {
        Some((_, to)) => out.push_str(to),
        None => out.push(c),
    }
}
/// Python's `str.lower()`, final sigma included (CPython `handle_capital_sigma`).
pub fn py_lower(s: &str) -> String {
    let chars: Vec<char> = s.chars().collect();
    let mut out = String::with_capacity(s.len());
    for (i, &c) in chars.iter().enumerate() {
        if c != '\u{3a3}' {
            lower_char(c, &mut out);
            continue;
        }
        let ignorable = |c: char| in_ranges(t::CASE_IGNORABLE, c);
        let cased = |c: char| in_ranges(t::CASED, c);
        let before = chars[..i].iter().rev().find(|c| !ignorable(**c));
        let after = chars[i + 1..].iter().find(|c| !ignorable(**c));
        let last = before.is_some_and(|c| cased(*c)) && !after.is_some_and(|c| cased(*c));
        out.push(if last { '\u{3c2}' } else { '\u{3c3}' });
    }
    out
}
/// `memory_bm25.tokenize`: lowercase, then every `\w+` run.
pub fn tokenize(text: &str) -> Vec<String> {
    let lower = py_lower(text);
    let mut out = Vec::new();
    let mut current = String::new();
    for c in lower.chars() {
        if is_word(c) {
            current.push(c);
        } else if !current.is_empty() {
            out.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        out.push(current);
    }
    out
}
/// `str.split()` with no argument.
pub fn py_split(s: &str) -> Vec<&str> {
    s.split(py_space).filter(|p| !p.is_empty()).collect()
}
/// `memory_hooks._single_line`: non-printable characters become spaces, then
/// whitespace runs collapse to one space and the ends are trimmed.
pub fn single_line(s: &str) -> String {
    let printable: String = s
        .chars()
        .map(|c| if is_printable(c) { c } else { ' ' })
        .collect();
    py_split(&printable).join(" ")
}
/// `str.splitlines()`.
pub fn py_splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut iter = s.char_indices().peekable();
    while let Some((i, c)) = iter.next() {
        let boundary = matches!(
            c,
            '\n' | '\r'
                | '\u{b}'
                | '\u{c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if !boundary {
            continue;
        }
        out.push(&s[start..i]);
        let mut end = i + c.len_utf8();
        if c == '\r' && iter.peek().map(|(_, n)| *n) == Some('\n') {
            iter.next();
            end += 1;
        }
        start = end;
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// A value `ast.literal_eval` accepts inside a front-matter list.
#[derive(Clone, Debug, PartialEq)]
pub enum Lit {
    Str(String),
    Int(String),
    Float(f64),
    Bool(bool),
    None,
    List(Vec<Lit>),
    Tuple(Vec<Lit>),
}
/// A front-matter value as `memory_cmd._parse_frontmatter` stores it.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Str(String),
    Bool(bool),
    /// A list, or a tuple when the text was `[a], [b]`.
    Lit(Lit),
}
impl Value {
    fn truthy(&self) -> bool {
        match self {
            Value::Str(s) => !s.is_empty(),
            Value::Bool(b) => *b,
            Value::Lit(Lit::List(items) | Lit::Tuple(items)) => !items.is_empty(),
            Value::Lit(_) => true,
        }
    }
    /// `str(value)`.
    fn py_str(&self) -> String {
        match self {
            Value::Str(s) => s.clone(),
            Value::Bool(b) => py_bool(*b).into(),
            Value::Lit(lit) => lit.repr(),
        }
    }
}
fn py_bool(b: bool) -> &'static str {
    if b { "True" } else { "False" }
}
/// `repr(str)`.
fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c if c.is_ascii() || is_printable(c) => out.push(c),
            c if (c as u32) < 0x100 => out.push_str(&format!("\\x{:02x}", c as u32)),
            c if (c as u32) < 0x10000 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push_str(&format!("\\U{:08x}", c as u32)),
        }
    }
    out.push(quote);
    out
}
fn float_repr(f: f64) -> String {
    if f.is_infinite() {
        return if f > 0.0 { "inf" } else { "-inf" }.into();
    }
    py_float(f)
}
impl Lit {
    /// `repr(value)`.
    pub fn repr(&self) -> String {
        match self {
            Lit::Str(s) => repr_str(s),
            Lit::Int(digits) => digits.clone(),
            Lit::Float(f) => float_repr(*f),
            Lit::Bool(b) => py_bool(*b).into(),
            Lit::None => "None".into(),
            Lit::List(items) => format!(
                "[{}]",
                items.iter().map(Lit::repr).collect::<Vec<_>>().join(", ")
            ),
            Lit::Tuple(items) if items.len() == 1 => format!("({},)", items[0].repr()),
            Lit::Tuple(items) => format!(
                "({})",
                items.iter().map(Lit::repr).collect::<Vec<_>>().join(", ")
            ),
        }
    }
    /// `str(value)`: a string is itself, everything else its repr.
    pub fn py_str(&self) -> String {
        match self {
            Lit::Str(s) => s.clone(),
            other => other.repr(),
        }
    }
}

/// The subset of `ast.literal_eval` front-matter lists use: strings (with
/// the `r` and `u` prefixes, escapes and implicit concatenation), decimal
/// integers and floats with one sign, `True`, `False`, `None`, lists and
/// tuples. Anything else, dicts and sets included, returns `None`, and the
/// caller keeps the raw text as Python does when `literal_eval` raises.
pub fn literal_eval(source: &str) -> Option<Lit> {
    let chars: Vec<char> = source.chars().collect();
    let mut p = Parser { s: &chars, i: 0 };
    let first = p.value()?;
    p.space();
    if p.peek() != Some(',') {
        return (p.i == chars.len()).then_some(first);
    }
    // `a, b` is a tuple, and so is `a,`.
    let mut items = vec![first];
    while p.peek() == Some(',') {
        p.i += 1;
        p.space();
        if p.i == chars.len() {
            break;
        }
        items.push(p.value()?);
        p.space();
    }
    (p.i == chars.len()).then_some(Lit::Tuple(items))
}
struct Parser<'a> {
    s: &'a [char],
    i: usize,
}
impl Parser<'_> {
    fn peek(&self) -> Option<char> {
        self.s.get(self.i).copied()
    }
    fn space(&mut self) {
        while matches!(self.peek(), Some(' ' | '\t' | '\u{c}')) {
            self.i += 1;
        }
    }
    fn value(&mut self) -> Option<Lit> {
        self.space();
        match self.peek()? {
            '[' => {
                self.i += 1;
                Some(Lit::List(self.items(']')?.0))
            }
            '(' => {
                self.i += 1;
                let (items, comma) = self.items(')')?;
                // `(x)` is just `x`; a tuple needs a comma or nothing at all.
                if items.len() == 1 && !comma {
                    items.into_iter().next()
                } else {
                    Some(Lit::Tuple(items))
                }
            }
            '+' | '-' => {
                let negative = self.peek() == Some('-');
                self.i += 1;
                self.space();
                match self.number()? {
                    Lit::Int(d) if negative && d != "0" => Some(Lit::Int(format!("-{d}"))),
                    Lit::Float(f) if negative => Some(Lit::Float(-f)),
                    n => Some(n),
                }
            }
            c if c.is_ascii_digit() || c == '.' => self.number(),
            '\'' | '"' => self.strings(),
            c if c.is_alphabetic() || c == '_' => {
                let start = self.i;
                while self.peek().is_some_and(|c| c.is_alphanumeric() || c == '_') {
                    self.i += 1;
                }
                let word: String = self.s[start..self.i].iter().collect();
                match word.as_str() {
                    "True" => Some(Lit::Bool(true)),
                    "False" => Some(Lit::Bool(false)),
                    "None" => Some(Lit::None),
                    "r" | "R" | "u" | "U" if matches!(self.peek(), Some('\'' | '"')) => {
                        self.i = start;
                        self.strings()
                    }
                    _ => None,
                }
            }
            _ => None,
        }
    }
    /// Items up to `close`, and whether a comma followed the last one.
    fn items(&mut self, close: char) -> Option<(Vec<Lit>, bool)> {
        let mut items = Vec::new();
        let mut comma = false;
        loop {
            self.space();
            if self.peek()? == close {
                self.i += 1;
                return Some((items, comma));
            }
            if !items.is_empty() && !comma {
                return None;
            }
            items.push(self.value()?);
            self.space();
            comma = self.peek() == Some(',');
            if comma {
                self.i += 1;
            }
        }
    }
    fn number(&mut self) -> Option<Lit> {
        let start = self.i;
        while self
            .peek()
            .is_some_and(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_'))
            || (matches!(self.peek(), Some('+' | '-'))
                && matches!(self.s.get(self.i - 1), Some('e' | 'E')))
        {
            self.i += 1;
        }
        let raw: String = self.s[start..self.i].iter().collect();
        if raw.is_empty() {
            return None;
        }
        // Underscores only between digits.
        let bytes = raw.as_bytes();
        for (k, b) in bytes.iter().enumerate() {
            if *b == b'_'
                && !(k > 0
                    && bytes[k - 1].is_ascii_digit()
                    && bytes.get(k + 1).is_some_and(u8::is_ascii_digit))
            {
                return None;
            }
        }
        let clean = raw.replace('_', "");
        if clean.bytes().all(|b| b.is_ascii_digit()) {
            // Leading zeros are a syntax error unless every digit is zero.
            if clean.len() > 1 && clean.starts_with('0') {
                return clean
                    .bytes()
                    .all(|b| b == b'0')
                    .then(|| Lit::Int("0".into()));
            }
            return Some(Lit::Int(clean));
        }
        let shape = clean
            .bytes()
            .all(|b| b.is_ascii_digit() || matches!(b, b'.' | b'e' | b'E' | b'+' | b'-'));
        let mantissa = clean.split(['e', 'E']).next().unwrap_or("");
        if !shape
            || mantissa.matches('.').count() > 1
            || !mantissa.bytes().any(|b| b.is_ascii_digit())
        {
            return None;
        }
        clean.parse::<f64>().ok().map(Lit::Float)
    }
    fn strings(&mut self) -> Option<Lit> {
        let mut out = String::new();
        loop {
            let mut raw = false;
            if let Some(prefix @ ('r' | 'R' | 'u' | 'U')) = self.peek() {
                raw = matches!(prefix, 'r' | 'R');
                self.i += 1;
            }
            let quote = self.peek()?;
            if !matches!(quote, '\'' | '"') {
                return None;
            }
            // Triple quotes are not in the subset.
            if self.s.get(self.i + 1) == Some(&quote) && self.s.get(self.i + 2) == Some(&quote) {
                return None;
            }
            self.i += 1;
            loop {
                let c = self.peek()?;
                self.i += 1;
                if c == quote {
                    break;
                }
                if c != '\\' {
                    out.push(c);
                    continue;
                }
                let next = self.peek()?;
                self.i += 1;
                if raw {
                    out.push('\\');
                    out.push(next);
                    continue;
                }
                match next {
                    '\\' | '\'' | '"' => out.push(next),
                    'a' => out.push('\u{7}'),
                    'b' => out.push('\u{8}'),
                    'f' => out.push('\u{c}'),
                    'n' => out.push('\n'),
                    'r' => out.push('\r'),
                    't' => out.push('\t'),
                    'v' => out.push('\u{b}'),
                    '0'..='7' => {
                        let mut value = next.to_digit(8)?;
                        for _ in 0..2 {
                            match self.peek().and_then(|c| c.to_digit(8)) {
                                Some(d) => {
                                    value = value * 8 + d;
                                    self.i += 1;
                                }
                                None => break,
                            }
                        }
                        out.push(char::from_u32(value)?);
                    }
                    'x' | 'u' | 'U' => {
                        let width = match next {
                            'x' => 2,
                            'u' => 4,
                            _ => 8,
                        };
                        let digits: String = self.s.get(self.i..self.i + width)?.iter().collect();
                        if !digits.chars().all(|c| c.is_ascii_hexdigit()) {
                            return None;
                        }
                        self.i += width;
                        // A lone surrogate is a valid Python str but not a Rust one.
                        out.push(char::from_u32(u32::from_str_radix(&digits, 16).ok()?)?);
                    }
                    // `\N{name}` needs the Unicode name table.
                    'N' => return None,
                    other => {
                        out.push('\\');
                        out.push(other);
                    }
                }
            }
            self.space();
            if !matches!(self.peek(), Some('\'' | '"' | 'r' | 'R' | 'u' | 'U')) {
                return Some(Lit::Str(out));
            }
            // A following letter must start another string literal.
            if matches!(self.peek(), Some('r' | 'R' | 'u' | 'U'))
                && !matches!(self.s.get(self.i + 1), Some('\'' | '"'))
            {
                return None;
            }
        }
    }
}

/// `memory_cmd._parse_frontmatter`: keys in order, the last duplicate wins.
pub fn front_matter(text: &str) -> Vec<(String, Value)> {
    let mut data: Vec<(String, Value)> = Vec::new();
    if !text.starts_with("---\n") {
        return data;
    }
    let lines = py_splitlines(text);
    let Some(end) = (1..lines.len()).find(|i| py_trim(lines[*i]) == "---") else {
        return data;
    };
    for raw in &lines[1..end] {
        let Some((key, value)) = raw.split_once(':') else {
            continue;
        };
        let (key, value) = (py_trim(key), py_trim(value));
        if key.is_empty() {
            continue;
        }
        let parsed = if value.starts_with('[') && value.ends_with(']') {
            match literal_eval(value) {
                Some(lit @ (Lit::List(_) | Lit::Tuple(_))) => Value::Lit(lit),
                _ => Value::Str(value.into()),
            }
        } else if matches!(py_lower(value).as_str(), "true" | "false") {
            Value::Bool(py_lower(value) == "true")
        } else {
            Value::Str(value.trim_matches(['\'', '"']).into())
        };
        match data.iter_mut().find(|(k, _)| k == key) {
            Some(slot) => slot.1 = parsed,
            None => data.push((key.into(), parsed)),
        }
    }
    data
}

/// The searchable fields of one card stored as `cards/<card_id>.md`.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Fields {
    pub title: String,
    pub tags: Vec<String>,
    pub summary: String,
}
/// `memory_cmd._card_search_fields` for `cards/<card_id>.md`, whose stem is the card id.
pub fn fields(text: &str, card_id: &str) -> Fields {
    let data = front_matter(text);
    let get = |key: &str| data.iter().find(|(k, _)| k == key).map(|(_, v)| v);
    let first_truthy = |keys: &[&str]| {
        keys.iter()
            .filter_map(|k| get(k))
            .find(|v| v.truthy())
            .map(Value::py_str)
    };
    let tags = match get("tags") {
        Some(Value::Lit(Lit::List(items))) => items.iter().map(Lit::py_str).collect(),
        Some(v) if v.truthy() => vec![v.py_str()],
        _ => Vec::new(),
    };
    Fields {
        title: first_truthy(&["title"]).unwrap_or_else(|| card_id.into()),
        tags,
        summary: first_truthy(&["description", "summary"]).unwrap_or_default(),
    }
}
/// `memory_bm25.card_text`: the single BM25 field.
pub fn card_text(fields: &Fields, body: &str) -> String {
    format!(
        "{}\n{}\n{}\n{}",
        fields.title,
        fields.tags.join(" "),
        fields.summary,
        body
    )
}
/// Python's `round(x, 4)`: the correctly rounded decimal, read back.
pub fn round4(x: f64) -> f64 {
    format!("{x:.4}").parse().unwrap_or(x)
}
