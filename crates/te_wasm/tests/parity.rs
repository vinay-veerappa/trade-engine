//! Parity tests: drive the SAME op sequence into:
//! (a) `te_core`'s `Book` directly
//! (b) `te_wasm`'s api layer (`SimBookApi`)
//! and assert identical results and identical refusals (kind AND message) at every step.
//!
//! Covers at least 200 seeded walks x ~50 steps across every order type, both sides,
//! brackets, cancels, replaces, gaps, malformed input JSON, restore, and trailing stops.

use std::collections::HashMap;

use chrono::NaiveDate;
use te_core::ledger::model::{Instrument, OrderState, OrderType, Side, Tif};
use te_core::ledger::pydec::PyDec;
use te_core::sim::broker::{self as sb, Alloc, Begin, Book, Pos, VFill, VOrder};
use te_core::sim::trailing as tr;
use te_core::sim::{session_close, session_open, Ts};
use te_wasm::api::{self, bar_to_json, parse_vorder_json, vorder_to_json, SimBookApi};

struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Self(if seed == 0 { 0xdeadbeefcafebabe } else { seed })
    }

    fn next_u64(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }

    fn gen_range(&mut self, min: usize, max: usize) -> usize {
        if min >= max {
            return min;
        }
        min + (self.next_u64() as usize % (max - min))
    }

    fn gen_bool(&mut self, p: f64) -> bool {
        let r = (self.next_u64() % 10000) as f64 / 10000.0;
        r < p
    }

    fn choice<'a, T>(&mut self, slice: &'a [T]) -> &'a T {
        &slice[self.gen_range(0, slice.len())]
    }
}

fn dec(s: &str) -> PyDec {
    PyDec::parse(s).unwrap()
}

fn cents(c: i64) -> String {
    format!("{}.{:02}", c / 100, (c % 100).abs())
}

#[derive(Default)]
struct Tally {
    steps: usize,
    submits: usize,
    brackets: usize,
    cancels: usize,
    replaces: usize,
    bars: usize,
    orders_queries: usize,
    fills_queries: usize,
    positions_queries: usize,
    fill_queries: usize,
    malformed_json: usize,
    restores: usize,
    trailing_updates: usize,
    refusals_matched: usize,
}

struct WalkContext<'a> {
    rng: &'a mut Rng,
    tally: &'a mut Tally,
    direct: Book,
    api: SimBookApi,
    day: NaiveDate,
    clock: Ts,
    order_counter: usize,
    working_orders: Vec<String>,
    all_orders: HashMap<String, VOrder>,
    prices: HashMap<String, i64>,
    last_bar_ts: HashMap<String, Ts>,
}

impl<'a> WalkContext<'a> {
    fn new(rng: &'a mut Rng, tally: &'a mut Tally, seed: usize) -> Option<Self> {
        let days = [
            NaiveDate::from_ymd_opt(2026, 3, 2).unwrap(),  // regular session
            NaiveDate::from_ymd_opt(2026, 6, 15).unwrap(), // regular summer
            NaiveDate::from_ymd_opt(2026, 11, 27).unwrap(), // early close (day after Thanksgiving)
            NaiveDate::from_ymd_opt(2026, 12, 24).unwrap(), // early close (Christmas Eve)
            NaiveDate::from_ymd_opt(2026, 1, 2).unwrap(),  // day after New Year
        ];
        let day = *rng.choice(&days);
        let open_ts = session_open(day).unwrap();

        // 3% chance of testing invalid negative slippage to assert construction refusal parity
        let slip_str = if rng.gen_bool(0.03) {
            "-5"
        } else {
            *rng.choice(&["0", "1", "5", "10", "2.5"])
        };

        let direct_res = Book::new("ACC", true, dec(slip_str));
        let api_res = SimBookApi::new("ACC", true, slip_str);

        match (direct_res, api_res) {
            (Ok(direct), Ok(api)) => {
                tally.steps += 1;
                let clock = open_ts.add_seconds(-300).unwrap();
                let mut prices = HashMap::new();
                prices.insert("AAPL".to_string(), rng.gen_range(15000, 25000) as i64);
                prices.insert("MSFT".to_string(), rng.gen_range(30000, 45000) as i64);

                let mut ctx = WalkContext {
                    rng,
                    tally,
                    direct,
                    api,
                    day,
                    clock,
                    order_counter: seed * 1000,
                    working_orders: Vec::new(),
                    all_orders: HashMap::new(),
                    prices,
                    last_bar_ts: HashMap::new(),
                };

                // Connect
                ctx.do_connect();
                Some(ctx)
            }
            (Err(de), Err(ae)) => {
                tally.steps += 1;
                tally.refusals_matched += 1;
                assert_eq!(de.kind, ae.kind);
                assert_eq!(de.msg, ae.msg);
                None
            }
            (d, a) => panic!("Mismatch on construction: direct={:?}, api={:?}", d.err(), a.err()),
        }
    }

    fn do_connect(&mut self) {
        self.tally.steps += 1;
        let now_iso = self.clock.iso.clone();
        let mut c = || Ok(now_iso.clone());
        let direct_res = self.direct.connect(&mut c);
        let api_res = self.api.connect(&now_iso);

        match (direct_res, api_res) {
            (Ok(dt), Ok(at)) => {
                assert_eq!(dt.iso, at);
            }
            (Err(de), Err(ae)) => {
                self.tally.refusals_matched += 1;
                assert_eq!(de.kind, ae.kind);
                assert_eq!(de.msg, ae.msg);
            }
            (d, a) => panic!("Mismatch on connect: direct={:?}, api={:?}", d.err(), a.err()),
        }
    }

