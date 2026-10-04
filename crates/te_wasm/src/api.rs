//! Plain-Rust API layer for `te_wasm`.
//!
//! All functions take/return `String` or `Result<String, ApiError>` so that the logic
//! is testable natively without wasm32 dependencies. Money crosses as decimal strings (D6).
//! Timestamps cross as ISO-8601 UTC strings. Refusals cross as `ApiError { kind, msg }`.

use te_core::ledger::bridge as lb;
use te_core::ledger::model::{Instrument, LErr, Obj, OrderState, OrderType, Side, Tif, R};
use te_core::ledger::pydec::PyDec;
use te_core::sim::broker::{self as sb, Alloc, Begin, Book, Pos, VFill, VOrder};
use te_core::sim::trailing as tr;
use te_core::sim::{Clock, Ts};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApiError {
    pub kind: String,
    pub msg: String,
}

impl ApiError {
    pub fn new(kind: impl Into<String>, msg: impl Into<String>) -> Self {
        Self {
            kind: kind.into(),
            msg: msg.into(),
        }
    }
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.kind, self.msg)
    }
}

impl std::error::Error for ApiError {}

impl From<LErr> for ApiError {
    fn from(e: LErr) -> Self {
        ApiError {
            kind: e.kind.to_string(),
            msg: e.msg,
        }
    }
}

pub type ApiResult<T> = Result<T, ApiError>;

fn fail<T>(kind: &str, msg: impl Into<String>) -> ApiResult<T> {
    Err(ApiError::new(kind, msg))
}

pub fn parse_dec(s: &str) -> ApiResult<PyDec> {
    match PyDec::parse(s) {
        Some(d) => Ok(d),
        None => fail("value", format!("not a Decimal: {s:?}")),
    }
}

pub fn parse_side(s: &str) -> ApiResult<Side> {
    Side::parse(s).map_or_else(|| fail("value", format!("bad side {s:?}")), Ok)
}

pub fn parse_order_type(s: &str) -> ApiResult<OrderType> {
    OrderType::parse(s).map_or_else(|| fail("value", format!("bad order type {s:?}")), Ok)
}

pub fn parse_tif(s: &str) -> ApiResult<Tif> {
    Tif::parse(s).map_or_else(|| fail("value", format!("bad time in force {s:?}")), Ok)
}

pub fn parse_order_state(s: &str) -> ApiResult<OrderState> {
    OrderState::parse(s).map_or_else(|| fail("value", format!("bad state {s:?}")), Ok)
}

pub fn parse_ts(iso: &str, name: &str) -> ApiResult<Ts> {
    Ts::aware(iso, name).map_err(Into::into)
}

pub fn parse_instrument(text: &str) -> ApiResult<Instrument> {
    let trimmed = text.trim();
    if !trimmed.starts_with('{') && !trimmed.starts_with('[') {
        if text.is_empty() {
            return fail("value", "expected an instrument");
        }
        if trimmed.starts_with('/') {
            let fc = te_core::sim::tick::parse_future_symbol(text)?;
            return Ok(Instrument::Future(fc));
        }
        if let Ok(fc) = te_core::sim::tick::parse_future_symbol(text) {
            return Ok(Instrument::Future(fc));
        }
        return Ok(Instrument::Equity(text.to_string()));
    }
    if let Ok(serde_json::Value::Object(map)) = serde_json::from_str(trimmed) {
        if let Some(root) = map.get("root").and_then(|s| s.as_str()) {
            let sym = match map.get("contract_month") {
                Some(serde_json::Value::String(cm)) if !cm.is_empty() => format!("{root}{cm}"),
                Some(serde_json::Value::Object(cmo)) => {
                    let y = cmo.get("year").and_then(|val| val.as_i64()).unwrap_or(2026) as i32;
                    let m = cmo.get("month").and_then(|val| val.as_u64()).unwrap_or(12) as u32;
                    let code = te_core::sim::tick::month_to_code(m);
                    format!("{}{}{:02}", root, code, y.rem_euclid(100))
                }
                _ => root.to_string(),
            };
            let fc = te_core::sim::tick::parse_future_symbol(&sym)?;
            return Ok(Instrument::Future(fc));
        }
    }
    match lb::obj_from_text(text)? {
        Obj::Instr(i) => Ok(i),
        _ => fail("value", "expected an instrument"),
    }
}

pub fn instrument_from_value(v: &serde_json::Value) -> ApiResult<Instrument> {
    match v {
        serde_json::Value::String(s) => parse_instrument(s),
        serde_json::Value::Object(map) => {
            if let Some(root) = map.get("root").and_then(|s| s.as_str()) {
                let sym = match map.get("contract_month") {
                    Some(serde_json::Value::String(cm)) if !cm.is_empty() => format!("{root}{cm}"),
                    Some(serde_json::Value::Object(cmo)) => {
                        let y = cmo.get("year").and_then(|val| val.as_i64()).unwrap_or(2026) as i32;
                        let m = cmo.get("month").and_then(|val| val.as_u64()).unwrap_or(12) as u32;
                        let code = te_core::sim::tick::month_to_code(m);
                        format!("{}{}{:02}", root, code, y.rem_euclid(100))
                    }
                    _ => root.to_string(),
                };
                let fc = te_core::sim::tick::parse_future_symbol(&sym)?;
                Ok(Instrument::Future(fc))
            } else if map.contains_key("dc") {
                let text =
                    serde_json::to_string(v).map_err(|e| ApiError::new("value", e.to_string()))?;
                match lb::obj_from_text(&text)? {
                    Obj::Instr(i) => Ok(i),
                    _ => fail("value", "expected an instrument"),
                }
            } else if let Some(sym) = map.get("symbol").and_then(|s| s.as_str()) {
                parse_instrument(sym)
            } else {
                fail("value", "expected an instrument")
            }
        }
        _ => fail("value", "expected an instrument"),
    }
}

