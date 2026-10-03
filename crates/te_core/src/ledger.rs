//! The ledger model (docs/RUST_PORT.md P2a): the domain values, every event payload, the
//! codec that reads and writes the bytes the Python codec writes, and the fold. A
//! shadow: nothing in production calls it; `tests/test_ledger_codec_parity.py` and
//! `tests/test_ledger_fold_parity.py` hold it to the Python it will replace.

pub mod json;
pub mod model;
pub mod codec;
pub mod pydec;
pub mod ops;
pub mod mirror;
pub mod fold;
pub mod canon;
pub mod bridge;
