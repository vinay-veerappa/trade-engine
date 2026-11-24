//! The ledger model (docs/RUST_PORT.md P2a): the domain values, every event payload, the
//! codec that reads and writes the bytes the Python codec writes, and the fold. This is
//! the production ledger (P7, 2026-10-07); `tests/test_ledger_codec_parity.py` and
//! `tests/test_fold_behaviour.py` pin its bytes and fold results with explicit values.

pub mod json;
pub mod model;
pub mod codec;
#[cfg(test)]
pub mod pydec;
pub mod ops;
pub mod mirror;
pub mod fold;
pub mod canon;
pub mod bridge;