    fn assert_ack_equal(direct_ack: &sb::Ack, api_json: &str) {
        let v: serde_json::Value = serde_json::from_str(api_json).unwrap();
        assert_eq!(v["id"].as_str().unwrap(), direct_ack.id);
        assert_eq!(v["status"].as_str().unwrap(), direct_ack.status);
        assert_eq!(v["ts"].as_str().unwrap(), direct_ack.ts.iso);
        let expected_msg = direct_ack.msg.as_deref();
        let actual_msg = v["msg"].as_str();
        assert_eq!(actual_msg, expected_msg);
    }

    fn step_submit(&mut self, is_bracket: bool) {
        self.tally.steps += 1;
        self.order_counter += 1;
        let id = format!("ord_{}", self.order_counter);

        let sym = if self.rng.gen_bool(0.6) { "AAPL" } else { "MSFT" };
        let instr = Instrument::Equity(sym.to_string());
        let price_cents = self.prices[sym];

        let otype = *self.rng.choice(&[
            OrderType::Market,
            OrderType::Limit,
            OrderType::Stop,
            OrderType::StopLimit,
        ]);
        let side = if self.rng.gen_bool(0.5) { Side::Buy } else { Side::Sell };
        let tif = *self.rng.choice(&[Tif::Day, Tif::Gtc, Tif::Opg]);

        let qty_int = self.rng.gen_range(1, 100);
        let qty = dec(&qty_int.to_string());

        let mut limit = None;
        let mut stop = None;
        let trail = None;

        match otype {
            OrderType::Limit => {
                let offset = self.rng.gen_range(1, 200) as i64;
                let p = if side == Side::Buy { price_cents - offset } else { price_cents + offset };
                limit = Some(dec(&cents(p.max(100))));
            }
            OrderType::Stop => {
                let offset = self.rng.gen_range(1, 200) as i64;
                let p = if side == Side::Buy { price_cents + offset } else { price_cents - offset };
                stop = Some(dec(&cents(p.max(100))));
            }
            OrderType::StopLimit => {
                let offset = self.rng.gen_range(1, 200) as i64;
                let sp = if side == Side::Buy { price_cents + offset } else { price_cents - offset };
                let lp = if side == Side::Buy { sp + 50 } else { sp - 50 };
                stop = Some(dec(&cents(sp.max(100))));
                limit = Some(dec(&cents(lp.max(100))));
            }
            _ => {}
        }

        let mut parent = None;
        let mut oco = None;

        if is_bracket && !self.working_orders.is_empty() {
            let p_id = self.rng.choice(&self.working_orders).clone();
            parent = Some(p_id.clone());
            if self.rng.gen_bool(0.7) {
                oco = Some(format!("{p_id}:oco"));
            }
            self.tally.brackets += 1;
        } else {
            self.tally.submits += 1;
        }

        let alloc_account = if self.rng.gen_bool(0.98) { "ACC" } else { "WRONG_ACC" };
        let order = VOrder {
            id: id.clone(),
            instr,
            otype,
            side,
            quantity: qty,
            submitted_at: self.clock.clone(),
            tif,
            limit,
            stop,
            trail,
            allocs: vec![Alloc {
                soid: format!("so_{id}"),
                account: alloc_account.to_string(),
                qty: dec(&qty_int.to_string()),
            }],
            parent,
            oco,
        };

        let now_iso = self.clock.iso.clone();
        let mut c = || Ok(now_iso.clone());
        let direct_res = self.direct.submit(order.clone(), &mut c);

        let order_json = vorder_to_json(&order);
        let api_res = self.api.submit(&order_json, &now_iso);

        match (direct_res, api_res) {
            (Ok(da), Ok(aj)) => {
                Self::assert_ack_equal(&da, &aj);
                if da.status == "ACCEPTED" {
                    self.working_orders.push(id.clone());
                    self.all_orders.insert(id, order);
                }
            }
            (Err(de), Err(ae)) => {
                self.tally.refusals_matched += 1;
                assert_eq!(de.kind, ae.kind);
                assert_eq!(de.msg, ae.msg);
            }
            (d, a) => panic!("Mismatch on submit: direct={:?}, api={:?}", d.err(), a.err()),
        }
    }

