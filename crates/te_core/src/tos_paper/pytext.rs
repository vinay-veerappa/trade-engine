//! Python's text behaviours the decisions lean on: `repr`/`str` of a JSON value (floats
//! included), truthiness, `str.isdigit`, `str.isprintable`, `strip().upper()`. Messages the
//! host compares byte for byte are built from these, so they must read as Python reads.

use super::pytables::{IS_DIGIT, NOT_PRINTABLE};
use crate::ledger::json::Json;
use crate::options::pyrules::strip;

fn in_ranges(table: &[(u32, u32)], cp: u32) -> bool {
    table
        .binary_search_by(|&(lo, hi)| {
            if cp < lo {
                std::cmp::Ordering::Greater
            } else if cp > hi {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// `c.isprintable()`.
pub fn is_printable(c: char) -> bool {
    !in_ranges(&NOT_PRINTABLE, c as u32)
}

/// `c.isdigit()`.
pub fn is_digit(c: char) -> bool {
    in_ranges(&IS_DIGIT, c as u32)
}

/// `s.isdigit()`: non-empty and every character a digit.
pub fn str_isdigit(s: &str) -> bool {
    !s.is_empty() && s.chars().all(is_digit)
}

/// `s.strip().upper()`.
pub fn strip_upper(s: &str) -> String {
    strip(s).to_uppercase()
}

/// `repr(s)` for a `str`.
pub fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        let cp = c as u32;
        match c {
            _ if c == quote || c == '\\' => {
                out.push('\\');
                out.push(c);
            }
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            _ if cp < 0x20 || cp == 0x7f => out.push_str(&format!("\\x{cp:02x}")),
            _ if cp < 0x7f => out.push(c),
            _ if is_printable(c) => out.push(c),
            _ if cp <= 0xff => out.push_str(&format!("\\x{cp:02x}")),
            _ if cp <= 0xffff => out.push_str(&format!("\\u{cp:04x}")),
            _ => out.push_str(&format!("\\U{cp:08x}")),
        }
    }
    out.push(quote);
    out
}

/// `repr(float)` of a JSON float token (`1.5`, `1e3`, `NaN`, `-Infinity`).
pub fn repr_float(text: &str) -> String {
    match text {
        "NaN" => return "nan".into(),
        "Infinity" => return "inf".into(),
        "-Infinity" => return "-inf".into(),
        _ => {}
    }
    let v: f64 = text.parse().unwrap_or(f64::NAN);
    if v.is_nan() {
        return "nan".into();
    }
    let sign = if v.is_sign_negative() { "-" } else { "" };
    if v.is_infinite() {
        return format!("{sign}inf");
    }
    if v == 0.0 {
        return format!("{sign}0.0");
    }
    // Shortest round-trip digits and exponent, from Rust's `{:e}` ("1.2345e-5").
    let sci = format!("{:e}", v.abs());
    let (mant, exp) = sci.split_once('e').expect("exponent form");
    let exp: i32 = exp.parse().expect("exponent");
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let decpt = exp + 1; // value = 0.DIGITS x 10^decpt
    let n = digits.len() as i32;
    let body = if decpt > 16 || decpt < -3 {
        let frac = if n > 1 { format!(".{}", &digits[1..]) } else { String::new() };
        let e = decpt - 1;
        format!("{}{}e{}{:02}", &digits[..1], frac, if e < 0 { '-' } else { '+' }, e.abs())
    } else if decpt <= 0 {
        format!("0.{}{}", "0".repeat((-decpt) as usize), digits)
    } else if decpt >= n {
        format!("{}{}.0", digits, "0".repeat((decpt - n) as usize))
    } else {
        format!("{}.{}", &digits[..decpt as usize], &digits[decpt as usize..])
    };
    format!("{sign}{body}")
}

/// `repr(value)` of a JSON value read as the Python object `json.loads` would build.
pub fn repr(j: &Json) -> String {
    match j {
        Json::Null => "None".into(),
        Json::Bool(true) => "True".into(),
        Json::Bool(false) => "False".into(),
        Json::Int(n) => n.to_string(),
        Json::BigInt(t) => t.clone(),
        Json::Float(t) => repr_float(t),
        Json::Str(s) => repr_str(s),
        Json::Arr(items) => format!("[{}]", items.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Json::Obj(pairs) => format!(
            "{{{}}}",
            pairs.iter().map(|(k, v)| format!("{}: {}", repr_str(k), repr(v))).collect::<Vec<_>>().join(", ")
        ),
    }
}

/// `str(value)`.
pub fn to_str(j: &Json) -> String {
    match j {
        Json::Str(s) => s.clone(),
        other => repr(other),
    }
}

/// `bool(value)`.
pub fn truthy(j: &Json) -> bool {
    match j {
        Json::Null => false,
        Json::Bool(b) => *b,
        Json::Int(n) => *n != 0,
        Json::BigInt(_) => true,
        Json::Float(t) => t.parse::<f64>().map_or(true, |v| v != 0.0),
        Json::Str(s) => !s.is_empty(),
        Json::Arr(a) => !a.is_empty(),
        Json::Obj(o) => !o.is_empty(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ledger::json::parse;

    fn r(text: &str) -> String {
        repr(&parse(text).unwrap())
    }

    #[test]
    fn repr_reads_as_python_does() {
        assert_eq!(
            r(r#"{"a": [1, null, true, "x'y", 1.0, 1e16, 1e-5, 0.0001, 123456789012345678]}"#),
            r#"{'a': [1, None, True, "x'y", 1.0, 1e+16, 1e-05, 0.0001, 123456789012345678]}"#
        );
        assert_eq!(r(r#""a\"b'c""#), r#"'a"b\'c'"#);
        assert_eq!(r(r#""\u0001\u00e9\u200b\ud83d\ude00\u00ad""#), r"'\x01é\u200b😀\xad'");
        assert_eq!(r("-0.0"), "-0.0");
        assert_eq!(r("1e400"), "inf");
        assert_eq!(r("1e15"), "1000000000000000.0");
        assert_eq!(r("123.456"), "123.456");
        assert_eq!(r("1.5e-7"), "1.5e-07");
    }

    #[test]
    fn truthiness_and_text_tests() {
        assert!(!truthy(&parse("0.0").unwrap()) && !truthy(&parse("[]").unwrap()) && truthy(&parse("\"0\"").unwrap()));
        assert!(str_isdigit("5403527317") && str_isdigit("\u{b2}") && !str_isdigit("") && !str_isdigit("12x"));
        assert!(!str_isdigit("\u{bd}"));
        assert_eq!(strip_upper("  open\t"), "OPEN");
    }
}
