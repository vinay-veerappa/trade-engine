//! `te_wasm`: trade engine sim book compiled to wasm32 (docs/RUST_PORT.md P6).
//!
//! Wraps `te_core::sim::broker::Book` for browser and node replay environments.

use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub fn te_wasm_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}