    fn step_feed_bar(&mut self, is_gap: bool, is_touch: bool) {
        self.tally.steps += 1;
        self.tally.bars += 1;

        let sym = if self.rng.gen_bool(0.5) { "AAPL" } else { "MSFT" };
        let instr = Instrument::Equity(sym.to_string());
        let p = self.prices[sym];

        let next_ts = match self.last_bar_ts.get(sym) {
            None => session_open(self.day).unwrap(),
            Some(last) => last.add_seconds(60).unwrap(),
        };

        // Don't go past session close
        let close = session_close(self.day).unwrap();
        if next_ts.ge(&close) {
            return;
        }

        self.clock = next_ts.clone();

        let gap = if is_gap {
            self.rng.gen_range(50, 400) as i64 * (if self.rng.gen_bool(0.5) { 1 } else { -1 })
        } else {
            0
        };

        let open = (p + gap).max(100);
        let close_p = (open + (self.rng.gen_range(0, 100) as i64 - 50)).max(100);

        let mut high = open.max(close_p) + self.rng.gen_range(0, 50) as i64;
        let mut low = (open.min(close_p) - self.rng.gen_range(0, 50) as i64).max(10);

        if is_touch && !self.working_orders.is_empty() {
            let wid = self.rng.choice(&self.working_orders);
            if let Some(w) = self.all_orders.get(wid) {
                if let Some(lim) = &w.limit {
                    let cents: i64 = (lim.to_py_string().parse::<f64>().unwrap() * 100.0) as i64;
                    high = high.max(cents + 10);
                    low = low.min(cents - 10);
                }
                if let Some(st) = &w.stop {
                    let cents: i64 = (st.to_py_string().parse::<f64>().unwrap() * 100.0) as i64;
                    high = high.max(cents + 10);
                    low = low.min(cents - 10);
                }
            }
        }

        self.prices.insert(sym.to_string(), close_p);

        let bar = sb::Bar {
            instr,
            ts: next_ts.clone(),
            open: dec(&cents(open)),
            high: dec(&cents(high)),
            low: dec(&cents(low)),
            close: dec(&cents(close_p)),
            volume: dec("1000"),
            as_of: next_ts.add_seconds(60).unwrap(),
        };

        let direct_res = self.direct.process_bar(Some(bar.clone()));
        let bar_json = bar_to_json(&bar);
        let api_res = self.api.process_bar(Some(&bar_json));

        match (direct_res, api_res) {
            (Ok(dfills), Ok(afills_json)) => {
                let afills: Vec<usize> = serde_json::from_str(&afills_json).unwrap();
                assert_eq!(dfills, afills);
                self.last_bar_ts.insert(sym.to_string(), next_ts);
            }
            (Err(de), Err(ae)) => {
                self.tally.refusals_matched += 1;
                assert_eq!(de.kind, ae.kind);
                assert_eq!(de.msg, ae.msg);
            }
            (d, a) => panic!("Mismatch on bar: direct={:?}, api={:?}", d.err(), a.err()),
        }
    }

    fn step_feed_bad_bar(&mut self) {
        self.tally.steps += 1;
        self.tally.bars += 1;

        let kind = self.rng.gen_range(0, 5);
        match kind {
            0 => {
                // missing bar (None)
                let direct_res = self.direct.process_bar(None);
                let api_res = self.api.process_bar(None);
                match (direct_res, api_res) {
                    (Err(de), Err(ae)) => {
                        self.tally.refusals_matched += 1;
                        assert_eq!(de.kind, ae.kind);
                        assert_eq!(de.msg, ae.msg);
                    }
                    (d, a) => panic!("Expected refusal on None bar: direct={:?}, api={:?}", d.err(), a.err()),
                }
            }
            1 => {
                // Non-minute aligned bar timestamp (has seconds)
                let open_ts = session_open(self.day).unwrap();
                let bad_ts = open_ts.add_seconds(30).unwrap();
                let bar = sb::Bar {
                    instr: Instrument::Equity("AAPL".into()),
                    ts: bad_ts,
                    open: dec("150.00"),
                    high: dec("151.00"),
                    low: dec("149.00"),
                    close: dec("150.50"),
                    volume: dec("100"),
                    as_of: open_ts.add_seconds(60).unwrap(),
                };
                let direct_res = self.direct.process_bar(Some(bar.clone()));
                let bar_json = bar_to_json(&bar);
                let api_res = self.api.process_bar(Some(&bar_json));
                match (direct_res, api_res) {
                    (Err(de), Err(ae)) => {
                        self.tally.refusals_matched += 1;
                        assert_eq!(de.kind, ae.kind);
                        assert_eq!(de.msg, ae.msg);
                    }
                    (d, a) => panic!("Expected refusal on non-minute bar: direct={:?}, api={:?}", d.err(), a.err()),
                }
            }
            2 => {
                // Non-session date bar
                let bad_date = NaiveDate::from_ymd_opt(2026, 3, 1).unwrap(); // Sunday
                let bad_ts = Ts::aware(&format!("{}T14:30:00+00:00", bad_date), "t").unwrap();
                let bar = sb::Bar {
                    instr: Instrument::Equity("AAPL".into()),
                    ts: bad_ts.clone(),
                    open: dec("150.00"),
                    high: dec("151.00"),
                    low: dec("149.00"),
                    close: dec("150.50"),
                    volume: dec("100"),
                    as_of: bad_ts.add_seconds(60).unwrap(),
                };
                let direct_res = self.direct.process_bar(Some(bar.clone()));
                let bar_json = bar_to_json(&bar);
                let api_res = self.api.process_bar(Some(&bar_json));
                match (direct_res, api_res) {
                    (Err(de), Err(ae)) => {
                        self.tally.refusals_matched += 1;
                        assert_eq!(de.kind, ae.kind);
                        assert_eq!(de.msg, ae.msg);
                    }
                    (d, a) => panic!("Expected refusal on non-session bar: direct={:?}, api={:?}", d.err(), a.err()),
                }
            }
            3 => {
                // Out of order bar (timestamp earlier than last)
                if let Some(last) = self.last_bar_ts.get("AAPL") {
                    let earlier = last.add_seconds(-60).unwrap();
                    let bar = sb::Bar {
                        instr: Instrument::Equity("AAPL".into()),
                        ts: earlier.clone(),
                        open: dec("150.00"),
                        high: dec("151.00"),
                        low: dec("149.00"),
                        close: dec("150.50"),
                        volume: dec("100"),
                        as_of: earlier.add_seconds(60).unwrap(),
                    };
                    let direct_res = self.direct.process_bar(Some(bar.clone()));
                    let bar_json = bar_to_json(&bar);
                    let api_res = self.api.process_bar(Some(&bar_json));
                    match (direct_res, api_res) {
                        (Err(de), Err(ae)) => {
                            self.tally.refusals_matched += 1;
                            assert_eq!(de.kind, ae.kind);
                            assert_eq!(de.msg, ae.msg);
                        }
                        (d, a) => panic!("Expected refusal on out-of-order bar: direct={:?}, api={:?}", d.err(), a.err()),
                    }
                }
            }
            _ => {
                // Option instrument on equity broker
                let bar = sb::Bar {
                    instr: Instrument::Option(te_core::ledger::model::OptionContract {
                        underlying: "AAPL".into(),
                        expiry: self.day,
                        strike: dec("150"),
                        right: te_core::options::Right::Call,
                        multiplier: 100,
                    }),
                    ts: session_open(self.day).unwrap(),
                    open: dec("5.00"),
                    high: dec("6.00"),
                    low: dec("4.00"),
                    close: dec("5.50"),
                    volume: dec("10"),
                    as_of: session_open(self.day).unwrap().add_seconds(60).unwrap(),
                };
                let direct_res = self.direct.process_bar(Some(bar.clone()));
                let bar_json = bar_to_json(&bar);
                let api_res = self.api.process_bar(Some(&bar_json));
                match (direct_res, api_res) {
                    (Err(de), Err(ae)) => {
                        self.tally.refusals_matched += 1;
                        assert_eq!(de.kind, ae.kind);
                        assert_eq!(de.msg, ae.msg);
                    }
                    (d, a) => panic!("Expected refusal on non-equity bar: direct={:?}, api={:?}", d.err(), a.err()),
                }
            }
        }
    }