pub fn parse_vorder_value(v: &serde_json::Value) -> ApiResult<VOrder> {
    match v {
        serde_json::Value::Object(map) => {
            let id = map
                .get("id")
                .or_else(|| map.get("venue_order_id"))
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing or non-string order id"))?;
            let instr_val = map
                .get("instr")
                .or_else(|| map.get("instrument"))
                .or_else(|| map.get("symbol"))
                .ok_or_else(|| ApiError::new("value", "missing order instrument"))?;
            let instr = instrument_from_value(instr_val)?;
            let otype_str = map
                .get("otype")
                .or_else(|| map.get("order_type"))
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing or non-string order type"))?;
            let otype = parse_order_type(otype_str)?;
            let side_str = map
                .get("side")
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing or non-string side"))?;
            let side = parse_side(side_str)?;
            let qty_str = map
                .get("quantity")
                .or_else(|| map.get("qty"))
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing or non-string quantity"))?;
            let quantity = parse_dec(qty_str)?;
            let sub_str = map
                .get("submitted_at")
                .or_else(|| map.get("submitted"))
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing or non-string submitted_at"))?;
            let submitted_at = parse_ts(sub_str, "submitted_at")?;
            let tif_str = map
                .get("tif")
                .or_else(|| map.get("time_in_force"))
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing or non-string tif"))?;
            let tif = parse_tif(tif_str)?;

            let limit = match map.get("limit").or_else(|| map.get("limit_price")) {
                None | Some(serde_json::Value::Null) => None,
                Some(serde_json::Value::String(s)) => Some(parse_dec(s)?),
                _ => return fail("value", "invalid limit price"),
            };
            let stop = match map.get("stop").or_else(|| map.get("stop_price")) {
                None | Some(serde_json::Value::Null) => None,
                Some(serde_json::Value::String(s)) => Some(parse_dec(s)?),
                _ => return fail("value", "invalid stop price"),
            };
            let trail = match map.get("trail").or_else(|| map.get("trail_amount")) {
                None | Some(serde_json::Value::Null) => None,
                Some(serde_json::Value::String(s)) => Some(parse_dec(s)?),
                _ => return fail("value", "invalid trail amount"),
            };

            let mut allocs = Vec::new();
            if let Some(alloc_val) = map.get("allocs").or_else(|| map.get("allocations")) {
                let arr = alloc_val
                    .as_array()
                    .ok_or_else(|| ApiError::new("value", "allocations must be an array"))?;
                for it in arr {
                    match it {
                        serde_json::Value::Object(am) => {
                            let soid = am
                                .get("soid")
                                .or_else(|| am.get("strategy_order_id"))
                                .and_then(|x| x.as_str())
                                .ok_or_else(|| ApiError::new("value", "missing alloc soid"))?;
                            let account = am
                                .get("account")
                                .or_else(|| am.get("account_id"))
                                .and_then(|x| x.as_str())
                                .ok_or_else(|| ApiError::new("value", "missing alloc account"))?;
                            let q = am
                                .get("qty")
                                .or_else(|| am.get("quantity"))
                                .and_then(|x| x.as_str())
                                .ok_or_else(|| ApiError::new("value", "missing alloc quantity"))?;
                            allocs.push(Alloc {
                                soid: soid.to_string(),
                                account: account.to_string(),
                                qty: parse_dec(q)?,
                            });
                        }
                        serde_json::Value::Array(triple) if triple.len() == 3 => {
                            let soid = triple[0]
                                .as_str()
                                .ok_or_else(|| ApiError::new("value", "bad alloc soid"))?;
                            let account = triple[1]
                                .as_str()
                                .ok_or_else(|| ApiError::new("value", "bad alloc account"))?;
                            let q = triple[2]
                                .as_str()
                                .ok_or_else(|| ApiError::new("value", "bad alloc qty"))?;
                            allocs.push(Alloc {
                                soid: soid.to_string(),
                                account: account.to_string(),
                                qty: parse_dec(q)?,
                            });
                        }
                        _ => return fail("value", "invalid allocation item"),
                    }
                }
            }

            let parent = match map.get("parent").or_else(|| map.get("parent_order_id")) {
                None | Some(serde_json::Value::Null) => None,
                Some(serde_json::Value::String(s)) => Some(s.clone()),
                _ => return fail("value", "invalid parent order id"),
            };
            let oco = match map.get("oco").or_else(|| map.get("oco_group")) {
                None | Some(serde_json::Value::Null) => None,
                Some(serde_json::Value::String(s)) => Some(s.clone()),
                _ => return fail("value", "invalid oco group"),
            };

            Ok(VOrder {
                id: id.to_string(),
                instr,
                otype,
                side,
                quantity,
                submitted_at,
                tif,
                limit,
                stop,
                trail,
                allocs,
                parent,
                oco,
            })
        }
        serde_json::Value::Array(arr) if arr.len() == 11 => {
            let id = arr[0]
                .as_str()
                .ok_or_else(|| ApiError::new("value", "bad order id"))?;
            let instr = instrument_from_value(&arr[1])?;
            let otype = parse_order_type(
                arr[2]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad otype"))?,
            )?;
            let side = parse_side(
                arr[3]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad side"))?,
            )?;
            let quantity = parse_dec(
                arr[4]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad quantity"))?,
            )?;
            let submitted_at = parse_ts(
                arr[5]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad submitted_at"))?,
                "submitted_at",
            )?;
            let tif = parse_tif(
                arr[6]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad tif"))?,
            )?;

            let (limit, stop, trail) = match &arr[7] {
                serde_json::Value::Array(tri) if tri.len() == 3 => {
                    let l = match &tri[0] {
                        serde_json::Value::Null => None,
                        serde_json::Value::String(s) => Some(parse_dec(s)?),
                        _ => return fail("value", "bad limit"),
                    };
                    let st = match &tri[1] {
                        serde_json::Value::Null => None,
                        serde_json::Value::String(s) => Some(parse_dec(s)?),
                        _ => return fail("value", "bad stop"),
                    };
                    let tr = match &tri[2] {
                        serde_json::Value::Null => None,
                        serde_json::Value::String(s) => Some(parse_dec(s)?),
                        _ => return fail("value", "bad trail"),
                    };
                    (l, st, tr)
                }
                _ => return fail("value", "bad prices tuple"),
            };

            let mut allocs = Vec::new();
            if let serde_json::Value::Array(items) = &arr[8] {
                for item in items {
                    match item {
                        serde_json::Value::Array(triple) if triple.len() == 3 => {
                            let soid = triple[0]
                                .as_str()
                                .ok_or_else(|| ApiError::new("value", "bad soid"))?;
                            let account = triple[1]
                                .as_str()
                                .ok_or_else(|| ApiError::new("value", "bad account"))?;
                            let q = triple[2]
                                .as_str()
                                .ok_or_else(|| ApiError::new("value", "bad qty"))?;
                            allocs.push(Alloc {
                                soid: soid.to_string(),
                                account: account.to_string(),
                                qty: parse_dec(q)?,
                            });
                        }
                        _ => return fail("value", "bad alloc item"),
                    }
                }
            } else {
                return fail("value", "bad allocs array");
            }

            let parent = match &arr[9] {
                serde_json::Value::Null => None,
                serde_json::Value::String(s) => Some(s.clone()),
                _ => return fail("value", "bad parent"),
            };
            let oco = match &arr[10] {
                serde_json::Value::Null => None,
                serde_json::Value::String(s) => Some(s.clone()),
                _ => return fail("value", "bad oco"),
            };

            Ok(VOrder {
                id: id.to_string(),
                instr,
                otype,
                side,
                quantity,
                submitted_at,
                tif,
                limit,
                stop,
                trail,
                allocs,
                parent,
                oco,
            })
        }
        _ => fail("value", "expected order object or array"),
    }
}

pub fn parse_vorder_json(json_str: &str) -> ApiResult<VOrder> {
    let v: serde_json::Value = serde_json::from_str(json_str)
        .map_err(|e| ApiError::new("value", format!("malformed JSON: {e}")))?;
    parse_vorder_value(&v)
}

