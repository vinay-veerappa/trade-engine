# `te_wasm`: Trade Engine SimBook compiled to WebAssembly (P6)

This crate compiles the engine's core deterministic simulation book (`te_core::sim::broker::Book`) and trailing stop emulator (`te_core::sim::trailing`) to `wasm32-unknown-unknown` for use in browser replay engines (`web/engine`) and Node.js testing environments (`replay-sim`).

## Architecture

- **`api.rs`**: Plain-Rust logic layer where all functions take and return `String` / `Result<String, ApiError>`. Money crosses as decimal strings (D6), timestamps as ISO-8601 UTC strings, and complex payloads (orders, bars, fills, positions) as JSON strings. This layer is fully testable natively without wasm dependencies.
- **`lib.rs`**: Thin `#[wasm_bindgen]` wrapper exposing the `SimBook` class, `sim_instrument_key`, `trail_check_amount`, and `trail_update`. Refusals are thrown as JavaScript `Error` objects with message `"<kind>: <message>"`, matching the exception kinds used by `te_py`.
- **`tests/parity.rs`**: 250 seeded parity walks (16,920 steps) driving identical operation sequences into `te_core::sim::broker::Book` directly and `te_wasm::api::SimBookApi`, verifying identical return values and identical refusal kind and message at every step.

## Build and Smoke Test Commands

### 1. Build release Wasm binary
```bash
cargo build --manifest-path crates/Cargo.toml -p te_wasm --target wasm32-unknown-unknown --release
```

### 2. Generate Node.js bindings with wasm-bindgen
```bash
wasm-bindgen --target nodejs --out-dir crates/te_wasm/pkg crates/target/wasm32-unknown-unknown/release/te_wasm.wasm
```

### 3. Run Node smoke test
```bash
node crates/te_wasm/smoke/smoke.mjs
```

### 4. Run native parity tests
```bash
cargo test --manifest-path crates/Cargo.toml -p te_wasm
```

## Exposed JS API

```typescript
export class SimBook {
    free(): void;
    constructor(account_id: string, is_decimal: boolean, slippage_bps: string);
    connect(now_iso: string): string;
    restore(orders_json: string, fills_json: string, positions_json: string, now_iso: string): void;
    submit(order_json: string, now_iso: string): string;
    cancel(id: string, now_iso: string): string;
    replace_begin(id: string, new_quantity: string | null | undefined, now_iso: string): string;
    replace_reject(id: string, msg: string, now_iso: string): string;
    replace_commit(id: string, order_json: string, now_iso: string): string;
    orders(since: string, now_iso: string): string;
    orders_since(since: string, now_iso: string): string;
    fills(since: string, now_iso: string): string;
    fills_since(since: string, now_iso: string): string;
    positions(now_iso: string): string;
    process_bar(bar_json?: string | null): string;
    on_bar(bar_json?: string | null): string;
    has(id: string): boolean;
    fill_count(): number;
    fill(i: number): string;
    cash_events(since: string): string;
}

export function sim_instrument_key(text: string): string;
export function trail_check_amount(trail_amount: string): void;
export function trail_update(side: string, trail_amount: string, state_json: string, price: string): string;
```