    fn step_cancel(&mut self) {
        self.tally.steps += 1;
        self.tally.cancels += 1;

        let id = if !self.working_orders.is_empty() && self.rng.gen_bool(0.7) {
            self.rng.choice(&self.working_orders).clone()
        } else {
            format!("nonexistent_{}", self.order_counter)
        };

        let now_iso = self.clock.iso.clone();
        let mut c = || Ok(now_iso.clone());
        let direct_res = self.direct.cancel(&id, &mut c);
        let api_res = self.api.cancel(&id, &now_iso);

        match (direct_res, api_res) {
            (Ok(da), Ok(aj)) => {
                Self::assert_ack_equal(&da, &aj);
            }
            (Err(de), Err(ae)) => {
                self.tally.refusals_matched += 1;
                assert_eq!(de.kind, ae.kind);
                assert_eq!(de.msg, ae.msg);
            }
            (d, a) => panic!("Mismatch on cancel: direct={:?}, api={:?}", d.err(), a.err()),
        }
    }

    fn step_replace(&mut self) {
        self.tally.steps += 1;
        self.tally.replaces += 1;

        let id = if !self.working_orders.is_empty() && self.rng.gen_bool(0.8) {
            self.rng.choice(&self.working_orders).clone()
        } else {
            format!("nonexistent_{}", self.order_counter)
        };

        let new_qty_val = if self.rng.gen_bool(0.1) {
            Some(dec("-10")) // invalid negative quantity
        } else if self.rng.gen_bool(0.8) {
            Some(dec(&self.rng.gen_range(1, 200).to_string()))
        } else {
            None
        };

        let now_iso = self.clock.iso.clone();
        let mut c = || Ok(now_iso.clone());
        let direct_begin = self.direct.replace_begin(&id, new_qty_val.clone(), &mut c);

        let new_qty_str = new_qty_val.as_ref().map(|d| d.to_py_string());
        let api_begin = self.api.replace_begin(&id, new_qty_str.as_deref(), &now_iso);

        match (direct_begin, api_begin) {
            (Ok(Begin::Done(da)), Ok(aj)) => {
                let v: serde_json::Value = serde_json::from_str(&aj).unwrap();
                assert_eq!(v["outcome"].as_str().unwrap(), "ack");
                let ack_obj = &v["ack"];
                assert_eq!(ack_obj["id"].as_str().unwrap(), da.id);
                assert_eq!(ack_obj["status"].as_str().unwrap(), da.status);
            }
            (Ok(Begin::Go(dq)), Ok(aj)) => {
                let v: serde_json::Value = serde_json::from_str(&aj).unwrap();
                assert_eq!(v["outcome"].as_str().unwrap(), "go");
                assert_eq!(v["quantity"].as_str().unwrap(), dq.to_py_string());

                // Now test either reject or commit
                if self.rng.gen_bool(0.5) {
                    let msg = "Rejected by risk check".to_string();
                    let mut c2 = || Ok(now_iso.clone());
                    let direct_rej = self.direct.replace_reject(&id, msg.clone(), &mut c2);
                    let api_rej = self.api.replace_reject(&id, &msg, &now_iso);
                    match (direct_rej, api_rej) {
                        (Ok(da), Ok(aj)) => Self::assert_ack_equal(&da, &aj),
                        (Err(de), Err(ae)) => {
                            self.tally.refusals_matched += 1;
                            assert_eq!(de.kind, ae.kind);
                            assert_eq!(de.msg, ae.msg);
                        }
                        (d, a) => panic!("Mismatch on replace_reject: direct={:?}, api={:?}", d.err(), a.err()),
                    }
                } else if let Some(mut updated) = self.all_orders.get(&id).cloned() {
                    updated.quantity = dq;
                    let mut c3 = || Ok(now_iso.clone());
                    let direct_commit = self.direct.replace_commit(&id, updated.clone(), &mut c3);
                    let ord_json = vorder_to_json(&updated);
                    let api_commit = self.api.replace_commit(&id, &ord_json, &now_iso);
                    match (direct_commit, api_commit) {
                        (Ok(da), Ok(aj)) => {
                            Self::assert_ack_equal(&da, &aj);
                            self.all_orders.insert(id, updated);
                        }
                        (Err(de), Err(ae)) => {
                            self.tally.refusals_matched += 1;
                            assert_eq!(de.kind, ae.kind);
                            assert_eq!(de.msg, ae.msg);
                        }
                        (d, a) => panic!("Mismatch on replace_commit: direct={:?}, api={:?}", d.err(), a.err()),
                    }
                }
            }
            (Err(de), Err(ae)) => {
                self.tally.refusals_matched += 1;
                assert_eq!(de.kind, ae.kind);
                assert_eq!(de.msg, ae.msg);
            }
            (d, a) => panic!("Mismatch on replace_begin: direct={:?}, api={:?}", d.err(), a.err()),
        }
    }