pub fn parse_bar_value(v: &serde_json::Value) -> ApiResult<sb::Bar> {
    match v {
        serde_json::Value::Object(map) => {
            let instr_val = map
                .get("instr")
                .or_else(|| map.get("instrument"))
                .or_else(|| map.get("symbol"))
                .ok_or_else(|| ApiError::new("value", "missing bar instrument"))?;
            let instr = instrument_from_value(instr_val)?;
            let ts_str = map
                .get("ts")
                .or_else(|| map.get("timestamp"))
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing or non-string bar timestamp"))?;
            let ts = parse_ts(ts_str, "Bar timestamp")?;
            let o = map
                .get("open")
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing bar open"))?;
            let h = map
                .get("high")
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing bar high"))?;
            let l = map
                .get("low")
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing bar low"))?;
            let c = map
                .get("close")
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing bar close"))?;
            let vol = map
                .get("volume")
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing bar volume"))?;
            let as_of_str = map
                .get("as_of")
                .and_then(|x| x.as_str())
                .ok_or_else(|| ApiError::new("value", "missing bar as_of"))?;
            let as_of = parse_ts(as_of_str, "Bar as_of")?;

            Ok(sb::Bar {
                instr,
                ts,
                open: parse_dec(o)?,
                high: parse_dec(h)?,
                low: parse_dec(l)?,
                close: parse_dec(c)?,
                volume: parse_dec(vol)?,
                as_of,
            })
        }
        serde_json::Value::Array(arr) if arr.len() == 8 => {
            let instr = instrument_from_value(&arr[0])?;
            let ts = parse_ts(
                arr[1]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad bar ts"))?,
                "Bar timestamp",
            )?;
            let open = parse_dec(
                arr[2]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad open"))?,
            )?;
            let high = parse_dec(
                arr[3]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad high"))?,
            )?;
            let low = parse_dec(
                arr[4]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad low"))?,
            )?;
            let close = parse_dec(
                arr[5]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad close"))?,
            )?;
            let volume = parse_dec(
                arr[6]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad volume"))?,
            )?;
            let as_of = parse_ts(
                arr[7]
                    .as_str()
                    .ok_or_else(|| ApiError::new("value", "bad as_of"))?,
                "Bar as_of",
            )?;
            Ok(sb::Bar {
                instr,
                ts,
                open,
                high,
                low,
                close,
                volume,
                as_of,
            })
        }
        _ => fail("value", "expected bar object or array"),
    }
}

pub fn parse_bar_json(json_str: &str) -> ApiResult<Option<sb::Bar>> {
    let s = json_str.trim();
    if s.is_empty() || s == "null" {
        return Ok(None);
    }
    let v: serde_json::Value = serde_json::from_str(s)
        .map_err(|e| ApiError::new("value", format!("malformed JSON: {e}")))?;
    if v.is_null() {
        return Ok(None);
    }
    parse_bar_value(&v).map(Some)
}

