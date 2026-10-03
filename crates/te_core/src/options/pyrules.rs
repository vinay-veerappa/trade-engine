//! The two pieces of CPython text behaviour the pre-port Python rules leaned on and
//! that Rust's `str` does not share: `str.strip()` / `\s` (the `str.isspace` set) and
//! `\d` (every Unicode decimal digit, category Nd). Both are pinned here, from
//! CPython 3.13's `unicodedata` (Unicode 15.1), and tested against it by
//! `tests/test_option_rules_parity.py`.

/// `str.isspace()` for one character. Rust's `char::is_whitespace` differs only in
/// U+001C..U+001F, which Python counts as whitespace.
pub fn is_space(c: char) -> bool {
    matches!(
        c,
        '\u{9}'..='\u{d}'
            | '\u{1c}'..='\u{1f}'
            | ' '
            | '\u{85}'
            | '\u{a0}'
            | '\u{1680}'
            | '\u{2000}'..='\u{200a}'
            | '\u{2028}'
            | '\u{2029}'
            | '\u{202f}'
            | '\u{205f}'
            | '\u{3000}'
    )
}

/// `str.strip()` with no argument.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// First code point of every run of ten Unicode decimal digits (Nd), ascending.
const ND_STARTS: [u32; 68] = [
    0x30, 0x660, 0x6F0, 0x7C0, 0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66, 0xDE6, 0xE50,
    0xED0, 0xF20, 0x1040, 0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90, 0x1B50, 0x1BB0, 0x1C40,
    0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0, 0xAA50, 0xABF0, 0xFF10, 0x104A0, 0x10D30, 0x11066,
    0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0, 0x11650, 0x116C0, 0x11730, 0x118E0, 0x11950,
    0x11C50, 0x11D50, 0x11DA0, 0x11F50, 0x16A60, 0x16AC0, 0x16B50, 0x1D7CE, 0x1D7D8, 0x1D7E2, 0x1D7EC,
    0x1D7F6, 0x1E140, 0x1E2F0, 0x1E4F0, 0x1E950, 0x1FBF0,
];

/// The decimal value of `c` when it is a Unicode decimal digit (what `\d` matches and
/// `int()` reads), else `None`.
pub fn digit(c: char) -> Option<u32> {
    let cp = c as u32;
    let i = ND_STARTS.partition_point(|&s| s <= cp);
    if i == 0 {
        return None;
    }
    let off = cp - ND_STARTS[i - 1];
    (off < 10).then_some(off)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn space_set_is_pythons() {
        assert!(is_space('\u{1c}') && is_space('\u{a0}') && is_space('\u{3000}'));
        assert!(!is_space('\u{200b}') && !is_space('a') && !is_space('0'));
        assert_eq!(strip("\u{1f} AAPL\u{a0}"), "AAPL");
    }

    #[test]
    fn digits_cover_ascii_and_other_scripts() {
        assert_eq!(digit('7'), Some(7));
        assert_eq!(digit('\u{666}'), Some(6)); // Arabic-Indic six
        assert_eq!(digit('\u{ff19}'), Some(9)); // fullwidth nine
        assert_eq!(digit('\u{b2}'), None); // superscript two is not decimal
        assert_eq!(digit('a'), None);
    }
}