    fn step_queries(&mut self) {
        self.tally.steps += 4;
        self.tally.orders_queries += 1;
        self.tally.fills_queries += 1;
        self.tally.positions_queries += 1;
        self.tally.fill_queries += 1;

        let now_iso = self.clock.iso.clone();
        let since_iso = session_open(self.day).unwrap().iso;

        // 1. orders_since
        let mut c = || Ok(now_iso.clone());
        let direct_orders = self.direct.orders_since(&since_iso, &mut c);
        let api_orders = self.api.orders_since(&since_iso, &now_iso);
        match (direct_orders, api_orders) {
            (Ok(drows), Ok(ajson)) => {
                let arows: Vec<serde_json::Value> = serde_json::from_str(&ajson).unwrap();
                assert_eq!(drows.len(), arows.len());
                for (d, a) in drows.iter().zip(&arows) {
                    assert_eq!(d.0, a["id"].as_str().unwrap());
                    assert_eq!(d.1.value(), a["state"].as_str().unwrap());
                    assert_eq!(d.2.to_py_string(), a["filled"].as_str().unwrap());
                    assert_eq!(d.3.to_py_string(), a["remaining"].as_str().unwrap());
                    assert_eq!(d.4.iso, a["updated_at"].as_str().unwrap());
                }
            }
            (Err(de), Err(ae)) => {
                self.tally.refusals_matched += 1;
                assert_eq!(de.kind, ae.kind);
                assert_eq!(de.msg, ae.msg);
            }
            (d, a) => panic!("Mismatch on orders_since: direct={:?}, api={:?}", d.err(), a.err()),
        }

        // 2. fills_since
        let mut c2 = || Ok(now_iso.clone());
        let direct_fills = self.direct.fills_since(&since_iso, &mut c2);
        let api_fills = self.api.fills_since(&since_iso, &now_iso);
        match (direct_fills, api_fills) {
            (Ok(df), Ok(ajson)) => {
                let af: Vec<usize> = serde_json::from_str(&ajson).unwrap();
                assert_eq!(df, af);
            }
            (Err(de), Err(ae)) => {
                self.tally.refusals_matched += 1;
                assert_eq!(de.kind, ae.kind);
                assert_eq!(de.msg, ae.msg);
            }
            (d, a) => panic!("Mismatch on fills_since: direct={:?}, api={:?}", d.err(), a.err()),
        }

        // 3. positions
        let mut c3 = || Ok(now_iso.clone());
        let direct_pos = self.direct.positions(&mut c3);
        let api_pos = self.api.positions(&now_iso);
        match (direct_pos, api_pos) {
            (Ok(dp), Ok(ajson)) => {
                let ap: Vec<serde_json::Value> = serde_json::from_str(&ajson).unwrap();
                assert_eq!(dp.len(), ap.len());
                for (d, a) in dp.iter().zip(&ap) {
                    assert_eq!(d.instr.symbol().unwrap(), a["symbol"].as_str().unwrap());
                    assert_eq!(d.qty.to_py_string(), a["quantity"].as_str().unwrap());
                    assert_eq!(d.avg.to_py_string(), a["avg_price"].as_str().unwrap());
                    assert_eq!(d.as_of.iso, a["as_of"].as_str().unwrap());
                }
            }
            (Err(de), Err(ae)) => {
                self.tally.refusals_matched += 1;
                assert_eq!(de.kind, ae.kind);
                assert_eq!(de.msg, ae.msg);
            }
            (d, a) => panic!("Mismatch on positions: direct={:?}, api={:?}", d.err(), a.err()),
        }

        // 4. has and fill_count and fill
        assert_eq!(self.direct.fills.len(), self.api.fill_count());
        if !self.direct.fills.is_empty() {
            let idx = self.rng.gen_range(0, self.direct.fills.len());
            let df = &self.direct.fills[idx];
            let af_json = self.api.fill(idx).unwrap();
            let af: serde_json::Value = serde_json::from_str(&af_json).unwrap();
            assert_eq!(df.fill_id, af["fill_id"].as_str().unwrap());
            assert_eq!(df.order_id, af["order_id"].as_str().unwrap());
            assert_eq!(df.quantity.to_py_string(), af["quantity"].as_str().unwrap());
            assert_eq!(df.price.to_py_string(), af["price"].as_str().unwrap());
            assert_eq!(df.filled_at.iso, af["filled_at"].as_str().unwrap());
            assert_eq!(df.side.value(), af["side"].as_str().unwrap());
        }

        // 5. cash_events
        assert_eq!(self.api.cash_events(&since_iso).unwrap(), "[]");
        // Naive timestamp refusal on cash_events
        let naive_err = self.api.cash_events("2026-03-02T14:30:00").unwrap_err();
        assert_eq!(naive_err.kind, "value");
        assert_eq!(naive_err.msg, "since must be timezone-aware");
    }

