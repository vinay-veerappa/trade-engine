import assert from "node:assert";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const pkgPath = path.resolve(__dirname, "../pkg/te_wasm.js");
const wasm = await import(pathToFileURL(pkgPath).href);
const { SimBook } = wasm;

console.log("Running node smoke test with SimBook...");

const book = new SimBook("SMOKE_ACC", true, "0");
const connected_at = book.connect("2026-03-02T14:29:00+00:00");

// 1. Submit Entry: Buy 100 AAPL @ Limit 150.00
const entryOrder = {
  id: "entry_1",
  instr: "AAPL",
  otype: "LIMIT",
  side: "BUY",
  quantity: "100",
  submitted_at: "2026-03-02T14:29:30+00:00",
  tif: "DAY",
  limit: "150.00",
  stop: null,
  trail: null,
  allocs: [{ soid: "s1", account: "SMOKE_ACC", qty: "100" }],
  parent: null,
  oco: null,
};
const entry_ack = JSON.parse(book.submit(JSON.stringify(entryOrder), "2026-03-02T14:29:30+00:00"));

// 2. Bar 1 fills entry @ 150.00
const bar1 = {
  instr: "AAPL",
  ts: "2026-03-02T14:30:00+00:00",
  open: "150.50",
  high: "151.00",
  low: "149.50",
  close: "150.00",
  volume: "1000",
  as_of: "2026-03-02T14:31:00+00:00",
};
const bar1_fills = JSON.parse(book.process_bar(JSON.stringify(bar1)));
assert.deepStrictEqual(bar1_fills, [0]);

// 3. Submit Target: Sell 100 AAPL @ Limit 155.00
const targetOrder = {
  id: "target_1",
  instr: "AAPL",
  otype: "LIMIT",
  side: "SELL",
  quantity: "100",
  submitted_at: "2026-03-02T14:30:30+00:00",
  tif: "DAY",
  limit: "155.00",
  stop: null,
  trail: null,
  allocs: [{ soid: "s1:target:1", account: "SMOKE_ACC", qty: "100" }],
  parent: "entry_1",
  oco: "entry_1:oco",
};
const target_ack = JSON.parse(book.submit(JSON.stringify(targetOrder), "2026-03-02T14:30:30+00:00"));

// 4. Submit Stop: Sell 100 AAPL @ Stop 145.00
const stopOrder = {
  id: "stop_1",
  instr: "AAPL",
  otype: "STOP",
  side: "SELL",
  quantity: "100",
  submitted_at: "2026-03-02T14:30:30+00:00",
  tif: "DAY",
  limit: null,
  stop: "145.00",
  trail: null,
  allocs: [{ soid: "s1:stop", account: "SMOKE_ACC", qty: "100" }],
  parent: "entry_1",
  oco: "entry_1:oco",
};
const stop_ack = JSON.parse(book.submit(JSON.stringify(stopOrder), "2026-03-02T14:30:30+00:00"));

// 5. Bar 2: between stop and target
const bar2 = {
  instr: "AAPL",
  ts: "2026-03-02T14:31:00+00:00",
  open: "150.00",
  high: "152.00",
  low: "149.80",
  close: "151.50",
  volume: "1000",
  as_of: "2026-03-02T14:32:00+00:00",
};
const bar2_fills = JSON.parse(book.process_bar(JSON.stringify(bar2)));
assert.deepStrictEqual(bar2_fills, []);

// 6. Bar 3: fills target @ 155.00
const bar3 = {
  instr: "AAPL",
  ts: "2026-03-02T14:32:00+00:00",
  open: "152.00",
  high: "156.00",
  low: "151.00",
  close: "155.50",
  volume: "1000",
  as_of: "2026-03-02T14:33:00+00:00",
};
const bar3_fills = JSON.parse(book.process_bar(JSON.stringify(bar3)));
assert.deepStrictEqual(bar3_fills, [1]);

// 7. Query positions
const positions = JSON.parse(book.positions("2026-03-02T14:33:00+00:00"));

