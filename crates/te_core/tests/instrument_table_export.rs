//! BP-4c: the instrument table export that riskguard reads (design 6.1 / D3).
//!
//! `data/futures_instruments.json` is generated from the one `FUTURE_SPECS` table, never
//! edited by hand. To regenerate after a table change:
//!
//! ```text
//! TE_REGEN_INSTRUMENTS=1 cargo test -p te_core --test instrument_table_export
//! ```
//!
//! then copy the file to `nt8-riskguard/rust/crates/riskguard-core/data/bp1_instruments.json`
//! (its `tools/check_bp1_instrument_table.py` compares the two byte for byte).

use std::path::PathBuf;

use te_core::futures_symbols::{instrument_export_json, INSTRUMENT_EXPORT_SCHEMA};
use te_core::sim::tick::FUTURE_SPECS;

fn committed_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data").join("futures_instruments.json")
}

/// Git may check the file out with CRLF (`core.autocrlf=true`); the index holds LF.
fn lf(text: &str) -> String {
    text.replace("\r\n", "\n")
}

/// The generator: regenerates the committed file on request, otherwise fails when the
/// committed file is stale.
#[test]
fn committed_instrument_file_is_current() {
    let fresh = instrument_export_json();
    let path = committed_path();
    if std::env::var_os("TE_REGEN_INSTRUMENTS").is_some() {
        std::fs::write(&path, &fresh).expect("write the instrument export");
    }
    let committed = std::fs::read_to_string(&path).unwrap_or_else(|e| {
        panic!("{} is missing ({e}); run with TE_REGEN_INSTRUMENTS=1 to generate it", path.display())
    });
    assert_eq!(
        lf(&committed),
        fresh,
        "data/futures_instruments.json is stale against FUTURE_SPECS; regenerate with \
         TE_REGEN_INSTRUMENTS=1 and copy it to nt8-riskguard"
    );
}

/// The file parses, carries every root once in table order, and the numbers equal the table.
#[test]
fn instrument_file_matches_the_table_row_for_row() {
    let text = std::fs::read_to_string(committed_path()).expect("read the committed export");
    let doc: serde_json::Value = serde_json::from_str(&text).expect("valid JSON");
    assert_eq!(doc["schema"], INSTRUMENT_EXPORT_SCHEMA);
    let rows = doc["instruments"].as_array().expect("instruments array");
    assert_eq!(rows.len(), FUTURE_SPECS.len(), "one row per root");
    for (row, spec) in rows.iter().zip(FUTURE_SPECS.iter()) {
        assert_eq!(row["root"], spec.root);
        assert_eq!(row["tick_size"], spec.tick_size_str);
        assert_eq!(row["tick_value"], spec.tick_value_str);
        assert_eq!(row["point_value"], spec.point_value_str);
        assert!(row["tick_value"].is_string() && row["tick_size"].is_string(), "D6: strings");
    }
}

/// The export is deterministic, LF-only and ends with one newline (so the byte comparison
/// riskguard makes is stable).
#[test]
fn export_text_is_lf_with_one_trailing_newline() {
    let text = instrument_export_json();
    assert!(!text.contains('\r'));
    assert!(text.ends_with("}\n") && !text.ends_with("\n\n"));
    assert_eq!(text, instrument_export_json());
}

/// MET is a riskguard-only root (owner decision, 2026-11-11): it must stay out of BP-1.
/// riskguard's overlay test fails the day this list gains MET, which forces the overlay's removal.
#[test]
fn met_is_not_in_the_bp1_table() {
    assert!(FUTURE_SPECS.iter().all(|s| !s.root.eq_ignore_ascii_case("MET")));
}