    fn step_malformed_json(&mut self) {
        self.tally.steps += 1;
        self.tally.malformed_json += 1;

        let bad_json = self.rng.choice(&[
            "{not valid json",
            "{\"id\": 123}",
            "{\"id\": \"v1\", \"instr\": \"AAPL\", \"otype\": \"BAD_TYPE\"}",
            "{\"id\": \"v1\", \"instr\": \"AAPL\", \"otype\": \"MARKET\", \"side\": \"UNKNOWN\"}",
            "{\"id\": \"v1\", \"instr\": \"AAPL\", \"otype\": \"MARKET\", \"side\": \"BUY\", \"quantity\": \"abc\"}",
            "{\"id\": \"v1\", \"instr\": \"AAPL\", \"otype\": \"MARKET\", \"side\": \"BUY\", \"quantity\": \"10\", \"submitted_at\": \"naive_ts\"}",
            "null",
            "",
        ]);

        let now_iso = self.clock.iso.clone();
        let direct_parse = parse_vorder_json(bad_json);
        let api_submit = self.api.submit(bad_json, &now_iso);

        match (direct_parse, api_submit) {
            (Err(de), Err(ae)) => {
                self.tally.refusals_matched += 1;
                assert_eq!(de.kind, ae.kind);
                assert_eq!(de.msg, ae.msg);
            }
            (Ok(_), Ok(_)) => panic!("Expected bad json to fail: {bad_json}"),
            (d, a) => panic!("Mismatch on malformed json: direct={:?}, api={:?}", d.err(), a.err()),
        }
    }
}

fn test_restore_parity() {
    let now_iso = "2026-03-02T14:30:00+00:00";
    let mut direct_book = Book::new("ACC", true, dec("0")).unwrap();
    let mut api_book = SimBookApi::new("ACC", true, "0").unwrap();

    let order = VOrder {
        id: "v1".to_string(),
        instr: Instrument::Equity("AAPL".into()),
        otype: OrderType::Limit,
        side: Side::Buy,
        quantity: dec("100"),
        submitted_at: Ts::aware(now_iso, "t").unwrap(),
        tif: Tif::Day,
        limit: Some(dec("150.00")),
        stop: None,
        trail: None,
        allocs: vec![Alloc {
            soid: "s1".into(),
            account: "ACC".into(),
            qty: dec("100"),
        }],
        parent: None,
        oco: None,
    };

    let fill = VFill {
        fill_id: "v1:fill:1".into(),
        order_id: "v1".into(),
        instr: Instrument::Equity("AAPL".into()),
        quantity: dec("50"),
        price: dec("149.50"),
        filled_at: Ts::aware(now_iso, "t").unwrap(),
        side: Side::Buy,
        src: Some(0),
    };

    let pos = Pos {
        instr: Instrument::Equity("AAPL".into()),
        qty: dec("50"),
        avg: dec("149.50"),
        as_of: Ts::aware(now_iso, "t").unwrap(),
    };

    // 1. Successful restore
    let mut c1 = || Ok(now_iso.to_string());
    let direct_res = direct_book.restore(
        vec![(order.clone(), OrderState::PartiallyFilled)],
        vec![fill.clone()],
        vec![pos.clone()],
        &mut c1,
    );

    let orders_json = serde_json::to_string(&serde_json::json!([
        [serde_json::from_str::<serde_json::Value>(&vorder_to_json(&order)).unwrap(), "PARTIALLY_FILLED"]
    ]))
    .unwrap();

    let fills_json = serde_json::to_string(&serde_json::json!([
        ["v1:fill:1", "v1", "AAPL", "50", "149.50", now_iso, "BUY"]
    ]))
    .unwrap();

    let positions_json = serde_json::to_string(&serde_json::json!([
        ["AAPL", "50", "149.50", now_iso]
    ]))
    .unwrap();

    let api_res = api_book.restore(&orders_json, &fills_json, &positions_json, now_iso);

    assert!(direct_res.is_ok());
    assert!(api_res.is_ok());

    // 2. Refusal parity on non-empty restore
    let mut c2 = || Ok(now_iso.to_string());
    let direct_fail = direct_book.restore(vec![], vec![], vec![], &mut c2);
    let api_fail = api_book.restore("[]", "[]", "[]", now_iso);

    assert_eq!(direct_fail.unwrap_err().kind, "sim");
    assert_eq!(api_fail.unwrap_err().kind, "sim");
    assert_eq!(
        direct_book.state_of("v1").is_some(),
        api_book.has("v1")
    );
}