// 8. Query orders
const orders = JSON.parse(book.orders_since("2026-03-02T14:29:00+00:00", "2026-03-02T14:33:00+00:00"));

// 9. Query fills
const fillsIndices = JSON.parse(book.fills_since("2026-03-02T14:29:00+00:00", "2026-03-02T14:33:00+00:00"));
assert.deepStrictEqual(fillsIndices, [0, 1]);

const fill0 = JSON.parse(book.fill(0));
const fill1 = JSON.parse(book.fill(1));

const actual = {
  bar1_fills,
  bar2_fills,
  bar3_fills,
  connected_at,
  entry_ack,
  fills: [fill0, fill1],
  orders,
  positions,
  stop_ack,
  target_ack,
};

// Compare with golden.json
const goldenPath = path.resolve(__dirname, "golden.json");
const goldenText = fs.readFileSync(goldenPath, "utf-8");
const golden = JSON.parse(goldenText);

assert.deepStrictEqual(actual, golden, "Smoke test output must match golden.json");

// 10. Verify refusal contract: "<kind>: <message>"
assert.throws(
  () => book.submit(JSON.stringify({ ...entryOrder, id: "entry_1", quantity: "999" }), "2026-03-02T14:33:00+00:00"),
  (err) => {
    assert.match(err.message, /^sim: venue_order_id 'entry_1' was reused with different terms$/);
    return true;
  },
  "Reused order id must throw JS error with 'sim: ...' message"
);

// 11. Verify helpers: sim_instrument_key, trail_check_amount, trail_update
const { trail_check_amount, trail_update, sim_instrument_key } = wasm;
assert.strictEqual(sim_instrument_key("AAPL"), "E\u0001AAPL");
trail_check_amount("2.5");
assert.throws(
  () => trail_check_amount("-1"),
  (err) => {
    assert.match(err.message, /^value: trail_amount must be positive$/);
    return true;
  },
  "Negative trail amount must throw JS error with 'value: ...' message"
);

const trailRes = JSON.parse(
  trail_update(
    "SELL",
    "2.00",
    JSON.stringify({ extreme: null, stop_price: null, triggered: false }),
    "100.00"
  )
);
assert.strictEqual(trailRes.triggered, false);
assert.strictEqual(trailRes.state.extreme, "100.00");
assert.strictEqual(trailRes.state.stop_price, "98.00");

// 12. Futures scenario: MNQ stop entry with 1-tick slippage, then a target
console.log("Running futures smoke test with SimBook.newFutures...");
const futBook = SimBook.newFutures("SMOKE_FUT", 1);
const fut_connected_at = futBook.connect("2026-03-01T22:59:00+00:00");
assert.strictEqual(fut_connected_at, "2026-03-01T22:59:00+00:00");

// 12.1 Submit Entry: Buy 1 MNQ @ Stop 18000.00
const futEntryOrder = {
  id: "fut_entry_1",
  instr: "MNQ",
  otype: "STOP",
  side: "BUY",
  quantity: "1",
  submitted_at: "2026-03-01T22:59:30+00:00",
  tif: "DAY",
  limit: null,
  stop: "18000.00",
  trail: null,
  allocs: [{ soid: "fe1", account: "SMOKE_FUT", qty: "1" }],
  parent: null,
  oco: null,
};
const fut_entry_ack = JSON.parse(futBook.submit(JSON.stringify(futEntryOrder), "2026-03-01T22:59:30+00:00"));
assert.strictEqual(fut_entry_ack.status, "ACCEPTED");

// 12.2 Bar 1: Triggers buy stop @ 18000.00. 1-tick (0.25) slippage adverse -> 18000.25
const futBar1 = {
  instr: "MNQ",
  ts: "2026-03-01T23:00:00+00:00",
  open: "17990.00",
  high: "18010.00",
  low: "17985.00",
  close: "18005.00",
  volume: "500",
  as_of: "2026-03-01T23:01:00+00:00",
};
const futBar1_fills = JSON.parse(futBook.process_bar(JSON.stringify(futBar1)));
assert.deepStrictEqual(futBar1_fills, [0]);

