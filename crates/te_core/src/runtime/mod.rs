//! Runtime decisions only. All observations and effects belong to the host.
pub mod eod;
pub mod eod_glue;
pub mod eod_flow;
pub mod intraday;
pub mod intraday_flow;
pub mod intraday_glue;
pub mod routing;
pub mod lifecycle;
pub mod lifecycle_glue;
pub mod journal;

/// Python's `str.strip()` with no argument: Rust's Unicode whitespace plus U+001C..U+001F.
pub(crate) fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}