fn test_trailing_parity() {
    // 1. check_trail_amount
    assert!(api::trail_check_amount("2.5").is_ok());
    assert_eq!(
        api::trail_check_amount("-1").unwrap_err().msg,
        "trail_amount must be positive"
    );
    assert_eq!(
        api::trail_check_amount("0").unwrap_err().msg,
        "trail_amount must be positive"
    );

    // 2. trail_update sell trailing up
    let mut t = tr::Trail {
        side: Side::Sell,
        trail_amount: dec("2"),
        extreme: None,
        stop_price: None,
        triggered: false,
    };

    let mut state_json = serde_json::to_string(&serde_json::json!({
        "extreme": null,
        "stop_price": null,
        "triggered": false,
    }))
    .unwrap();

    let prices = ["100", "105", "104", "103", "102"];
    for p_str in prices {
        let p_dec = dec(p_str);
        let direct_trig = tr::update(&mut t, &p_dec).unwrap();

        let api_out_json = api::trail_update("SELL", "2", &state_json, p_str).unwrap();
        let api_out: serde_json::Value = serde_json::from_str(&api_out_json).unwrap();

        assert_eq!(direct_trig, api_out["triggered"].as_bool().unwrap());
        assert_eq!(
            t.extreme.as_ref().map(|d| d.to_py_string()),
            api_out["state"]["extreme"].as_str().map(|s| s.to_string())
        );
        assert_eq!(
            t.stop_price.as_ref().map(|d| d.to_py_string()),
            api_out["state"]["stop_price"].as_str().map(|s| s.to_string())
        );

        state_json = serde_json::to_string(&api_out["state"]).unwrap();
    }
}

#[test]
fn test_sim_book_parity_walks() {
    let mut tally = Tally::default();
    let num_walks = 250;
    let steps_per_walk = 60;

    for seed in 0..num_walks {
        let mut rng = Rng::new(seed as u64 + 1000);
        let mut ctx = match WalkContext::new(&mut rng, &mut tally, seed) {
            Some(c) => c,
            None => continue,
        };

        for step in 0..steps_per_walk {
            let choice = ctx.rng.gen_range(0, 10);
            match choice {
                0 | 1 => ctx.step_submit(false),
                2 => ctx.step_submit(true), // bracket
                3 | 4 => {
                    let is_touch = ctx.rng.gen_bool(0.4);
                    ctx.step_feed_bar(false, is_touch);
                }
                5 => ctx.step_feed_bar(true, false), // gap
                6 => ctx.step_feed_bad_bar(),
                7 => ctx.step_cancel(),
                8 => ctx.step_replace(),
                9 => {
                    if step % 2 == 0 {
                        ctx.step_queries();
                    } else {
                        ctx.step_malformed_json();
                    }
                }
                _ => {}
            }
        }
    }

    test_restore_parity();
    tally.restores += 2;

    test_trailing_parity();
    tally.trailing_updates += 5;

    // Verify instrument key
    assert_eq!(api::sim_instrument_key("AAPL").unwrap(), "E\u{1}AAPL");
    assert_eq!(
        api::sim_instrument_key("{\"dc\":\"Equity\",\"f\":{\"symbol\":\"MSFT\"}}").unwrap(),
        "E\u{1}MSFT"
    );

    println!(
        "\nParity summary: {} walks, {} compared steps (submits: {}, brackets: {}, cancels: {}, replaces: {}, bars: {}, queries: {}, malformed: {}, restores: {}, trailing: {}, refusals matched: {})",
        num_walks,
        tally.steps,
        tally.submits,
        tally.brackets,
        tally.cancels,
        tally.replaces,
        tally.bars,
        tally.orders_queries + tally.fills_queries + tally.positions_queries + tally.fill_queries,
        tally.malformed_json,
        tally.restores,
        tally.trailing_updates,
        tally.refusals_matched,
    );

    assert!(tally.steps >= 10000, "Expected at least 10,000 compared steps, got {}", tally.steps);
    assert!(tally.submits > 0);
    assert!(tally.brackets > 0);
    assert!(tally.cancels > 0);
    assert!(tally.replaces > 0);
    assert!(tally.bars > 0);
    assert!(tally.malformed_json > 0);
    assert!(tally.refusals_matched > 0);
}

