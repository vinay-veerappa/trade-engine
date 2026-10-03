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
    if !text.starts_with('{') && !text.starts_with('[') {
        if !text.is_empty() {
            return Ok(Instrument::Equity(text.to_string()));
        } else {
            return fail("value", "expected an instrument");
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
            if map.contains_key("dc") {
                let text =
                    serde_json::to_string(v).map_err(|e| ApiError::new("value", e.to_string()))?;
                parse_instrument(&text)
            } else if let Some(sym) = map.get("symbol").and_then(|s| s.as_str()) {
                Ok(Instrument::Equity(sym.to_string()))
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
