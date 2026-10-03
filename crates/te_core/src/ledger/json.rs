//! JSON as Python's `json` module reads and writes it, for the ledger's bytes.
//!
//! Reading follows `json.loads` (strict): the same literals including `NaN` and
//! `Infinity`, the same number grammar, control characters refused inside strings, an
//! object with a repeated key keeps the first position and the last value. Writing is
//! `json.dumps(sort_keys=True, separators=(",", ":"))` with the default
//! `ensure_ascii`. Floats are carried as text so they can be refused with the right
//! message; the codec never writes one.
//!
//! A string holding a lone UTF-16 surrogate (legal in Python, not in Rust) is refused
//! as unsupported rather than altered.

use std::fmt::Write;

#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i128),
    /// An integer beyond `i128`: carried as text.
    BigInt(String),
    /// A float, as text (`1.5`, `1e3`, `NaN`, `Infinity`).
    Float(String),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(pairs) => pairs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn type_name(&self) -> &'static str {
        match self {
            Json::Null => "NoneType",
            Json::Bool(_) => "bool",
            Json::Int(_) | Json::BigInt(_) => "int",
            Json::Float(_) => "float",
            Json::Str(_) => "str",
            Json::Arr(_) => "list",
            Json::Obj(_) => "dict",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JsonError {
    pub unsupported: bool,
    pub message: String,
}

fn err<T>(message: impl Into<String>) -> Result<T, JsonError> {
    Err(JsonError { unsupported: false, message: message.into() })
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
    depth: usize,
}

const MAX_DEPTH: usize = 400;

pub fn parse(text: &str) -> Result<Json, JsonError> {
    let mut p = Parser { s: text.as_bytes(), i: 0, depth: 0 };
    p.ws();
    let v = p.value()?;
    p.ws();
    if p.i != p.s.len() {
        return err(format!("Extra data: char {}", p.i));
    }
    Ok(v)
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn lit(&mut self, word: &str) -> bool {
        if self.s[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            true
        } else {
            false
        }
    }

    fn value(&mut self) -> Result<Json, JsonError> {
        let Some(&c) = self.s.get(self.i) else {
            return err("Expecting value");
        };
        match c {
            b'"' => self.string().map(Json::Str),
            b'{' => self.object(),
            b'[' => self.array(),
            b'n' if self.lit("null") => Ok(Json::Null),
            b't' if self.lit("true") => Ok(Json::Bool(true)),
            b'f' if self.lit("false") => Ok(Json::Bool(false)),
            b'N' if self.lit("NaN") => Ok(Json::Float("NaN".into())),
            b'I' if self.lit("Infinity") => Ok(Json::Float("Infinity".into())),
            b'-' if self.s[self.i..].starts_with(b"-Infinity") => {
                self.i += 9;
                Ok(Json::Float("-Infinity".into()))
            }
            b'-' | b'0'..=b'9' => self.number(),
            _ => err("Expecting value"),
        }
    }

    fn number(&mut self) -> Result<Json, JsonError> {
        let start = self.i;
        if self.s[self.i] == b'-' {
            self.i += 1;
        }
        match self.s.get(self.i) {
            Some(b'0') => self.i += 1,
            Some(b'1'..=b'9') => {
                while matches!(self.s.get(self.i), Some(b'0'..=b'9')) {
                    self.i += 1;
                }
            }
            _ => return err("Expecting value"),
        }
        let mut is_float = false;
        if self.s.get(self.i) == Some(&b'.') && matches!(self.s.get(self.i + 1), Some(b'0'..=b'9')) {
            is_float = true;
            self.i += 1;
            while matches!(self.s.get(self.i), Some(b'0'..=b'9')) {
                self.i += 1;
            }
        }
        if matches!(self.s.get(self.i), Some(b'e' | b'E')) {
            let mut j = self.i + 1;
            if matches!(self.s.get(j), Some(b'+' | b'-')) {
                j += 1;
            }
            if matches!(self.s.get(j), Some(b'0'..=b'9')) {
                while matches!(self.s.get(j), Some(b'0'..=b'9')) {
                    j += 1;
                }
                is_float = true;
                self.i = j;
            }
        }
        let text = std::str::from_utf8(&self.s[start..self.i]).expect("ascii");
        if is_float {
            return Ok(Json::Float(text.to_string()));
        }
        match text.parse::<i128>() {
            Ok(n) => Ok(Json::Int(n)),
            Err(_) => Ok(Json::BigInt(text.to_string())),
        }
    }

    fn hex4(&mut self) -> Result<u32, JsonError> {
        let Some(h) = self.s.get(self.i..self.i + 4) else {
            return err("Invalid \\uXXXX escape");
        };
        let mut v = 0u32;
        for &b in h {
            let d = (b as char).to_digit(16);
            match d {
                Some(d) => v = v * 16 + d,
                None => return err("Invalid \\uXXXX escape"),
            }
        }
        self.i += 4;
        Ok(v)
    }

    fn string(&mut self) -> Result<String, JsonError> {
        self.i += 1; // opening quote
        let mut out = String::new();
        loop {
            let Some(&b) = self.s.get(self.i) else {
                return err("Unterminated string");
            };
            match b {
                b'"' => {
                    self.i += 1;
                    return Ok(out);
                }
                b'\\' => {
                    self.i += 1;
                    let Some(&e) = self.s.get(self.i) else {
                        return err("Unterminated string");
                    };
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            let cp = if (0xD800..0xDC00).contains(&hi) && self.s[self.i..].starts_with(b"\\u") {
                                let save = self.i;
                                self.i += 2;
                                let lo = self.hex4()?;
                                if (0xDC00..0xE000).contains(&lo) {
                                    0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)
                                } else {
                                    self.i = save;
                                    hi
                                }
                            } else {
                                hi
                            };
                            match char::from_u32(cp) {
                                Some(c) => out.push(c),
                                None => {
                                    return Err(JsonError {
                                        unsupported: true,
                                        message: "a lone UTF-16 surrogate in a string".into(),
                                    })
                                }
                            }
                        }
                        _ => return err("Invalid \\escape"),
                    }
                }
                0..=0x1f => return err("Invalid control character"),
                _ => {
                    // copy one UTF-8 scalar
                    let rest = std::str::from_utf8(&self.s[self.i..]).map_err(|_| JsonError {
                        unsupported: false,
                        message: "invalid UTF-8".into(),
                    })?;
                    let c = rest.chars().next().expect("non-empty");
                    out.push(c);
                    self.i += c.len_utf8();
                }
            }
        }
    }

    fn array(&mut self) -> Result<Json, JsonError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return err("nesting too deep");
        }
        self.i += 1;
        self.ws();
        let mut items = Vec::new();
        if self.s.get(self.i) == Some(&b']') {
            self.i += 1;
            self.depth -= 1;
            return Ok(Json::Arr(items));
        }
        loop {
            self.ws();
            items.push(self.value()?);
            self.ws();
            match self.s.get(self.i) {
                Some(b',') => self.i += 1,
                Some(b']') => {
                    self.i += 1;
                    break;
                }
                _ => return err("Expecting ',' delimiter"),
            }
        }
        self.depth -= 1;
        Ok(Json::Arr(items))
    }

    fn object(&mut self) -> Result<Json, JsonError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return err("nesting too deep");
        }
        self.i += 1;
        self.ws();
        let mut pairs: Vec<(String, Json)> = Vec::new();
        if self.s.get(self.i) == Some(&b'}') {
            self.i += 1;
            self.depth -= 1;
            return Ok(Json::Obj(pairs));
        }
        loop {
            self.ws();
            if self.s.get(self.i) != Some(&b'"') {
                return err("Expecting property name enclosed in double quotes");
            }
            let key = self.string()?;
            self.ws();
            if self.s.get(self.i) != Some(&b':') {
                return err("Expecting ':' delimiter");
            }
            self.i += 1;
            self.ws();
            let v = self.value()?;
            match pairs.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = v,
                None => pairs.push((key, v)),
            }
            self.ws();
            match self.s.get(self.i) {
                Some(b',') => self.i += 1,
                Some(b'}') => {
                    self.i += 1;
                    break;
                }
                _ => return err("Expecting ',' delimiter"),
            }
        }
        self.depth -= 1;
        Ok(Json::Obj(pairs))
    }
}