#[test]
fn test_smoke_golden_session() {
    let mut api = SimBookApi::new("SMOKE_ACC", true, "0").unwrap();

    let connected_at = api.connect("2026-03-02T14:29:00+00:00").unwrap();

    // 1. Submit Entry: Buy 100 AAPL @ Limit 150.00
    let entry_order = serde_json::json!({
        "id": "entry_1",
        "instr": "AAPL",
        "otype": "LIMIT",
        "side": "BUY",
        "quantity": "100",
        "submitted_at": "2026-03-02T14:29:30+00:00",
        "tif": "DAY",
        "limit": "150.00",
        "stop": null,
        "trail": null,
        "allocs": [{"soid": "s1", "account": "SMOKE_ACC", "qty": "100"}],
        "parent": null,
        "oco": null,
    });
    let entry_ack_json = api
        .submit(&serde_json::to_string(&entry_order).unwrap(), "2026-03-02T14:29:30+00:00")
        .unwrap();
    let entry_ack: serde_json::Value = serde_json::from_str(&entry_ack_json).unwrap();

    // 2. Bar 1 fills entry @ 150.00
    let bar1 = serde_json::json!({
        "instr": "AAPL",
        "ts": "2026-03-02T14:30:00+00:00",
        "open": "150.50",
        "high": "151.00",
        "low": "149.50",
        "close": "150.00",
        "volume": "1000",
        "as_of": "2026-03-02T14:31:00+00:00",
    });
    let bar1_fills_json = api
        .process_bar(Some(&serde_json::to_string(&bar1).unwrap()))
        .unwrap();
    let bar1_fills: Vec<usize> = serde_json::from_str(&bar1_fills_json).unwrap();
    assert_eq!(bar1_fills, vec![0]);

    // 3. Submit Target: Sell 100 AAPL @ Limit 155.00
    let target_order = serde_json::json!({
        "id": "target_1",
        "instr": "AAPL",
        "otype": "LIMIT",
        "side": "SELL",
        "quantity": "100",
        "submitted_at": "2026-03-02T14:30:30+00:00",
        "tif": "DAY",
        "limit": "155.00",
        "stop": null,
        "trail": null,
        "allocs": [{"soid": "s1:target:1", "account": "SMOKE_ACC", "qty": "100"}],
        "parent": "entry_1",
        "oco": "entry_1:oco",
    });
    let target_ack_json = api
        .submit(&serde_json::to_string(&target_order).unwrap(), "2026-03-02T14:30:30+00:00")
        .unwrap();
    let target_ack: serde_json::Value = serde_json::from_str(&target_ack_json).unwrap();

    // 4. Submit Stop: Sell 100 AAPL @ Stop 145.00
    let stop_order = serde_json::json!({
        "id": "stop_1",
        "instr": "AAPL",
        "otype": "STOP",
        "side": "SELL",
        "quantity": "100",
        "submitted_at": "2026-03-02T14:30:30+00:00",
        "tif": "DAY",
        "limit": null,
        "stop": "145.00",
        "trail": null,
        "allocs": [{"soid": "s1:stop", "account": "SMOKE_ACC", "qty": "100"}],
        "parent": "entry_1",
        "oco": "entry_1:oco",
    });
    let stop_ack_json = api
        .submit(&serde_json::to_string(&stop_order).unwrap(), "2026-03-02T14:30:30+00:00")
        .unwrap();
    let stop_ack: serde_json::Value = serde_json::from_str(&stop_ack_json).unwrap();

    // 5. Bar 2: between stop and target
    let bar2 = serde_json::json!({
        "instr": "AAPL",
        "ts": "2026-03-02T14:31:00+00:00",
        "open": "150.00",
        "high": "152.00",
        "low": "149.80",
        "close": "151.50",
        "volume": "1000",
        "as_of": "2026-03-02T14:32:00+00:00",
    });
    let bar2_fills_json = api
        .process_bar(Some(&serde_json::to_string(&bar2).unwrap()))
        .unwrap();
    let bar2_fills: Vec<usize> = serde_json::from_str(&bar2_fills_json).unwrap();
    assert_eq!(bar2_fills, Vec::<usize>::new());

    // 6. Bar 3: fills target @ 155.00
    let bar3 = serde_json::json!({
        "instr": "AAPL",
        "ts": "2026-03-02T14:32:00+00:00",
        "open": "152.00",
        "high": "156.00",
        "low": "151.00",
        "close": "155.50",
        "volume": "1000",
        "as_of": "2026-03-02T14:33:00+00:00",
    });
    let bar3_fills_json = api
        .process_bar(Some(&serde_json::to_string(&bar3).unwrap()))
        .unwrap();
    let bar3_fills: Vec<usize> = serde_json::from_str(&bar3_fills_json).unwrap();
    assert_eq!(bar3_fills, vec![1]);

    // 7. Query positions
    let positions_json = api.positions("2026-03-02T14:33:00+00:00").unwrap();
    let positions: serde_json::Value = serde_json::from_str(&positions_json).unwrap();
    assert_eq!(positions, serde_json::json!([]));

    // 8. Query orders
    let orders_json = api
        .orders_since("2026-03-02T14:29:00+00:00", "2026-03-02T14:33:00+00:00")
        .unwrap();
    let orders: serde_json::Value = serde_json::from_str(&orders_json).unwrap();

    // 9. Query fills
    let fills_indices_json = api
        .fills_since("2026-03-02T14:29:00+00:00", "2026-03-02T14:33:00+00:00")
        .unwrap();
    let fills_indices: Vec<usize> = serde_json::from_str(&fills_indices_json).unwrap();
    assert_eq!(fills_indices, vec![0, 1]);

    let fill0: serde_json::Value = serde_json::from_str(&api.fill(0).unwrap()).unwrap();
    let fill1: serde_json::Value = serde_json::from_str(&api.fill(1).unwrap()).unwrap();

    let session_output = serde_json::json!({
        "connected_at": connected_at,
        "entry_ack": entry_ack,
        "bar1_fills": bar1_fills,
        "target_ack": target_ack,
        "stop_ack": stop_ack,
        "bar2_fills": bar2_fills,
        "bar3_fills": bar3_fills,
        "positions": positions,
        "orders": orders,
        "fills": [fill0, fill1],
    });

    let golden_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("smoke");
    std::fs::create_dir_all(&golden_dir).unwrap();
    let golden_path = golden_dir.join("golden.json");
    let golden_text = serde_json::to_string_pretty(&session_output).unwrap();
    std::fs::write(&golden_path, golden_text).unwrap();
}