const futFill0 = JSON.parse(futBook.fill(0));
assert.strictEqual(futFill0.price, "18000.25");
assert.strictEqual(futFill0.quantity, "1");
assert.strictEqual(futFill0.side, "BUY");
assert.strictEqual(futFill0.symbol, "MNQ");

// 12.3 Query positions: carries point_value (2) and tick_size (0.25)
const futPositions1 = JSON.parse(futBook.positions("2026-03-01T23:01:00+00:00"));
assert.strictEqual(futPositions1.length, 1);
assert.strictEqual(futPositions1[0].symbol, "MNQ");
assert.strictEqual(futPositions1[0].quantity, "1");
assert.strictEqual(futPositions1[0].avg_price, "18000.25");
assert.strictEqual(futPositions1[0].point_value, "2");
assert.strictEqual(futPositions1[0].tick_size, "0.25");

// 12.4 Submit Target: Sell 1 MNQ @ Limit 18020.00
const futTargetOrder = {
  id: "fut_target_1",
  instr: "MNQ",
  otype: "LIMIT",
  side: "SELL",
  quantity: "1",
  submitted_at: "2026-03-01T23:00:30+00:00",
  tif: "DAY",
  limit: "18020.00",
  stop: null,
  trail: null,
  allocs: [{ soid: "fe1:target:1", account: "SMOKE_FUT", qty: "1" }],
  parent: "fut_entry_1",
  oco: "fut_entry_1:oco",
};
const fut_target_ack = JSON.parse(futBook.submit(JSON.stringify(futTargetOrder), "2026-03-01T23:00:30+00:00"));
assert.strictEqual(fut_target_ack.status, "ACCEPTED");

// 12.5 Bar 2: Fills target limit @ 18020.00 without slippage
const futBar2 = {
  instr: "MNQ",
  ts: "2026-03-01T23:01:00+00:00",
  open: "18005.00",
  high: "18025.00",
  low: "18000.00",
  close: "18020.00",
  volume: "500",
  as_of: "2026-03-01T23:02:00+00:00",
};
const futBar2_fills = JSON.parse(futBook.process_bar(JSON.stringify(futBar2)));
assert.deepStrictEqual(futBar2_fills, [1]);

const futFill1 = JSON.parse(futBook.fill(1));
assert.strictEqual(futFill1.price, "18020.00");
assert.strictEqual(futFill1.quantity, "1");
assert.strictEqual(futFill1.side, "SELL");

// 12.6 Query positions: position is now closed
const futPositions2 = JSON.parse(futBook.positions("2026-03-01T23:02:00+00:00"));
assert.deepStrictEqual(futPositions2, []);

// 12.7 Cross-venue refusals
assert.throws(
  () => futBook.submit(JSON.stringify({ ...futEntryOrder, id: "bad_equity", instr: "AAPL" }), "2026-03-01T23:02:00+00:00"),
  (err) => {
    assert.match(err.message, /^value: SimBroker accepts futures orders only$/);
    return true;
  },
  "Equity order on futures book must throw 'value: SimBroker accepts futures orders only'"
);

assert.throws(
  () => book.submit(JSON.stringify({ ...futEntryOrder, id: "bad_fut", instr: "MNQ" }), "2026-03-02T14:33:00+00:00"),
  (err) => {
    assert.match(err.message, /^value: SimBroker accepts equity orders only$/);
    return true;
  },
  "Futures order on equity book must throw 'value: SimBroker accepts equity orders only'"
);

// 12.8 Futures instrument key helper
assert.strictEqual(sim_instrument_key("MNQ"), "F\u0001MNQ\u0001\u000125e-2\u00012e0");
assert.strictEqual(sim_instrument_key("/ES"), "F\u0001ES\u0001\u000125e-2\u00015e1");

console.log("Smoke test passed: equity matched golden.json and futures scenario verified.");