fn with_clock<T>(now_iso: &str, f: impl FnOnce(&mut Clock<'_>) -> R<T>) -> ApiResult<T> {
    let mut clock = || Ok(now_iso.to_string());
    f(&mut clock).map_err(Into::into)
}

fn serialize_ack(ack: &sb::Ack) -> String {
    serde_json::to_string(&serde_json::json!({
        "id": ack.id,
        "status": ack.status,
        "ts": ack.ts.iso,
        "msg": ack.msg,
    }))
    .unwrap()
}

pub fn instrument_to_json_val(i: &Instrument) -> serde_json::Value {
    match i {
        Instrument::Equity(s) => serde_json::Value::String(s.clone()),
        Instrument::Future(f) => {
            let cm_val = match &f.contract_month {
                None => serde_json::Value::Null,
                Some(cm) => serde_json::Value::String(format!(
                    "{}{:02}",
                    cm.month_code(),
                    cm.year.rem_euclid(100)
                )),
            };
            serde_json::json!({
                "root": f.root,
                "contract_month": cm_val,
                "tick_size": f.tick_size.to_py_string(),
                "point_value": f.point_value.to_py_string(),
            })
        }
        other => {
            let j = te_core::ledger::codec::enc_instrument(other).unwrap();
            serde_json::from_str(&te_core::ledger::json::dumps(&j)).unwrap()
        }
    }
}

pub fn vorder_to_json(order: &VOrder) -> String {
    let allocs: Vec<serde_json::Value> = order
        .allocs
        .iter()
        .map(|a| {
            serde_json::json!({
                "soid": a.soid,
                "account": a.account,
                "qty": a.qty.to_py_string(),
            })
        })
        .collect();

    let instr_val = instrument_to_json_val(&order.instr);

    serde_json::to_string(&serde_json::json!({
        "id": order.id,
        "instr": instr_val,
        "otype": order.otype.value(),
        "side": order.side.value(),
        "quantity": order.quantity.to_py_string(),
        "submitted_at": order.submitted_at.iso,
        "tif": order.tif.value(),
        "limit": order.limit.as_ref().map(|d| d.to_py_string()),
        "stop": order.stop.as_ref().map(|d| d.to_py_string()),
        "trail": order.trail.as_ref().map(|d| d.to_py_string()),
        "allocs": allocs,
        "parent": order.parent,
        "oco": order.oco,
    }))
    .unwrap()
}

pub fn bar_to_json(bar: &sb::Bar) -> String {
    let instr_val = instrument_to_json_val(&bar.instr);
    serde_json::to_string(&serde_json::json!({
        "instr": instr_val,
        "ts": bar.ts.iso,
        "open": bar.open.to_py_string(),
        "high": bar.high.to_py_string(),
        "low": bar.low.to_py_string(),
        "close": bar.close.to_py_string(),
        "volume": bar.volume.to_py_string(),
        "as_of": bar.as_of.iso,
    }))
    .unwrap()
}

pub struct SimBookApi {
    pub book: Book,
}

impl SimBookApi {
    pub fn new(account_id: &str, is_decimal: bool, slippage_bps: &str) -> ApiResult<Self> {
        let dec_val = parse_dec(slippage_bps)?;
        let book = Book::new(account_id, is_decimal, dec_val)?;
        Ok(SimBookApi { book })
    }

    pub fn new_futures(account_id: &str, slippage_ticks: u32) -> ApiResult<Self> {
        let book = Book::new_futures(account_id, slippage_ticks)?;
        Ok(SimBookApi { book })
    }

    pub fn connect(&mut self, now_iso: &str) -> ApiResult<String> {
        with_clock(now_iso, |c| self.book.connect(c)).map(|t| t.iso)
    }

    pub fn restore(
        &mut self,
        orders_json: &str,
        fills_json: &str,
        positions_json: &str,
        now_iso: &str,
    ) -> ApiResult<()> {
        let ov: serde_json::Value = serde_json::from_str(orders_json)
            .map_err(|e| ApiError::new("value", format!("malformed orders JSON: {e}")))?;
        let fv: serde_json::Value = serde_json::from_str(fills_json)
            .map_err(|e| ApiError::new("value", format!("malformed fills JSON: {e}")))?;
        let pv: serde_json::Value = serde_json::from_str(positions_json)
            .map_err(|e| ApiError::new("value", format!("malformed positions JSON: {e}")))?;

        let o_arr = ov
            .as_array()
            .ok_or_else(|| ApiError::new("value", "orders must be an array"))?;
        let mut orders = Vec::new();
        for item in o_arr {
            match item {
                serde_json::Value::Array(pair) if pair.len() == 2 => {
                    let order = parse_vorder_value(&pair[0])?;
                    let state_str = pair[1]
                        .as_str()
                        .ok_or_else(|| ApiError::new("value", "bad state"))?;
                    let state = parse_order_state(state_str)?;
                    orders.push((order, state));
                }
                serde_json::Value::Object(m) => {
                    let order_val = m
                        .get("order")
                        .ok_or_else(|| ApiError::new("value", "missing order"))?;
                    let order = parse_vorder_value(order_val)?;
                    let state_str = m
                        .get("state")
                        .and_then(|x| x.as_str())
                        .ok_or_else(|| ApiError::new("value", "missing or bad state"))?;
                    let state = parse_order_state(state_str)?;
                    orders.push((order, state));
                }
                _ => return fail("value", "invalid order restore item"),
            }
        }

        let f_arr = fv
            .as_array()
            .ok_or_else(|| ApiError::new("value", "fills must be an array"))?;
        let mut fills = Vec::new();
        for (i, item) in f_arr.iter().enumerate() {
            match item {
                serde_json::Value::Array(arr) if arr.len() == 7 => {
                    let fid = arr[0]
                        .as_str()
                        .ok_or_else(|| ApiError::new("value", "bad fill_id"))?;
                    let oid = arr[1]
                        .as_str()
                        .ok_or_else(|| ApiError::new("value", "bad order_id"))?;
                    let instr = instrument_from_value(&arr[2])?;
                    let q = parse_dec(
                        arr[3]
                            .as_str()
                            .ok_or_else(|| ApiError::new("value", "bad fill qty"))?,
                    )?;
                    let p = parse_dec(
                        arr[4]
                            .as_str()
                            .ok_or_else(|| ApiError::new("value", "bad fill price"))?,
                    )?;
                    let at = parse_ts(
                        arr[5]
                            .as_str()
                            .ok_or_else(|| ApiError::new("value", "bad filled_at"))?,
                        "filled_at",
                    )?;
                    let sd = parse_side(
                        arr[6]
                            .as_str()
                            .ok_or_else(|| ApiError::new("value", "bad fill side"))?,
                    )?;
                    fills.push(VFill {
                        fill_id: fid.to_string(),
                        order_id: oid.to_string(),
                        instr,
                        quantity: q,
                        price: p,
                        filled_at: at,
                        side: sd,
                        src: Some(i),
                    });
                }
                serde_json::Value::Object(m) => {
                    let fid = m
                        .get("fill_id")
                        .and_then(|x| x.as_str())
                        .ok_or_else(|| ApiError::new("value", "bad fill_id"))?;
                    let oid = m
                        .get("order_id")
                        .and_then(|x| x.as_str())
                        .ok_or_else(|| ApiError::new("value", "bad order_id"))?;
                    let instr_val = m
                        .get("instr")
                        .or_else(|| m.get("instrument"))
                        .or_else(|| m.get("symbol"))
                        .ok_or_else(|| ApiError::new("value", "missing fill instr"))?;
                    let instr = instrument_from_value(instr_val)?;
                    let q_str = m
                        .get("quantity")
                        .or_else(|| m.get("qty"))
                        .and_then(|x| x.as_str())
                        .ok_or_else(|| ApiError::new("value", "bad fill qty"))?;
                    let p_str = m
                        .get("price")
                        .and_then(|x| x.as_str())
                        .ok_or_else(|| ApiError::new("value", "bad fill price"))?;
                    let at_str = m
                        .get("filled_at")
                        .and_then(|x| x.as_str())
                        .ok_or_else(|| ApiError::new("value", "bad filled_at"))?;
                    let sd_str = m
                        .get("side")
                        .and_then(|x| x.as_str())
                        .ok_or_else(|| ApiError::new("value", "bad fill side"))?;
                    fills.push(VFill {
                        fill_id: fid.to_string(),
                        order_id: oid.to_string(),
                        instr,
                        quantity: parse_dec(q_str)?,
                        price: parse_dec(p_str)?,
                        filled_at: parse_ts(at_str, "filled_at")?,
                        side: parse_side(sd_str)?,
                        src: Some(i),
                    });
                }
                _ => return fail("value", "invalid fill restore item"),
            }
        }

        let p_arr = pv
            .as_array()
            .ok_or_else(|| ApiError::new("value", "positions must be an array"))?;
        let mut positions = Vec::new();
        for item in p_arr {
            match item {
                serde_json::Value::Array(arr) if arr.len() == 4 => {
                    let instr = instrument_from_value(&arr[0])?;
                    let q = parse_dec(
                        arr[1]
                            .as_str()
                            .ok_or_else(|| ApiError::new("value", "bad pos qty"))?,
                    )?;
                    let avg = parse_dec(
                        arr[2]
                            .as_str()
                            .ok_or_else(|| ApiError::new("value", "bad pos avg"))?,
                    )?;
                    let as_of = parse_ts(
                        arr[3]
                            .as_str()
                            .ok_or_else(|| ApiError::new("value", "bad pos as_of"))?,
                        "as_of",
                    )?;
                    positions.push(Pos {
                        instr,
                        qty: q,
                        avg,
                        as_of,
                    });
                }
                serde_json::Value::Object(m) => {
                    let instr_val = m
                        .get("instr")
                        .or_else(|| m.get("instrument"))
                        .or_else(|| m.get("symbol"))
                        .ok_or_else(|| ApiError::new("value", "missing pos instr"))?;
                    let instr = instrument_from_value(instr_val)?;
                    let q_str = m
                        .get("quantity")
                        .or_else(|| m.get("qty"))
                        .and_then(|x| x.as_str())
                        .ok_or_else(|| ApiError::new("value", "bad pos qty"))?;
                    let avg_str = m
                        .get("avg_price")
                        .or_else(|| m.get("avg"))
                        .and_then(|x| x.as_str())
                        .ok_or_else(|| ApiError::new("value", "bad pos avg"))?;
                    let as_of_str = m
                        .get("as_of")
                        .and_then(|x| x.as_str())
                        .ok_or_else(|| ApiError::new("value", "bad pos as_of"))?;
                    positions.push(Pos {
                        instr,
                        qty: parse_dec(q_str)?,
                        avg: parse_dec(avg_str)?,
                        as_of: parse_ts(as_of_str, "as_of")?,
                    });
                }
                _ => return fail("value", "invalid position restore item"),
            }
        }

        let book = &mut self.book;
        with_clock(now_iso, |c| book.restore(orders, fills, positions, c))
    }

    pub fn submit(&mut self, order_json: &str, now_iso: &str) -> ApiResult<String> {
        let order = parse_vorder_json(order_json)?;
        let book = &mut self.book;
        let ack = with_clock(now_iso, |c| book.submit(order, c))?;
        Ok(serialize_ack(&ack))
    }

    pub fn cancel(&mut self, id: &str, now_iso: &str) -> ApiResult<String> {
        let ack = with_clock(now_iso, |c| self.book.cancel(id, c))?;
        Ok(serialize_ack(&ack))
    }

    pub fn replace_begin(
        &mut self,
        id: &str,
        new_quantity: Option<&str>,
        now_iso: &str,
    ) -> ApiResult<String> {
        let qty = match new_quantity {
            None => None,
            Some(s) if s.is_empty() || s == "null" => None,
            Some(s) => Some(parse_dec(s)?),
        };
        let book = &mut self.book;
        let r = with_clock(now_iso, |c| book.replace_begin(id, qty, c))?;
        Ok(match r {
            Begin::Done(ack) => serde_json::to_string(&serde_json::json!({
                "outcome": "ack",
                "ack": {
                    "id": ack.id,
                    "status": ack.status,
                    "ts": ack.ts.iso,
                    "msg": ack.msg,
                },
                "value": {
                    "id": ack.id,
                    "status": ack.status,
                    "ts": ack.ts.iso,
                    "msg": ack.msg,
                }
            }))
            .unwrap(),
            Begin::Go(q) => serde_json::to_string(&serde_json::json!({
                "outcome": "go",
                "quantity": q.to_py_string(),
                "value": q.to_py_string(),
            }))
            .unwrap(),
        })
    }

    pub fn replace_reject(&mut self, id: &str, msg: &str, now_iso: &str) -> ApiResult<String> {
        let ack = with_clock(now_iso, |c| {
            self.book.replace_reject(id, msg.to_string(), c)
        })?;
        Ok(serialize_ack(&ack))
    }

    pub fn replace_commit(
        &mut self,
        id: &str,
        order_json: &str,
        now_iso: &str,
    ) -> ApiResult<String> {
        let order = parse_vorder_json(order_json)?;
        let book = &mut self.book;
        let ack = with_clock(now_iso, |c| book.replace_commit(id, order, c))?;
        Ok(serialize_ack(&ack))
    }

    pub fn orders_since(&mut self, since: &str, now_iso: &str) -> ApiResult<String> {
        let book = &mut self.book;
        let rows = with_clock(now_iso, |c| book.orders_since(since, c))?;
        let out: Vec<serde_json::Value> = rows
            .into_iter()
            .map(|(id, st, filled, rem, at)| {
                serde_json::json!({
                    "id": id,
                    "state": st.value(),
                    "filled": filled.to_py_string(),
                    "remaining": rem.to_py_string(),
                    "updated_at": at.iso,
                })
            })
            .collect();
        Ok(serde_json::to_string(&out).unwrap())
    }

    pub fn orders(&mut self, since: &str, now_iso: &str) -> ApiResult<String> {
        self.orders_since(since, now_iso)
    }

    pub fn fills_since(&mut self, since: &str, now_iso: &str) -> ApiResult<String> {
        let book = &mut self.book;
        let indices = with_clock(now_iso, |c| book.fills_since(since, c))?;
        Ok(serde_json::to_string(&indices).unwrap())
    }

    pub fn fills(&mut self, since: &str, now_iso: &str) -> ApiResult<String> {
        self.fills_since(since, now_iso)
    }

    pub fn positions(&mut self, now_iso: &str) -> ApiResult<String> {
        let book = &mut self.book;
        let rows = with_clock(now_iso, |c| book.positions(c))?;
        let out: Vec<serde_json::Value> = rows
            .into_iter()
            .map(|p| {
                let sym = p.instr.symbol().unwrap_or_else(|_| "UNKNOWN".to_string());
                let hk = p.instr.hk();
                if let Instrument::Future(ref fc) = p.instr {
                    serde_json::json!({
                        "symbol": sym.clone(),
                        "key": hk,
                        "instrument": sym,
                        "quantity": p.qty.to_py_string(),
                        "qty": p.qty.to_py_string(),
                        "avg_price": p.avg.to_py_string(),
                        "avg": p.avg.to_py_string(),
                        "as_of": p.as_of.iso,
                        "point_value": fc.point_value.to_py_string(),
                        "tick_size": fc.tick_size.to_py_string(),
                    })
                } else {
                    serde_json::json!({
                        "symbol": sym.clone(),
                        "key": hk,
                        "instrument": sym,
                        "quantity": p.qty.to_py_string(),
                        "qty": p.qty.to_py_string(),
                        "avg_price": p.avg.to_py_string(),
                        "avg": p.avg.to_py_string(),
                        "as_of": p.as_of.iso,
                    })
                }
            })
            .collect();
        Ok(serde_json::to_string(&out).unwrap())
    }

    pub fn process_bar(&mut self, bar_json: Option<&str>) -> ApiResult<String> {
        let bar = match bar_json {
            None => None,
            Some(s) => parse_bar_json(s)?,
        };
        let fills = self.book.process_bar(bar)?;
        Ok(serde_json::to_string(&fills).unwrap())
    }

    pub fn on_bar(&mut self, bar_json: Option<&str>) -> ApiResult<String> {
        self.process_bar(bar_json)
    }

    pub fn has(&self, id: &str) -> bool {
        self.book.state_of(id).is_some()
    }

    pub fn fill_count(&self) -> usize {
        self.book.fills.len()
    }

    pub fn fill(&self, i: usize) -> ApiResult<String> {
        if i >= self.book.fills.len() {
            return fail(
                "value",
                format!("fill index {i} out of range 0..{}", self.book.fills.len()),
            );
        }
        let f = &self.book.fills[i];
        let sym = f.instr.symbol().unwrap_or_else(|_| "UNKNOWN".to_string());
        Ok(serde_json::to_string(&serde_json::json!({
            "fill_id": f.fill_id,
            "order_id": f.order_id,
            "instrument": sym.clone(),
            "symbol": sym,
            "quantity": f.quantity.to_py_string(),
            "price": f.price.to_py_string(),
            "filled_at": f.filled_at.iso,
            "side": f.side.value(),
            "src": f.src,
        }))
        .unwrap())
    }

    pub fn cash_events(&self, since: &str) -> ApiResult<String> {
        self.book.cash_events(since)?;
        Ok("[]".to_string())
    }

    pub fn position_pnl(&self, symbol: &str, mark_str: &str) -> ApiResult<Option<String>> {
        let mark = parse_dec(mark_str)?;
        let pnl = self.book.position_pnl(symbol, &mark)?;
        Ok(pnl.map(|d| d.to_py_string()))
    }
}

pub fn sim_instrument_key(text: &str) -> ApiResult<String> {
    let instr = parse_instrument(text)?;
    Ok(instr.hk())
}

pub fn trail_check_amount(trail_amount: &str) -> ApiResult<()> {
    let d = parse_dec(trail_amount)?;
    tr::check_trail_amount(&d).map_err(Into::into)
}

pub fn trail_update(
    side_str: &str,
    trail_amount: &str,
    state_json: &str,
    price_str: &str,
) -> ApiResult<String> {
    let s = parse_side(side_str)?;
    let amt = parse_dec(trail_amount)?;
    let p = parse_dec(price_str)?;

    let v: serde_json::Value = serde_json::from_str(state_json)
        .map_err(|e| ApiError::new("value", format!("malformed JSON: {e}")))?;

    let (extreme, stop_price, triggered) = match v {
        serde_json::Value::Object(map) => {
            let ext = match map.get("extreme") {
                None | Some(serde_json::Value::Null) => None,
                Some(serde_json::Value::String(st)) => Some(parse_dec(st)?),
                _ => return fail("value", "invalid extreme price"),
            };
            let stop = match map.get("stop_price").or_else(|| map.get("stop")) {
                None | Some(serde_json::Value::Null) => None,
                Some(serde_json::Value::String(st)) => Some(parse_dec(st)?),
                _ => return fail("value", "invalid stop price"),
            };
            let trig = map
                .get("triggered")
                .and_then(|x| x.as_bool())
                .unwrap_or(false);
            (ext, stop, trig)
        }
        serde_json::Value::Array(arr) if arr.len() == 3 => {
            let ext = match &arr[0] {
                serde_json::Value::Null => None,
                serde_json::Value::String(st) => Some(parse_dec(st)?),
                _ => return fail("value", "invalid extreme price"),
            };
            let stop = match &arr[1] {
                serde_json::Value::Null => None,
                serde_json::Value::String(st) => Some(parse_dec(st)?),
                _ => return fail("value", "invalid stop price"),
            };
            let trig = arr[2]
                .as_bool()
                .ok_or_else(|| ApiError::new("value", "invalid triggered bool"))?;
            (ext, stop, trig)
        }
        _ => return fail("value", "expected trail state object or array"),
    };

    let mut t = tr::Trail {
        side: s,
        trail_amount: amt,
        extreme,
        stop_price,
        triggered,
    };

    let r = tr::update(&mut t, &p);

    let state_out = serde_json::json!({
        "extreme": t.extreme.as_ref().map(|d| d.to_py_string()),
        "stop_price": t.stop_price.as_ref().map(|d| d.to_py_string()),
        "triggered": t.triggered,
    });

    let out = match r {
        Ok(trig) => serde_json::json!({
            "triggered": trig,
            "state": state_out,
            "error": null,
        }),
        Err(e) => serde_json::json!({
            "triggered": null,
            "state": state_out,
            "error": {
                "kind": e.kind,
                "msg": e.msg,
            },
        }),
    };

    Ok(serde_json::to_string(&out).unwrap())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_futures_parse_roundtrip() {
        let symbols = [
            "NQ", "MNQ", "ES", "MES",
            "/NQ", "/MNQ", "/ES", "/MES",
            "NQZ26", "MNQZ26", "/ESZ26", "MESH25", "NQZ6", "NQZ2026",
        ];
        for sym in symbols {
            let instr = parse_instrument(sym).unwrap();
            let Instrument::Future(ref fc) = instr else {
                panic!("Expected Instrument::Future for {sym}");
            };
            assert!(!fc.root.is_empty());

            // 1. JSON representation via instrument_to_json_val
            let json_val = instrument_to_json_val(&instr);
            let obj = json_val.as_object().unwrap();
            assert_eq!(obj["root"].as_str().unwrap(), fc.root);
            assert_eq!(obj["tick_size"].as_str().unwrap(), fc.tick_size.to_py_string());
            assert_eq!(obj["point_value"].as_str().unwrap(), fc.point_value.to_py_string());

            if let Some(ref cm) = fc.contract_month {
                let expected_cm = format!("{}{:02}", cm.month_code(), cm.year.rem_euclid(100));
                assert_eq!(obj["contract_month"].as_str().unwrap(), expected_cm);
            } else {
                assert!(obj["contract_month"].is_null());
            }

            // 2. Round-trip via instrument_from_value
            let from_val = instrument_from_value(&json_val).unwrap();
            assert!(instr.same(&from_val), "Mismatch on instrument_from_value for {sym}");

            // 3. Round-trip via parse_instrument from serialized JSON string
            let json_str = serde_json::to_string(&json_val).unwrap();
            let from_str = parse_instrument(&json_str).unwrap();
            assert!(instr.same(&from_str), "Mismatch on parse_instrument(json_str) for {sym}");
        }

        // Test VOrder roundtrip
        let order_json = serde_json::json!({
            "id": "ord_fut_1",
            "instr": "MNQ",
            "otype": "LIMIT",
            "side": "BUY",
            "quantity": "2",
            "submitted_at": "2026-03-01T23:00:00+00:00",
            "tif": "DAY",
            "limit": "18000.00",
            "stop": null,
            "trail": null,
            "allocs": [{"soid": "s1", "account": "ACC", "qty": "2"}],
            "parent": null,
            "oco": null,
        });
        let vorder = parse_vorder_value(&order_json).unwrap();
        let serialized_order = vorder_to_json(&vorder);
        let roundtrip_order = parse_vorder_json(&serialized_order).unwrap();
        assert_eq!(vorder.id, roundtrip_order.id);
        assert!(vorder.instr.same(&roundtrip_order.instr));

        // Test Bar roundtrip
        let bar_json = serde_json::json!({
            "instr": "NQZ26",
            "ts": "2026-03-01T23:00:00+00:00",
            "open": "18000.00",
            "high": "18010.00",
            "low": "17995.00",
            "close": "18005.00",
            "volume": "100",
            "as_of": "2026-03-01T23:01:00+00:00",
        });
        let bar = parse_bar_value(&bar_json).unwrap();
        let serialized_bar = bar_to_json(&bar);
        let roundtrip_bar = parse_bar_json(&serialized_bar).unwrap().unwrap();
        assert!(bar.instr.same(&roundtrip_bar.instr));
        assert_eq!(bar.ts.iso, roundtrip_bar.ts.iso);
    }

    #[test]
    fn test_new_futures_slippage_on_stop_fill() {
        // NQ tick size is 0.25. 2 ticks slippage = 0.50 adverse.
        let mut api = SimBookApi::new_futures("ACC", 2).unwrap();
        api.connect("2020-11-22T23:00:00+00:00").unwrap();

        // 1. Buy stop at 12000.00
        let buy_stop = serde_json::json!({
            "id": "buy_stop",
            "instr": "NQ",
            "otype": "STOP",
            "side": "BUY",
            "quantity": "1",
            "submitted_at": "2020-11-22T23:00:00+00:00",
            "tif": "DAY",
            "limit": null,
            "stop": "12000.00",
            "trail": null,
            "allocs": [{"soid": "s1", "account": "ACC", "qty": "1"}],
            "parent": null,
            "oco": null,
        });
        api.submit(&buy_stop.to_string(), "2020-11-22T23:00:00+00:00").unwrap();

        // Bar triggers stop: open 11990.00, high 12010.00, low 11985.00, close 12005.00
        let bar1 = serde_json::json!({
            "instr": "NQ",
            "ts": "2020-11-22T23:01:00+00:00",
            "open": "11990.00",
            "high": "12010.00",
            "low": "11985.00",
            "close": "12005.00",
            "volume": "100",
            "as_of": "2020-11-22T23:02:00+00:00",
        });
        let fills1 = api.process_bar(Some(&bar1.to_string())).unwrap();
        assert_eq!(fills1, "[0]");
        let fill0: serde_json::Value = serde_json::from_str(&api.fill(0).unwrap()).unwrap();
        // Slipped adverse buy: 12000.00 + 2 * 0.25 = 12000.50
        assert_eq!(fill0["price"].as_str().unwrap(), "12000.50");
        assert_eq!(fill0["side"].as_str().unwrap(), "BUY");

        // 2. Sell stop at 12000.00
        let sell_stop = serde_json::json!({
            "id": "sell_stop",
            "instr": "NQ",
            "otype": "STOP",
            "side": "SELL",
            "quantity": "1",
            "submitted_at": "2020-11-22T23:01:30+00:00",
            "tif": "DAY",
            "limit": null,
            "stop": "12000.00",
            "trail": null,
            "allocs": [{"soid": "s2", "account": "ACC", "qty": "1"}],
            "parent": null,
            "oco": null,
        });
        api.submit(&sell_stop.to_string(), "2020-11-22T23:01:30+00:00").unwrap();

        // Bar triggers sell stop: open 12010.00, high 12015.00, low 11990.00, close 11995.00
        let bar2 = serde_json::json!({
            "instr": "NQ",
            "ts": "2020-11-22T23:02:00+00:00",
            "open": "12010.00",
            "high": "12015.00",
            "low": "11990.00",
            "close": "11995.00",
            "volume": "100",
            "as_of": "2020-11-22T23:03:00+00:00",
        });
        let fills2 = api.process_bar(Some(&bar2.to_string())).unwrap();
        assert_eq!(fills2, "[1]");
        let fill1: serde_json::Value = serde_json::from_str(&api.fill(1).unwrap()).unwrap();
        // Slipped adverse sell: 12000.00 - 2 * 0.25 = 11999.50
        assert_eq!(fill1["price"].as_str().unwrap(), "11999.50");
        assert_eq!(fill1["side"].as_str().unwrap(), "SELL");
    }

    #[test]
    fn test_day_order_expiring_at_early_halt() {
        // Thanksgiving Friday 2020-11-27 has early halt at 13:15 ET (18:15 UTC).
        let mut api = SimBookApi::new_futures("ACC", 0).unwrap();
        api.connect("2020-11-27T15:00:00+00:00").unwrap();

        let day_order = serde_json::json!({
            "id": "day_ord",
            "instr": "NQ",
            "otype": "LIMIT",
            "side": "BUY",
            "quantity": "1",
            "submitted_at": "2020-11-27T15:00:00+00:00",
            "tif": "DAY",
            "limit": "10000.00",
            "stop": null,
            "trail": null,
            "allocs": [{"soid": "s1", "account": "ACC", "qty": "1"}],
            "parent": null,
            "oco": null,
        });
        api.submit(&day_order.to_string(), "2020-11-27T15:00:00+00:00").unwrap();

        // Bar at 13:14 ET (18:14 UTC)
        let bar_before = serde_json::json!({
            "instr": "NQ",
            "ts": "2020-11-27T18:14:00+00:00",
            "open": "12170.00",
            "high": "12175.00",
            "low": "12165.00",
            "close": "12170.00",
            "volume": "100",
            "as_of": "2020-11-27T18:15:00+00:00",
        });
        api.process_bar(Some(&bar_before.to_string())).unwrap();

        let orders_before: Vec<serde_json::Value> = serde_json::from_str(
            &api.orders_since("2020-11-27T00:00:00+00:00", "2020-11-27T18:14:00+00:00").unwrap(),
        )
        .unwrap();
        assert_eq!(orders_before[0]["state"].as_str().unwrap(), "ACCEPTED");

        // Advance clock to early halt 13:15 ET (18:15 UTC)
        let orders_after: Vec<serde_json::Value> = serde_json::from_str(
            &api.orders_since("2020-11-27T00:00:00+00:00", "2020-11-27T18:15:00+00:00").unwrap(),
        )
        .unwrap();
        assert_eq!(orders_after[0]["state"].as_str().unwrap(), "EXPIRED");
    }

    #[test]
    fn test_halt_bar_refused_with_missing_bar() {
        let mut api = SimBookApi::new_futures("ACC", 0).unwrap();
        api.connect("2020-11-23T22:30:00+00:00").unwrap();

        // Daily maintenance halt 17:00-18:00 ET (22:00-23:00 UTC)
        let halt_bar = serde_json::json!({
            "instr": "NQ",
            "ts": "2020-11-23T22:30:00+00:00",
            "open": "11900.00",
            "high": "11905.00",
            "low": "11895.00",
            "close": "11900.00",
            "volume": "100",
            "as_of": "2020-11-23T22:31:00+00:00",
        });
        let err = api.process_bar(Some(&halt_bar.to_string())).unwrap_err();
        assert_eq!(err.kind, "missing_bar");
        assert!(err.msg.contains("is not inside a Globex session (halt, weekend or closure)"));

        // Weekend bar: Saturday 12:00 ET (17:00 UTC)
        let mut api_wknd = SimBookApi::new_futures("ACC", 0).unwrap();
        api_wknd.connect("2020-11-21T17:00:00+00:00").unwrap();
        let wknd_bar = serde_json::json!({
            "instr": "NQ",
            "ts": "2020-11-21T17:00:00+00:00",
            "open": "11900.00",
            "high": "11905.00",
            "low": "11895.00",
            "close": "11900.00",
            "volume": "100",
            "as_of": "2020-11-21T17:01:00+00:00",
        });
        let err_wknd = api_wknd.process_bar(Some(&wknd_bar.to_string())).unwrap_err();
        assert_eq!(err_wknd.kind, "missing_bar");
        assert!(err_wknd.msg.contains("is not inside a Globex session (halt, weekend or closure)"));

        // Out-of-range calendar error: year 1999 (outside 2006..2027)
        let mut api_oor = SimBookApi::new_futures("ACC", 0).unwrap();
        api_oor.connect("1999-01-01T15:00:00+00:00").unwrap();
        let oor_bar = serde_json::json!({
            "instr": "NQ",
            "ts": "1999-01-01T15:00:00+00:00",
            "open": "11900.00",
            "high": "11905.00",
            "low": "11895.00",
            "close": "11900.00",
            "volume": "100",
            "as_of": "1999-01-01T15:01:00+00:00",
        });
        let err_oor = api_oor.process_bar(Some(&oor_bar.to_string())).unwrap_err();
        assert_eq!(err_oor.kind, "value");
        assert!(err_oor.msg.contains("outside the CME Globex calendar range"));
    }

    #[test]
    fn test_point_value_in_positions() {
        // Futures book: MNQ point value is 2
        let mut api_fut = SimBookApi::new_futures("ACC", 0).unwrap();
        api_fut.connect("2020-11-22T23:00:00+00:00").unwrap();
        let order_mnq = serde_json::json!({
            "id": "mnq_buy",
            "instr": "MNQ",
            "otype": "MARKET",
            "side": "BUY",
            "quantity": "2",
            "submitted_at": "2020-11-22T23:00:00+00:00",
            "tif": "DAY",
            "limit": null,
            "stop": null,
            "trail": null,
            "allocs": [{"soid": "s1", "account": "ACC", "qty": "2"}],
            "parent": null,
            "oco": null,
        });
        api_fut.submit(&order_mnq.to_string(), "2020-11-22T23:00:00+00:00").unwrap();
        let bar_mnq = serde_json::json!({
            "instr": "MNQ",
            "ts": "2020-11-22T23:01:00+00:00",
            "open": "18000.00",
            "high": "18010.00",
            "low": "17990.00",
            "close": "18005.00",
            "volume": "100",
            "as_of": "2020-11-22T23:02:00+00:00",
        });
        api_fut.process_bar(Some(&bar_mnq.to_string())).unwrap();

        let fut_pos: Vec<serde_json::Value> = serde_json::from_str(
            &api_fut.positions("2020-11-22T23:02:00+00:00").unwrap(),
        )
        .unwrap();
        assert_eq!(fut_pos.len(), 1);
        assert_eq!(fut_pos[0]["symbol"].as_str().unwrap(), "MNQ");
        assert_eq!(fut_pos[0]["point_value"].as_str().unwrap(), "2");
        assert_eq!(fut_pos[0]["tick_size"].as_str().unwrap(), "0.25");
        assert_eq!(fut_pos[0]["quantity"].as_str().unwrap(), "2");

        // PnL query via position_pnl: 2 contracts @ 18000.00, mark at 18010.00 -> (18010 - 18000) * 2 * 2 = 40.00
        let pnl = api_fut.position_pnl("MNQ", "18010.00").unwrap().unwrap();
        assert_eq!(pnl, "40.00");

        // Equity book: AAPL should NOT have point_value or tick_size in positions
        let mut api_eq = SimBookApi::new("ACC", true, "0").unwrap();
        api_eq.connect("2026-03-02T14:29:00+00:00").unwrap();
        let order_eq = serde_json::json!({
            "id": "aapl_buy",
            "instr": "AAPL",
            "otype": "MARKET",
            "side": "BUY",
            "quantity": "10",
            "submitted_at": "2026-03-02T14:29:30+00:00",
            "tif": "DAY",
            "limit": null,
            "stop": null,
            "trail": null,
            "allocs": [{"soid": "s1", "account": "ACC", "qty": "10"}],
            "parent": null,
            "oco": null,
        });
        api_eq.submit(&order_eq.to_string(), "2026-03-02T14:29:30+00:00").unwrap();
        let bar_eq = serde_json::json!({
            "instr": "AAPL",
            "ts": "2026-03-02T14:30:00+00:00",
            "open": "150.00",
            "high": "151.00",
            "low": "149.00",
            "close": "150.50",
            "volume": "100",
            "as_of": "2026-03-02T14:31:00+00:00",
        });
        api_eq.process_bar(Some(&bar_eq.to_string())).unwrap();

        let eq_pos: Vec<serde_json::Value> = serde_json::from_str(
            &api_eq.positions("2026-03-02T14:31:00+00:00").unwrap(),
        )
        .unwrap();
        assert_eq!(eq_pos.len(), 1);
        assert_eq!(eq_pos[0]["symbol"].as_str().unwrap(), "AAPL");
        assert!(eq_pos[0].get("point_value").is_none());
        assert!(eq_pos[0].get("tick_size").is_none());
    }

    #[test]
    fn test_mixed_venue_refusal() {
        // Equity book refuses futures:
        let mut eq_book = SimBookApi::new("ACC", true, "0").unwrap();
        eq_book.connect("2026-03-02T14:29:00+00:00").unwrap();

        let fut_ord = serde_json::json!({
            "id": "nq_on_eq",
            "instr": "NQ",
            "otype": "LIMIT",
            "side": "BUY",
            "quantity": "1",
            "submitted_at": "2026-03-02T14:29:30+00:00",
            "tif": "DAY",
            "limit": "18000.00",
            "stop": null,
            "trail": null,
            "allocs": [{"soid": "s1", "account": "ACC", "qty": "1"}],
            "parent": null,
            "oco": null,
        });
        let e_ord = eq_book.submit(&fut_ord.to_string(), "2026-03-02T14:29:30+00:00").unwrap_err();
        assert_eq!(e_ord.kind, "value");
        assert_eq!(e_ord.msg, "SimBroker accepts equity orders only");

        let fut_bar = serde_json::json!({
            "instr": "NQ",
            "ts": "2026-03-02T14:30:00+00:00",
            "open": "18000.00",
            "high": "18010.00",
            "low": "17990.00",
            "close": "18005.00",
            "volume": "100",
            "as_of": "2026-03-02T14:31:00+00:00",
        });
        let e_bar = eq_book.process_bar(Some(&fut_bar.to_string())).unwrap_err();
        assert_eq!(e_bar.kind, "value");
        assert_eq!(e_bar.msg, "SimBroker supports equities only");

        // Futures book refuses equities:
        let mut fut_book = SimBookApi::new_futures("ACC", 0).unwrap();
        fut_book.connect("2020-11-22T23:00:00+00:00").unwrap();

        let eq_ord = serde_json::json!({
            "id": "aapl_on_fut",
            "instr": "AAPL",
            "otype": "LIMIT",
            "side": "BUY",
            "quantity": "1",
            "submitted_at": "2020-11-22T23:00:00+00:00",
            "tif": "DAY",
            "limit": "150.00",
            "stop": null,
            "trail": null,
            "allocs": [{"soid": "s1", "account": "ACC", "qty": "1"}],
            "parent": null,
            "oco": null,
        });
        let e_fut_ord = fut_book.submit(&eq_ord.to_string(), "2020-11-22T23:00:00+00:00").unwrap_err();
        assert_eq!(e_fut_ord.kind, "value");
        assert_eq!(e_fut_ord.msg, "SimBroker accepts futures orders only");

        let eq_bar = serde_json::json!({
            "instr": "AAPL",
            "ts": "2020-11-22T23:01:00+00:00",
            "open": "150.00",
            "high": "151.00",
            "low": "149.00",
            "close": "150.50",
            "volume": "100",
            "as_of": "2020-11-22T23:02:00+00:00",
        });
        let e_fut_bar = fut_book.process_bar(Some(&eq_bar.to_string())).unwrap_err();
        assert_eq!(e_fut_bar.kind, "value");
        assert_eq!(e_fut_bar.msg, "SimBroker supports futures only");
    }

    #[test]
    fn test_restore_futures_book() {
        let mut api = SimBookApi::new_futures("ACC", 0).unwrap();
        api.connect("2020-11-22T23:00:00+00:00").unwrap();

        let orders_json = serde_json::json!([
            [{
                "id": "rest_ord",
                "instr": "MNQ",
                "otype": "LIMIT",
                "side": "SELL",
                "quantity": "1",
                "submitted_at": "2020-11-22T22:59:00+00:00",
                "tif": "GTC",
                "limit": "18020.00",
                "stop": null,
                "trail": null,
                "allocs": [{"soid": "s1", "account": "ACC", "qty": "1"}],
                "parent": null,
                "oco": null,
            }, "ACCEPTED"]
        ]);

        let positions_json = serde_json::json!([
            {
                "instr": "MNQ",
                "quantity": "1",
                "avg_price": "18000.00",
                "as_of": "2020-11-22T22:59:00+00:00"
            }
        ]);

        api.restore(
            &orders_json.to_string(),
            "[]",
            &positions_json.to_string(),
            "2020-11-22T23:00:00+00:00",
        )
        .unwrap();

        // Check positions were restored with point_value
        let pos_restored: Vec<serde_json::Value> = serde_json::from_str(
            &api.positions("2020-11-22T23:00:00+00:00").unwrap(),
        )
        .unwrap();
        assert_eq!(pos_restored.len(), 1);
        assert_eq!(pos_restored[0]["symbol"].as_str().unwrap(), "MNQ");
        assert_eq!(pos_restored[0]["point_value"].as_str().unwrap(), "2");

        // Feed bar that fills the restored order
        let bar = serde_json::json!({
            "instr": "MNQ",
            "ts": "2020-11-22T23:01:00+00:00",
            "open": "18015.00",
            "high": "18025.00",
            "low": "18010.00",
            "close": "18022.00",
            "volume": "100",
            "as_of": "2020-11-22T23:02:00+00:00",
        });
        let fills = api.process_bar(Some(&bar.to_string())).unwrap();
        assert_eq!(fills, "[0]");
        let f: serde_json::Value = serde_json::from_str(&api.fill(0).unwrap()).unwrap();
        assert_eq!(f["price"].as_str().unwrap(), "18020.00");
    }
}