fn write_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 || (c as u32) > 0x7e => {
                let cp = c as u32;
                if cp >= 0x10000 {
                    let v = cp - 0x10000;
                    let _ = write!(out, "\\u{:04x}\\u{:04x}", 0xd800 + (v >> 10), 0xdc00 + (v & 0x3ff));
                } else {
                    let _ = write!(out, "\\u{:04x}", cp);
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `json.dumps(v, sort_keys=True, separators=(",", ":"))`.
pub fn dumps(v: &Json) -> String {
    let mut out = String::new();
    write_json(&mut out, v);
    out
}

fn write_json(out: &mut String, v: &Json) {
    match v {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Int(n) => {
            let _ = write!(out, "{n}");
        }
        Json::BigInt(t) | Json::Float(t) => out.push_str(t),
        Json::Str(s) => write_str(out, s),
        Json::Arr(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_json(out, item);
            }
            out.push(']');
        }
        Json::Obj(pairs) => {
            let mut sorted: Vec<&(String, Json)> = pairs.iter().collect();
            sorted.sort_by(|a, b| a.0.cmp(&b.0));
            out.push('{');
            for (i, (k, val)) in sorted.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_str(out, k);
                out.push(':');
                write_json(out, val);
            }
            out.push('}');
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_sorted_compact_ascii() {
        let j = parse(r#" {"b": [1, true, null], "a": "x\u00e9\ud83d\ude00\n", "a": "y"} "#).unwrap();
        assert_eq!(dumps(&j), r#"{"a":"y","b":[1,true,null]}"#);
        let k = parse("\"\u{e9}\u{1f600}\u{7f}\"").unwrap();
        assert_eq!(dumps(&k), r#""\u00e9\ud83d\ude00\u007f""#);
    }

    #[test]
    fn refuses_what_python_refuses() {
        for bad in ["", "{", "[1,]", "01", "-", "{\"a\":1,}", "\"a\nb\"", "[1] 2", "{1:2}", "'a'", "+1", "1.", ".5"] {
            assert!(parse(bad).is_err(), "{bad:?}");
        }
        assert_eq!(parse("-Infinity").unwrap(), Json::Float("-Infinity".into()));
        assert_eq!(parse("123456789012345678901234567890123456789012").unwrap().type_name(), "int");
        assert!(parse("\"\\ud800\"").unwrap_err().unsupported);
    }
}
