//! `te_wasm`: trade engine sim book compiled to wasm32 (docs/RUST_PORT.md P6).
//!
//! Wraps `te_core::sim::broker::Book` for browser and node replay environments.
//! Keeps a thin `#[wasm_bindgen]` layer over the plain-Rust `api` module.

pub mod api;

use wasm_bindgen::prelude::*;

use crate::api::SimBookApi;

fn to_js_error(e: api::ApiError) -> wasm_bindgen::JsError {
    wasm_bindgen::JsError::new(&format!("{}: {}", e.kind, e.msg))
}

#[wasm_bindgen]
pub struct SimBook {
    inner: SimBookApi,
}

#[wasm_bindgen]
impl SimBook {
    #[wasm_bindgen(constructor)]
    pub fn new(
        account_id: &str,
        is_decimal: bool,
        slippage_bps: &str,
    ) -> Result<SimBook, wasm_bindgen::JsError> {
        SimBookApi::new(account_id, is_decimal, slippage_bps)
            .map(|inner| SimBook { inner })
            .map_err(to_js_error)
    }

    pub fn connect(&mut self, now_iso: &str) -> Result<String, wasm_bindgen::JsError> {
        self.inner.connect(now_iso).map_err(to_js_error)
    }

    pub fn restore(
        &mut self,
        orders_json: &str,
        fills_json: &str,
        positions_json: &str,
        now_iso: &str,
    ) -> Result<(), wasm_bindgen::JsError> {
        self.inner
            .restore(orders_json, fills_json, positions_json, now_iso)
            .map_err(to_js_error)
    }

    pub fn submit(
        &mut self,
        order_json: &str,
        now_iso: &str,
    ) -> Result<String, wasm_bindgen::JsError> {
        self.inner.submit(order_json, now_iso).map_err(to_js_error)
    }

    pub fn cancel(&mut self, id: &str, now_iso: &str) -> Result<String, wasm_bindgen::JsError> {
        self.inner.cancel(id, now_iso).map_err(to_js_error)
    }

    pub fn replace_begin(
        &mut self,
        id: &str,
        new_quantity: Option<String>,
        now_iso: &str,
    ) -> Result<String, wasm_bindgen::JsError> {
        self.inner
            .replace_begin(id, new_quantity.as_deref(), now_iso)
            .map_err(to_js_error)
    }

    pub fn replace_reject(
        &mut self,
        id: &str,
        msg: &str,
        now_iso: &str,
    ) -> Result<String, wasm_bindgen::JsError> {
        self.inner
            .replace_reject(id, msg, now_iso)
            .map_err(to_js_error)
    }

    pub fn replace_commit(
        &mut self,
        id: &str,
        order_json: &str,
        now_iso: &str,
    ) -> Result<String, wasm_bindgen::JsError> {
        self.inner
            .replace_commit(id, order_json, now_iso)
            .map_err(to_js_error)
    }

    pub fn orders_since(
        &mut self,
        since: &str,
        now_iso: &str,
    ) -> Result<String, wasm_bindgen::JsError> {
        self.inner.orders_since(since, now_iso).map_err(to_js_error)
    }

    pub fn orders(&mut self, since: &str, now_iso: &str) -> Result<String, wasm_bindgen::JsError> {
        self.inner.orders(since, now_iso).map_err(to_js_error)
    }

    pub fn fills_since(
        &mut self,
        since: &str,
        now_iso: &str,
    ) -> Result<String, wasm_bindgen::JsError> {
        self.inner.fills_since(since, now_iso).map_err(to_js_error)
    }

    pub fn fills(&mut self, since: &str, now_iso: &str) -> Result<String, wasm_bindgen::JsError> {
        self.inner.fills(since, now_iso).map_err(to_js_error)
    }

    pub fn positions(&mut self, now_iso: &str) -> Result<String, wasm_bindgen::JsError> {
        self.inner.positions(now_iso).map_err(to_js_error)
    }

    pub fn process_bar(
        &mut self,
        bar_json: Option<String>,
    ) -> Result<String, wasm_bindgen::JsError> {
        self.inner
            .process_bar(bar_json.as_deref())
            .map_err(to_js_error)
    }

    pub fn on_bar(&mut self, bar_json: Option<String>) -> Result<String, wasm_bindgen::JsError> {
        self.inner.on_bar(bar_json.as_deref()).map_err(to_js_error)
    }

    pub fn has(&self, id: &str) -> bool {
        self.inner.has(id)
    }

    pub fn fill_count(&self) -> usize {
        self.inner.fill_count()
    }

    pub fn fill(&self, i: usize) -> Result<String, wasm_bindgen::JsError> {
        self.inner.fill(i).map_err(to_js_error)
    }

    pub fn cash_events(&self, since: &str) -> Result<String, wasm_bindgen::JsError> {
        self.inner.cash_events(since).map_err(to_js_error)
    }
}

#[wasm_bindgen]
pub fn sim_instrument_key(text: &str) -> Result<String, wasm_bindgen::JsError> {
    api::sim_instrument_key(text).map_err(to_js_error)
}

#[wasm_bindgen]
pub fn trail_check_amount(trail_amount: &str) -> Result<(), wasm_bindgen::JsError> {
    api::trail_check_amount(trail_amount).map_err(to_js_error)
}

#[wasm_bindgen]
pub fn trail_update(
    side: &str,
    trail_amount: &str,
    state_json: &str,
    price: &str,
) -> Result<String, wasm_bindgen::JsError> {
    api::trail_update(side, trail_amount, state_json, price).map_err(to_js_error)
}
