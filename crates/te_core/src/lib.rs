//! Trade engine core (docs/RUST_PORT.md). Pure Rust: no Python, and no clock
//! read anywhere (I7) -- time always arrives as an argument.

pub mod calendar;
pub mod greeks;
pub mod ledger;
pub mod oms;
pub mod margin;
pub mod options;
pub mod risk;
pub mod risk_options;
pub mod sim;
