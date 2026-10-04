//! The thinkorswim paperMoney mirror's pure decisions (docs/RUST_PORT.md P5; was
//! `src/trade_engine/tos_paper/`). Pure: no I/O, no clock (I7), money is `PyDec` carried
//! as strings (D6). The host transports and the broker's state machine stay Python until
//! T9/T10; what lives here is what the Python decided from plain data.
//!
//! One door, [`decide`]`(op, json)`, takes the arguments as one JSON document and
//! answers one. A refusal is an [`LErr`] whose `kind` names the Python exception
//! (`tos_normalize` -> `NormalizeError`, `tos_slippage` -> `SlippageError`,
//! `tos_unsupported` -> `UnsupportedCapability`, `value` -> `ValueError`, and the
//! decimal kinds), its message exactly the Python's.

pub mod netting;
pub mod normalize;
pub mod pytables;
pub mod pytext;
pub mod reconcile;
pub mod slippage;
pub mod transport;
pub mod wire;

use crate::ledger::json::{self, Json};
use crate::ledger::model::{err, LErr};

/// The Python exception kinds a mirror refusal crosses as.
pub const NORMALIZE: &str = "tos_normalize";
pub const SLIPPAGE: &str = "tos_slippage";
pub const UNSUPPORTED: &str = "tos_unsupported";
pub const VALUE: &str = "value";
/// `int(Infinity)`: Python's `OverflowError`.
pub const OVERFLOW_ERROR: &str = "tos_overflow_error";
/// A host bug (a malformed door document), never a Python parity case.
pub const WIRE: &str = "tos_wire";

/// `op` over the JSON document `text`; the answer as JSON text.
pub fn decide(op: &str, text: &str) -> Result<String, LErr> {
    let doc = json::parse(text).map_err(|e| LErr { kind: WIRE, msg: format!("bad door json: {}", e.message) })?;
    let out: Json = match op {
        "ticket_validate" => transport::ticket_validate(&doc)?,
        "ticket_for" => transport::ticket_for(&doc)?,
        "vertical_reason" => netting::vertical_reason_op(&doc)?,
        "place_result" => normalize::place_result(&doc)?,
        "place_exception" => normalize::place_exception(&doc)?,
        "placed_order_id" => normalize::placed_order_id(&doc)?,
        "cancel_result" => normalize::cancel_result(&doc)?,
        "cancel_exception" => normalize::cancel_exception(&doc)?,
        "working_order" => normalize::working_order(&doc)?,
        "book_state" => normalize::book_state_op(&doc)?,
        "order_fill" => normalize::order_fill(&doc)?,
        "position" => normalize::position(&doc)?,
        "allocate_venue_fill" => slippage::allocate_venue_fill(&doc)?,
        "slippage_report" => slippage::slippage_report(&doc)?,
        "position_book" => reconcile::position_book_op(&doc)?,
        "reconcile" => reconcile::reconcile_op(&doc)?,
        "unreadable" => reconcile::unreadable_op(&doc)?,
        "confirm_ticket" => reconcile::confirm_ticket_op(&doc)?,
        "ticket_contracts" => reconcile::ticket_contracts_op(&doc)?,
        "text_probe" => wire::text_probe(&doc)?,
        _ => return err(WIRE, format!("unknown tos_paper op {op:?}")),
    };
    Ok(json::dumps(&out))
}
