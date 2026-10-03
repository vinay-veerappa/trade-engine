//! P3b-2b: `OrderManager`'s command flow (`oms/manager.py`), one fn per method.
//!
//! Pure: no Python, no clock read (I7), no I/O. Every effect goes through [`Host`], in
//! the order the Python performs it; the rules are the typed P3b-2a decisions in
//! `oms::manager`, called directly (no JSON between them). A broker call that raises
//! comes back as [`Net::Failed`]: the flow hands the failure to [`Host::cause`] and
//! returns the `broker_unknown` / `oco_unknown` refusal, which the host raises FROM the
//! original exception.
//!
//! Ported so far: the plumbing helpers (`_utc_now`, `_append`, `_context`,
//! `_planned_order`, `_refuse`, `_planned_refusal`, `_venue_order`, `_require_tif`,
//! `_trigger_order_type`, `_bracket_from_orders`). Every other fn is a stub that
//! refuses with kind `unported`; its doc comment names the Python method and the
//! ordering rules the port must keep.
//!
//! Python names, as the flow spells them: an exception is a refusal kind (`key` KeyError,
//! `value` ValueError, `order_management` OrderManagementError, `idempotency`
//! IdempotencyConflictError, `unsupported_order` UnsupportedOrderCapabilityError,
//! `pending_reconciliation` OrderPendingReconciliationError, `broker_unknown`
//! BrokerOutcomeUnknownError, `oco_unknown` OCOOutcomeUnknownError,
//! `order_reconciliation` OrderReconciliationError); `Decimal` is `PyDec`; `TimeInForce`
//! is `Tif`; `TrailingStopEmulator` is `crate::sim::trailing` (`check_trail_amount`, then
//! `update` on a `Trail`).
use crate::ledger::bridge;
use crate::ledger::codec::{enc_obj, enc_order, encode_event};
use crate::ledger::fold::AccountState;
use crate::ledger::json::{dumps, Json};
use crate::ledger::model::{
    check_event, err, DateTime, EmulatedOrderState, Event, EventKind, Fill, Instrument, LErr, Obj, Order,
    OrderState, OrderStateChange, OrderType, Side, Tif, R, SCHEMA_VERSION,
};
use crate::ledger::ops::{le, s, zero};
use crate::ledger::pydec::PyDec;
use crate::oms::manager::{self as plan, Prior, Refusal};

/// A broker call's outcome: the venue answered, or the call raised (the host holds the
/// exception as the cause of the refusal the flow returns).
#[derive(Debug, Clone)]
pub enum Net<T> {
    Ok(T),
    Failed,
}

/// `BrokerCapabilities`, as the decisions read it.
#[derive(Debug, Clone)]
pub struct Capabilities {
    pub types: Vec<OrderType>,
    pub tifs: Vec<Tif>,
    pub native_stops: bool,
}

/// `_OrderContext`: an order, its filled quantity, venue id and emulation, from the
/// first account (in first-event order) whose folded state holds it.
#[derive(Debug, Clone)]
pub struct Context {
    pub order: Order,
    pub filled: PyDec,
    pub venue_order_id: Option<String>,
    pub emulation: Option<EmulatedOrderState>,
}

/// `VenueOrderAllocation`.
#[derive(Debug, Clone)]
pub struct Allocation {
    pub strategy_order_id: String,
    pub account_id: String,
    pub quantity: PyDec,
}

/// `VenueOrder`'s fields. The host constructs the venue's object from them, so its
/// own validation runs where the Python ran it.
#[derive(Debug, Clone)]
pub struct VenueOrder {
    pub venue_order_id: String,
    pub instrument: Instrument,
    pub order_type: OrderType,
    pub side: Side,
    pub quantity: PyDec,
    /// The clock's value as read (not converted to UTC), as `_utc_now()` returned it.
    pub submitted_at: DateTime,
    pub tif: Tif,
    pub limit_price: Option<PyDec>,
    pub stop_price: Option<PyDec>,
    pub trail_amount: Option<PyDec>,
    pub allocations: Vec<Allocation>,
    pub parent_order_id: Option<String>,
    pub oco_group: Option<String>,
}

/// `VenueAck`, as the flow reads it (the status is not validated by the venue).
#[derive(Debug, Clone)]
pub struct VenueAck {
    pub venue_order_id: String,
    pub status: String,
    pub message: Option<String>,
}

/// `OrderChanges`.
#[derive(Debug, Clone, Default)]
pub struct OrderChanges {
    pub new_quantity: Option<PyDec>,
    pub new_limit_price: Option<PyDec>,
    pub new_stop_price: Option<PyDec>,
}

impl OrderChanges {
    /// `repr(changes)`: the replace reasons quote it.
    pub fn repr(&self) -> String {
        let f = |v: &Option<PyDec>| v.as_ref().map_or("None".to_string(), |d| format!("Decimal('{}')", s(d)));
        format!(
            "OrderChanges(new_quantity={}, new_limit_price={}, new_stop_price={})",
            f(&self.new_quantity),
            f(&self.new_limit_price),
            f(&self.new_stop_price)
        )
    }
}

/// `VenueOrderState`. `updated_at` is the venue's `isoformat()`, kept verbatim: the
/// reconcile command ids quote it.
#[derive(Debug, Clone)]
pub struct VenueOrderState {
    pub venue_order_id: String,
    pub state: OrderState,
    pub filled_quantity: PyDec,
    pub remaining_quantity: PyDec,
    pub updated_at: String,
}

/// `VenueFill`.
#[derive(Debug, Clone)]
pub struct VenueFill {
    pub venue_fill_id: String,
    pub venue_order_id: String,
    pub instrument: Instrument,
    pub quantity: PyDec,
    pub price: PyDec,
    pub filled_at: DateTime,
    pub side: Side,
    pub fee: PyDec,
    pub leg_id: Option<String>,
}

/// `Bracket`.
#[derive(Debug, Clone)]
pub struct Bracket {
    pub entry: Order,
    pub stop: Order,
    pub targets: Vec<Order>,
}

/// `OrderIntent`: the typed fields, and the `_wire(intent)` tree the bracket
/// fingerprint hashes.
#[derive(Debug, Clone)]
pub struct Intent {
    pub wire: Json,
    pub intent_id: String,
    pub account_id: String,
    pub instrument: Instrument,
    pub side: Side,
    pub entry_price: PyDec,
    pub stop_loss: PyDec,
    pub profit_targets: Vec<PyDec>,
    pub reason: String,
    pub command_id: String,
    pub entry_tif: Tif,
    pub exit_tif: Tif,
    pub entry_type: OrderType,
    pub entry_limit_price: Option<PyDec>,
    pub target_fractions: Option<Vec<PyDec>>,
}

/// Every effect of the command flow. Each call is one Python statement's effect, made
/// in the Python's order; an exception the host raised crosses back as kind `host`.
pub trait Host {
    /// The venue's own order object (`VenueOrder`), built by [`Host::venue_order`].
    type Venue;
    /// `clock.now_utc()`: its value and whether it is aware (`tzinfo` with an offset).
    fn now_utc(&self) -> R<(DateTime, bool)>;
    /// `ledger.accounts()`, in first-event order.
    fn accounts(&self) -> R<Vec<String>>;
    /// `ledger.state(account)`.
    fn account_state(&self, account: &str) -> R<AccountState>;
    /// `ledger.event_by_command(command_id)`.
    fn event_by_command(&self, command: &str) -> R<Option<Event>>;
    /// `ledger.events_of_kind(kind)`.
    fn events_of_kind(&self, kind: EventKind) -> R<Vec<Event>>;
    /// `ledger.append(event)`: the stored event (with its seq).
    fn append(&self, event: &Event) -> R<Event>;
    /// `broker.capabilities`.
    fn capabilities(&self) -> R<Capabilities>;
    /// `broker.env`.
    fn env(&self) -> R<String>;
    /// `VenueOrder(...)`: the venue object, or the constructor's refusal.
    fn venue_order(&self, order: &VenueOrder) -> R<Self::Venue>;
    /// `broker.submit(venue_order)`.
    fn submit(&self, order: &Self::Venue) -> R<Net<VenueAck>>;
    /// `broker.cancel(venue_order_id)`.
    fn cancel(&self, venue_order_id: &str) -> R<Net<VenueAck>>;
    /// `broker.replace(venue_order_id, changes)`.
    fn replace(&self, venue_order_id: &str, changes: &OrderChanges) -> R<Net<VenueAck>>;
    /// `broker.orders(since)`.
    fn orders(&self, since: &DateTime) -> R<Vec<VenueOrderState>>;
    /// `broker.fills(since)`.
    fn fills(&self, since: &DateTime) -> R<Vec<VenueFill>>;
    /// Hold `e` as the cause of the outcome-unknown refusal the flow returns next. A
    /// `host` error is the exception the host already holds; any other is a refusal
    /// raised inside the guarded call (a `VenueOrder` construction, `_utc_now`).
    fn cause(&self, e: LErr);
}

fn unported<T>(name: &str) -> R<T> {
    err("unported", format!("{name} not ported"))
}

/// The `__post_init__` checks of the payloads the manager constructs.
fn checked(p: &Obj) -> R<()> {
    let nonempty = |v: &str, m: &str| if v.is_empty() { err("payload", m) } else { Ok(()) };
    match p {
        Obj::StateChange(c) => {
            nonempty(&c.order_id, "OrderStateChange.order_id must be non-empty")?;
            if let Some(r) = &c.reason {
                nonempty(r, "OrderStateChange.reason must be non-empty when provided")?;
            }
        }
        Obj::OrdersCreated(c) => {
            if c.orders.is_empty() {
                return err("payload", "OrdersCreated.orders must not be empty");
            }
            nonempty(&c.fingerprint, "OrdersCreated.fingerprint must be non-empty")?;
            nonempty(&c.reason, "OrdersCreated.reason must be non-empty")?;
            let mut ids: Vec<&str> = c.orders.iter().map(|o| o.order_id.as_str()).collect();
            ids.sort_unstable();
            ids.dedup();
            if ids.len() != c.orders.len() {
                return err("payload", "OrdersCreated.order_id values must be unique");
            }
            let mut accounts: Vec<&str> = c.orders.iter().map(|o| o.account_id.as_str()).collect();
            accounts.sort_unstable();
            accounts.dedup();
            if accounts.len() != 1 {
                return err("payload", "OrdersCreated orders must belong to one account");
            }
        }
        Obj::OrderUpdated(u) => nonempty(&u.reason, "OrderUpdated.reason must be non-empty")?,
        Obj::Emulated(e) => {
            nonempty(&e.order_id, "EmulatedOrderState.order_id must be non-empty")?;
            for (v, name) in [(&e.observed_price, "observed_price"), (&e.extreme, "extreme"), (&e.stop_price, "stop_price")] {
                if let Some(v) = v {
                    if le(v, &zero())? {
                        return err("payload", format!("EmulatedOrderState.{name} must be positive"));
                    }
                }
            }
            nonempty(&e.reason, "EmulatedOrderState.reason must be non-empty")?;
        }
        _ => {}
    }
    Ok(())
}

// --- plumbing (ported) -------------------------------------------------------------------

/// `_utc_now`: the clock's value, refused when naive. Not converted: the event's
/// `ts_utc` is converted where the event is built, a venue order keeps the value.
pub fn utc_now<H: Host>(h: &H) -> R<DateTime> {
    let (now, aware) = h.now_utc()?;
    if !aware {
        return err("value", "Clock returned a naive datetime");
    }
    Ok(now)
}

/// `_append`: build the event (clock read first), then a replay of its command id
/// returns the stored event when the payload matches and refuses when it does not;
/// otherwise the event is appended.
pub fn append<H: Host>(h: &H, account: &str, kind: EventKind, payload: Obj, command: &str) -> R<Event> {
    checked(&payload)?;
    let now = utc_now(h)?;
    let mut event = Event {
        account: account.to_string(),
        kind,
        payload,
        ts_utc: now,
        command_id: Some(command.to_string()),
        schema_version: SCHEMA_VERSION,
        seq: None,
    };
    if account.is_empty() {
        return err("payload", "Event.account must be non-empty");
    }
    event.ts_utc = event.ts_utc.to_utc()?;
    check_event(&event)?;
    if let Some(existing) = h.event_by_command(command)? {
        plan::append_replay(&Prior::of(&existing), account, kind, &event.payload, command)?;
        return Ok(existing);
    }
    h.append(&event)
}

/// `_context`: the first account (in first-event order) whose state holds the order.
pub fn context<H: Host>(h: &H, order_id: &str) -> R<Context> {
    for account in h.accounts()? {
        let state = h.account_state(&account)?;
        if let Some(order) = state.orders.get(order_id) {
            return Ok(Context {
                order: order.clone(),
                filled: state.filled_quantity.get(order_id).cloned().unwrap_or_else(zero),
                venue_order_id: state.venue_order_ids.get(order_id).cloned(),
                emulation: state.emulated_orders.get(order_id).cloned(),
            });
        }
    }
    err("key", format!("Unknown order_id '{order_id}'"))
}

/// `get_order`.
pub fn get_order<H: Host>(h: &H, order_id: &str) -> R<Order> {
    Ok(context(h, order_id)?.order)
}

/// `_capabilities`.
pub fn capabilities<H: Host>(h: &H) -> R<Capabilities> {
    h.capabilities()
}

/// `_planned_order`: the order as its ORDERS_CREATED event planned it.
pub fn planned_order<H: Host>(h: &H, order_id: &str) -> R<Order> {
    for event in h.events_of_kind(EventKind::OrdersCreated)? {
        if event.kind == EventKind::OrdersCreated {
            if let Obj::OrdersCreated(created) = &event.payload {
                if let Some(o) = created.orders.iter().find(|o| o.order_id == order_id) {
                    return Ok(o.clone());
                }
            }
        }
    }
    err("key", format!("No creation event contains order '{order_id}'"))
}

/// `_planned_quantity`.
pub fn planned_quantity<H: Host>(h: &H, order_id: &str) -> R<PyDec> {
    Ok(planned_order(h, order_id)?.quantity)
}

/// `_refuse`: an ORDER_REFUSED event for the order.
pub fn refuse<H: Host>(h: &H, order: &Order, reason: &str, command: &str) -> R<Event> {
    let payload = Obj::StateChange(OrderStateChange {
        order_id: order.order_id.clone(),
        reason: Some(reason.to_string()),
        venue_order_id: None,
    });
    append(h, &order.account_id, EventKind::OrderRefused, payload, command)
}

/// `_planned_refusal`: record the refusal (under `command`, else the order's own
/// command id, with the plan's suffix), then refuse. It never returns Ok.
pub fn planned_refusal<H: Host, T>(h: &H, order: &Order, mode: Refusal, command: Option<&str>) -> R<T> {
    let (reason, suffix, refusal) = plan::refused(order, mode);
    refuse(h, order, &reason, &format!("{}:{suffix}", command.unwrap_or(&order.command_id)))?;
    Err(refusal)
}

/// `_venue_order`'s fields: the order as it is, or (a triggered emulated order) the
/// venue type and limit with no stop or trail. The clock is read here.
pub fn venue_terms<H: Host>(h: &H, order: &Order, trigger: Option<(OrderType, Option<PyDec>)>) -> R<VenueOrder> {
    let submitted_at = utc_now(h)?;
    let (order_type, limit_price, stop_price, trail_amount) = match trigger {
        None => (order.order_type, order.limit_price.clone(), order.stop_price.clone(), order.trail_amount.clone()),
        Some((t, limit)) => (t, limit, None, None),
    };
    Ok(VenueOrder {
        venue_order_id: order.order_id.clone(),
        instrument: order.instrument.clone(),
        order_type,
        side: order.side,
        quantity: order.quantity.clone(),
        submitted_at,
        tif: order.tif,
        limit_price,
        stop_price,
        trail_amount,
        allocations: vec![Allocation {
            strategy_order_id: order.order_id.clone(),
            account_id: order.account_id.clone(),
            quantity: order.quantity.clone(),
        }],
        parent_order_id: order.parent_order_id.clone(),
        oco_group: order.oco_group.clone(),
    })
}

/// `_venue_order`: the fields, then the venue object (its constructor validates).
pub fn venue_order<H: Host>(h: &H, order: &Order, trigger: Option<(OrderType, Option<PyDec>)>) -> R<(VenueOrder, H::Venue)> {
    let terms = venue_terms(h, order, trigger)?;
    let venue = h.venue_order(&terms)?;
    Ok((terms, venue))
}

/// `_trigger_order_type`: MARKET, else LIMIT; neither is a recorded refusal.
pub fn trigger_order_type<H: Host>(h: &H, order: &Order) -> R<OrderType> {
    let caps = h.capabilities()?;
    match plan::trigger_type(&caps.types) {
        Some(t) => Ok(t),
        None => planned_refusal(h, order, Refusal::Trigger, None),
    }
}

/// `_supports_native_type`.
pub fn supports_native_type<H: Host>(h: &H, t: OrderType) -> R<bool> {
    let caps = h.capabilities()?;
    Ok(plan::supports_native(t, &caps.types, caps.native_stops))
}

/// `_require_tif`: an unsupported time in force is a recorded refusal; an unsupported
/// trigger type refuses outright.
pub fn require_tif<H: Host>(h: &H, order: &Order, venue_type: Option<OrderType>) -> R<()> {
    let caps = h.capabilities()?;
    if plan::tif(order.tif, venue_type, &caps.types, &caps.tifs)? {
        return planned_refusal(h, order, Refusal::Tif, None);
    }
    Ok(())
}

/// `_stored_order`: the stored order, else the given one (an unknown id only).
pub fn stored_order<H: Host>(h: &H, order: &Order) -> R<Order> {
    match get_order(h, &order.order_id) {
        Ok(o) => Ok(o),
        Err(e) if e.kind == "key" => Ok(order.clone()),
        Err(e) => Err(e),
    }
}

/// `_bracket_from_orders`: each order as stored (first position, last value per id),
/// then the plan picks the entry, stop and targets.
pub fn bracket_from_orders<H: Host>(h: &H, orders: &[Order]) -> R<Bracket> {
    let mut by_id: Vec<Order> = Vec::new();
    for o in orders {
        let stored = stored_order(h, o)?;
        match by_id.iter_mut().find(|v| v.order_id == o.order_id) {
            Some(slot) => *slot = stored,
            None => by_id.push(stored),
        }
    }
    let refs: Vec<&Order> = by_id.iter().collect();
    let (entry, stop, targets) = plan::bracket(&refs)?;
    let find = |id: &str| {
        by_id.iter().find(|o| o.order_id == id).cloned().ok_or_else(|| LErr { kind: "key", msg: format!("'{id}'") })
    };
    Ok(Bracket { entry: find(&entry)?, stop: find(&stop)?, targets: targets.iter().map(|t| find(t)).collect::<R<_>>()? })
}

// --- commands (stubs) ----------------------------------------------------------------------

/// `create_bracket`. Refuse a non-positive quantity, then unsupported bracket
/// capabilities, BEFORE the fingerprint and the replay lookup. A replay of the command
/// id returns the stored bracket only when the fingerprint matches (`created_replay`).
/// Otherwise the clock is read once (`created_at` of every order), the quantity is
/// validated, targets are split or fractioned, and ONE ORDERS_CREATED event persists
/// entry, stop and targets (durable before any venue call).
pub fn create_bracket<H: Host>(_h: &H, _intent: &Intent, _quantity: &PyDec) -> R<Bracket> {
    unported("create_bracket")
}

/// `submit`. `_ensure_stored` first (validate the quantity, persist a standalone order
/// or refuse a changed one), then a child is held (a recorded refusal) until its parent
/// has a fill; then emulate (`_start_emulation`) or send natively (`_submit_native`).
pub fn submit<H: Host>(h: &H, order: &Order) -> R<Order> {
    ensure_stored(h, order)?;
    let ctx = context(h, &order.order_id)?;
    let current = ctx.order;
    if let Some(parent_id) = &current.parent_order_id {
        let parent_ctx = context(h, parent_id)?;
        let filled = h.account_state(&current.account_id)?
            .filled_quantity
            .get(&parent_ctx.order.order_id)
            .cloned()
            .unwrap_or_else(zero);
        if plan::child_hold(&filled)? {
            return planned_refusal(h, &current, Refusal::Child(&parent_ctx.order.order_id), None);
        }
    }
    let caps = capabilities(h)?;
    if plan::emulates(current.order_type, &caps.types, caps.native_stops) {
        return start_emulation(h, &current);
    }
    submit_native(h, &current)
}

/// `submit_trailing`: refuse a non-TRAIL order (`trailing_check`), then `submit`.
pub fn submit_trailing<H: Host>(h: &H, order: &Order) -> R<Order> {
    plan::trailing_check(order.order_type, "submit_trailing", "", &[])?;
    submit(h, order)
}

/// `update_trailing`: the order, `trailing_check` against the venue's types (a native
/// trail has no local trail), then `update_emulated_order`.
pub fn update_trailing<H: Host>(_h: &H, _order_id: &str, _price: &PyDec, _command: &str) -> R<Order> {
    unported("update_trailing")
}

/// `update_emulated_order`. Refuse a bad price before the context; `emulation_check`;
/// a replayed command id is checked (`observation_replay`) and routes a recorded
/// trigger that has not been sent. A new observation is recorded (ORDER_EMULATION_UPDATED)
/// BEFORE the trigger is routed, so a crash after the record re-routes on replay.
pub fn update_emulated_order<H: Host>(_h: &H, _order_id: &str, _price: &PyDec, _command: &str) -> R<Order> {
    unported("update_emulated_order")
}

/// `_route_emulated_trigger`: the venue type (a STOP_LIMIT at its limit, else
/// `_trigger_order_type`), `_require_tif`, then the OCO siblings are cancelled BEFORE
/// the triggered order is submitted (`_cancel_emulated_siblings`, then `_submit_emulated`).
pub fn route_emulated_trigger<H: Host>(_h: &H, _order: &Order, _trigger_price: Option<&PyDec>, _command: &str) -> R<Order> {
    unported("route_emulated_trigger")
}

/// `_cancel_emulated_siblings`: the order's OCO siblings in folded-state order, through
/// `_cancel_exits` under `{command}:{order_id}:trigger`.
pub fn cancel_emulated_siblings<H: Host>(_h: &H, _order: &Order, _command: &str) -> R<()> {
    unported("cancel_emulated_siblings")
}

/// `record_fill`. `fill_match` (account and venue env), validate the quantity, append
/// the FILL under `fill:{fill_id}`, then `_synchronize_bracket`: fills are recorded
/// before any protection is resized.
pub fn record_fill<H: Host>(_h: &H, _fill: &Fill) -> R<Order> {
    unported("record_fill")
}

/// `cancel`. A protective stop with open quantity is a recorded refusal (under the
/// caller's command id); then `_cancel_order`; then a parentless order synchronizes
/// its bracket.
pub fn cancel<H: Host>(_h: &H, _order_id: &str, _command: &str) -> R<Order> {
    unported("cancel")
}

/// `move_stop`: `_open_bracket_stop`, then `move_stop` (stops only tighten; an
/// unchanged price returns the stop), then `replace` with the new stop price.
pub fn move_stop<H: Host>(_h: &H, _entry: &str, _stop_price: &PyDec, _command: &str) -> R<Order> {
    unported("move_stop")
}

/// `close_bracket`. An existing close order is a replay (`close_replay`) or a conflict;
/// then `_open_bracket_stop`; a new close is guarded (`close_guard`), persisted as
/// ORDERS_CREATED (durable before network), then submitted.
pub fn close_bracket<H: Host>(_h: &H, _entry: &str, _command: &str, _reason: &str) -> R<Order> {
    unported("close_bracket")
}

/// `reduce_bracket`. Refuse a bad fraction, read the entry, fingerprint, then a replay
/// resumes `_send_reduce` on the stored reduce. Otherwise `_open_bracket_stop`, the
/// planned `reduce` size and id, ORDERS_CREATED (durable before network), `_send_reduce`.
pub fn reduce_bracket<H: Host>(_h: &H, _entry: &str, _fraction: &PyDec, _command: &str, _reason: &str) -> R<Order> {
    unported("reduce_bracket")
}

/// `_send_reduce`: cancel targets before submitting a reduce (the LIMIT children,
/// through `_cancel_exits` under `{command}:replaces-targets`), then `submit`.
pub fn send_reduce<H: Host>(_h: &H, _reduce: &Order, _command: &str) -> R<Order> {
    unported("send_reduce")
}

/// `_bracket_children`: the entry's children, in folded-state order.
pub fn bracket_children<H: Host>(_h: &H, _entry: &str) -> R<Vec<Order>> {
    unported("bracket_children")
}

/// `_open_bracket_stop`: the entry, its account state, then `open_stop`.
pub fn open_bracket_stop<H: Host>(_h: &H, _entry: &str) -> R<(Order, PyDec)> {
    unported("open_bracket_stop")
}

/// `replace`. A NEW emulated stop is replaced locally. Replays: the `:pending` request
/// is checked before the `:noop` one. Then `replace_state`, `_replacement_terms`; a
/// no-op records ORDER_UPDATED under `:noop`. Otherwise the confirmed venue id is
/// required, ORDER_PENDING is appended BEFORE `broker.replace` (durable before
/// network); a raise is BrokerOutcomeUnknownError from the original exception. The ack:
/// pending records ORDER_PENDING, rejected records ORDER_REFUSED then restores the
/// working state, then refuses; accepted records ORDER_UPDATED with the prior state.
pub fn replace<H: Host>(_h: &H, _order_id: &str, _changes: &OrderChanges, _command: &str) -> R<Order> {
    unported("replace")
}

/// `_replace_emulated_stop`: a `:local` replay is checked (`replace_replay` mode
/// local); otherwise `_replacement_terms` and ORDER_UPDATED under `{command}:local`.
pub fn replace_emulated_stop<H: Host>(_h: &H, _order: &Order, _changes: &OrderChanges, _command: &str) -> R<Order> {
    unported("replace_emulated_stop")
}

/// `reconcile_order`. Read back with `broker.orders(created_at)`, never resend. A
/// pending order with an unresolved replace resolves only on a terminal read-back.
/// Fills before terminal state: missing fills are ingested BEFORE the state event,
/// because the ledger refuses a fill on a cancelled order (I1). Then the state event
/// (command id quoting `updated_at.isoformat()`) and `_synchronize_bracket`.
pub fn reconcile_order<H: Host>(_h: &H, _order_id: &str) -> R<Order> {
    unported("reconcile_order")
}

/// `_ingest_venue_fills`: `broker.fills(created_at)`, the fills with the found venue id
/// recorded in venue order through `record_fill`, then `ingest_check`.
pub fn ingest_venue_fills<H: Host>(_h: &H, _order: &Order, _found: &VenueOrderState) -> R<()> {
    unported("ingest_venue_fills")
}

/// `_submit_native`. Not NEW returns the order. Unsupported type, then time in force,
/// are recorded refusals. ORDER_UPDATED (SUBMITTED) and ORDER_PENDING are appended
/// BEFORE the network call (durable-before-network); building the venue order and
/// `broker.submit` share one guard, and a failure of either is
/// BrokerOutcomeUnknownError from the original exception. Then `_record_submit_ack`.
pub fn submit_native<H: Host>(h: &H, order: &Order) -> R<Order> {
    use crate::ledger::model::OrderUpdated;
    if order.state != OrderState::New {
        return Ok(order.clone());
    }
    if !supports_native_type(h, order.order_type)? {
        planned_refusal(h, order, Refusal::Native, None)?;
    }
    require_tif(h, order, None)?;
    let submitted = {
        let mut o = order.clone();
        o.state = OrderState::Submitted;
        o
    };
    append(
        h,
        &submitted.account_id,
        EventKind::OrderUpdated,
        Obj::OrderUpdated(OrderUpdated {
            order: submitted.clone(),
            reason: "Order submission requested".into(),
            venue_order_id: None,
        }),
        &format!("{}:submit", order.command_id),
    )?;
    mark_pending(
        h,
        &submitted,
        "Submit outcome is pending until the venue responds",
        &format!("{}:submit-pending", order.command_id),
    )?;
    let broker_msg = format!(
        "Submit outcome for order '{}' is unknown; it will not be resent",
        order.order_id
    );
    let ack = match venue_order(h, order, None) {
        Ok((_, venue)) => match h.submit(&venue) {
            Ok(Net::Ok(ack)) => ack,
            Ok(Net::Failed) => return err("broker_unknown", broker_msg),
            // a BaseException from the broker (not an Exception) propagates as is
            Err(e) => return Err(e),
        },
        Err(e) => {
            h.cause(e);
            return err("broker_unknown", broker_msg);
        }
    };
    record_submit_ack(
        h,
        order,
        &ack,
        &format!("{}:submit-result", order.command_id),
    )?;
    get_order(h, &order.order_id)
}

/// `_start_emulation`. `start_emulation` plans it (a pending order refuses; STOP_LIMIT
/// without LIMIT is a recorded refusal), the trigger type is resolved and
/// `_require_tif` checked BEFORE the first ORDER_EMULATION_UPDATED is recorded.
pub fn start_emulation<H: Host>(_h: &H, _order: &Order) -> R<Order> {
    unported("start_emulation")
}

/// `_submit_emulated`. The current order; `submit_emulated`; `_require_tif`; then
/// ORDER_UPDATED (SUBMITTED) and `_mark_pending` BEFORE the venue order is built and
/// sent (durable before network). The venue order is built OUTSIDE the guard; only
/// `broker.submit` raising is BrokerOutcomeUnknownError. Then `_record_submit_ack`.
pub fn submit_emulated<H: Host>(
    _h: &H,
    _order: &Order,
    _trigger_price: Option<&PyDec>,
    _command: &str,
    _venue_type: OrderType,
    _limit_price: Option<&PyDec>,
) -> R<Order> {
    unported("submit_emulated")
}

/// `_record_submit_ack`: `submit_ack` names the event and reason; an unrecognized
/// status refuses before anything is recorded.
pub fn record_submit_ack<H: Host>(h: &H, order: &Order, ack: &VenueAck, command: &str) -> R<()> {
    let (kind, reason) = plan::submit_ack(&ack.status, ack.message.as_deref().unwrap_or(""))?;
    let payload = Obj::StateChange(OrderStateChange {
        order_id: order.order_id.clone(),
        reason: Some(reason),
        venue_order_id: Some(ack.venue_order_id.clone()),
    });
    append(h, &order.account_id, kind, payload, command)?;
    Ok(())
}

/// `_synchronize_bracket`. Protection sizing before target budgets: the stop is resized
/// to the open quantity BEFORE the target budgets are computed, so a budget refusal
/// cannot move ahead of that durable operation. A flat bracket cancels every exit;
/// a stop fill then cancels the targets and closers.
pub fn synchronize_bracket<H: Host>(_h: &H, _changed: &Order, _cause: &str) -> R<()> {
    unported("synchronize_bracket")
}

/// `_ensure_child_quantity`: `child_quantity` plans return, submit, a local resize
/// (ORDER_UPDATED under `:local-size`, then `_submit_child`) or a venue `replace`.
pub fn ensure_child_quantity<H: Host>(_h: &H, _child: &Order, _quantity: &PyDec, _command: &str) -> R<()> {
    unported("ensure_child_quantity")
}

/// `_submit_child`: a NEW child is submitted; a rejected protective stop then refuses.
pub fn submit_child<H: Host>(_h: &H, _child: &Order) -> R<()> {
    unported("submit_child")
}

/// `_cancel_exits` (and `_cancel_targets`): each exit re-read, terminal ones skipped,
/// cancelled as an OCO sibling, then `oco_check`, in order.
pub fn cancel_exits<H: Host>(_h: &H, _exits: &[Order], _command: &str) -> R<()> {
    unported("cancel_exits")
}

/// `_cancel_order`. `cancel_mode`: terminal returns; NEW cancels locally; otherwise the
/// confirmed venue id is required and `_mark_pending` is recorded BEFORE
/// `broker.cancel` (durable before network). A raise is OCOOutcomeUnknownError (an
/// OCO sibling) or BrokerOutcomeUnknownError, from the original exception. The ack:
/// accepted records ORDER_CANCELLED; rejected records ORDER_REFUSED then refuses;
/// pending records ORDER_PENDING then refuses an OCO sibling.
pub fn cancel_order<H: Host>(_h: &H, _order: &Order, _command: &str, _reason: &str, _oco: bool) -> R<Order> {
    unported("cancel_order")
}

/// `_restore_working_state`: ORDER_UPDATED with the previous state and the venue id.
pub fn restore_working_state<H: Host>(
    _h: &H,
    _order: &Order,
    _previous: OrderState,
    _reason: &str,
    _venue_order_id: &str,
    _command: &str,
) -> R<()> {
    unported("restore_working_state")
}

/// `_mark_pending`: the current order; already PENDING_UNKNOWN records nothing;
/// otherwise ORDER_PENDING with the context's venue id.
pub fn mark_pending<H: Host>(h: &H, order: &Order, reason: &str, command: &str) -> R<()> {
    let current = get_order(h, &order.order_id)?;
    if plan::pending_state(current.state) {
        return Ok(());
    }
    let ctx = context(h, &current.order_id)?;
    let payload = Obj::StateChange(OrderStateChange {
        order_id: current.order_id.clone(),
        reason: Some(reason.to_string()),
        venue_order_id: ctx.venue_order_id,
    });
    append(h, &current.account_id, EventKind::OrderPending, payload, command)?;
    Ok(())
}

/// `_ensure_stored`. Validate the quantity; an unknown order is persisted as a
/// standalone ORDERS_CREATED under its own command id; a stored one must match its
/// planned order (`stored`).
pub fn ensure_stored<H: Host>(h: &H, order: &Order) -> R<()> {
    use crate::ledger::model::OrdersCreated;
    plan::validate_quantity(&order.instrument, &order.quantity)?;
    match get_order(h, &order.order_id) {
        Err(e) if e.kind == "key" => {
            let payload = Obj::OrdersCreated(OrdersCreated {
                orders: vec![order.clone()],
                fingerprint: plan::fingerprint_order(order)?,
                reason: "Standalone strategy order created".into(),
            });
            append(h, &order.account_id, EventKind::OrdersCreated, payload, &order.command_id)?;
            Ok(())
        }
        Err(e) => Err(e),
        Ok(_) => {
            let created = planned_order(h, &order.order_id)?;
            plan::stored(&created, order)
        }
    }
}

/// `_has_unresolved_replace`: an ORDER_PENDING replace request whose `:accepted` and
/// `:rejected` commands are both absent.
pub fn has_unresolved_replace<H: Host>(_h: &H, _order_id: &str) -> R<bool> {
    unported("has_unresolved_replace")
}

// --- the door the host calls -------------------------------------------------------------

fn field<'a>(j: &'a Json, k: &str) -> R<&'a Json> {
    j.get(k).ok_or_else(|| LErr { kind: "value", msg: format!("flow request missing {k}") })
}
fn text<'a>(j: &'a Json, k: &str) -> R<&'a str> {
    match field(j, k)? {
        Json::Str(s) => Ok(s),
        _ => err("value", format!("flow request {k} is not a string")),
    }
}
fn otext<'a>(j: &'a Json, k: &str) -> R<Option<&'a str>> {
    match field(j, k)? {
        Json::Null => Ok(None),
        Json::Str(s) => Ok(Some(s)),
        _ => err("value", format!("flow request {k} is not a string")),
    }
}
fn flag(j: &Json, k: &str) -> R<bool> {
    match field(j, k)? {
        Json::Bool(b) => Ok(*b),
        _ => err("value", format!("flow request {k} is not a bool")),
    }
}
fn decimal(v: &str) -> R<PyDec> {
    PyDec::parse(v).ok_or_else(|| LErr { kind: "value", msg: format!("flow request: not a Decimal: {v:?}") })
}
fn dec(j: &Json, k: &str) -> R<PyDec> {
    decimal(text(j, k)?)
}
fn odec(j: &Json, k: &str) -> R<Option<PyDec>> {
    otext(j, k)?.map(decimal).transpose()
}
fn decs(j: &Json) -> R<Vec<PyDec>> {
    match j {
        Json::Arr(a) => a
            .iter()
            .map(|v| match v {
                Json::Str(s) => decimal(s),
                _ => err("value", "flow request: expected a decimal string"),
            })
            .collect(),
        _ => err("value", "flow request: expected a list"),
    }
}
fn obj(j: &Json) -> R<Obj> {
    bridge::obj_from_text(&dumps(j))
}
fn order_of(j: &Json) -> R<Order> {
    match obj(j)? {
        Obj::Order(o) => Ok(o),
        _ => err("value", "flow request: expected an order"),
    }
}
fn order(j: &Json, k: &str) -> R<Order> {
    order_of(field(j, k)?)
}
fn orders(j: &Json, k: &str) -> R<Vec<Order>> {
    match field(j, k)? {
        Json::Arr(a) => a.iter().map(order_of).collect(),
        _ => err("value", "flow request: expected a list of orders"),
    }
}
fn order_type(v: &str) -> R<OrderType> {
    OrderType::parse(v).ok_or_else(|| LErr { kind: "value", msg: format!("flow request: bad order type {v:?}") })
}
fn otype(j: &Json, k: &str) -> R<Option<OrderType>> {
    otext(j, k)?.map(order_type).transpose()
}
fn tif(v: &str) -> R<Tif> {
    Tif::parse(v).ok_or_else(|| LErr { kind: "value", msg: format!("flow request: bad time in force {v:?}") })
}
fn instrument(j: &Json) -> R<Instrument> {
    match obj(j)? {
        Obj::Instr(i) => Ok(i),
        _ => err("value", "flow request: expected an instrument"),
    }
}
fn changes(j: &Json) -> R<OrderChanges> {
    Ok(OrderChanges {
        new_quantity: odec(j, "new_quantity")?,
        new_limit_price: odec(j, "new_limit_price")?,
        new_stop_price: odec(j, "new_stop_price")?,
    })
}

/// The intent's `_wire` tree: enum values, decimal strings, the instrument's tree.
pub fn intent(j: &Json) -> R<Intent> {
    let side = text(j, "side")?;
    Ok(Intent {
        wire: j.clone(),
        intent_id: text(j, "intent_id")?.to_string(),
        account_id: text(j, "account_id")?.to_string(),
        instrument: instrument(field(j, "instrument")?)?,
        side: Side::parse(side).ok_or_else(|| LErr { kind: "value", msg: format!("flow request: bad side {side:?}") })?,
        entry_price: dec(j, "entry_price")?,
        stop_loss: dec(j, "stop_loss")?,
        profit_targets: decs(field(j, "profit_targets")?)?,
        reason: text(j, "reason")?.to_string(),
        command_id: text(j, "command_id")?.to_string(),
        entry_tif: tif(text(j, "entry_tif")?)?,
        exit_tif: tif(text(j, "exit_tif")?)?,
        entry_type: order_type(text(j, "entry_type")?)?,
        entry_limit_price: odec(j, "entry_limit_price")?,
        target_fractions: match field(j, "target_fractions")? {
            Json::Null => None,
            v => Some(decs(v)?),
        },
    })
}

fn fill(j: &Json) -> R<Fill> {
    match obj(field(j, "fill")?)? {
        Obj::Fill(f) => Ok(f),
        _ => err("value", "flow request: expected a fill"),
    }
}

fn found(j: &Json) -> R<VenueOrderState> {
    let state = text(j, "state")?;
    Ok(VenueOrderState {
        venue_order_id: text(j, "venue_order_id")?.to_string(),
        state: OrderState::parse(state).ok_or_else(|| LErr { kind: "value", msg: format!("flow request: bad state {state:?}") })?,
        filled_quantity: dec(j, "filled_quantity")?,
        remaining_quantity: dec(j, "remaining_quantity")?,
        updated_at: text(j, "updated_at")?.to_string(),
    })
}

fn js(v: impl Into<String>) -> Json {
    Json::Str(v.into())
}
fn ojs(v: &Option<String>) -> Json {
    v.as_ref().map_or(Json::Null, |s| js(s.as_str()))
}
fn jdec(v: &PyDec) -> Json {
    js(s(v))
}
fn ojdec(v: &Option<PyDec>) -> Json {
    v.as_ref().map_or(Json::Null, jdec)
}

/// A venue order as plain fields: decimals as strings, the instant as `isoformat()`,
/// the instrument as its codec tree.
pub fn venue_json(v: &VenueOrder) -> R<Json> {
    Ok(Json::Obj(vec![
        ("venue_order_id".into(), js(v.venue_order_id.as_str())),
        ("instrument".into(), enc_obj(&Obj::Instr(v.instrument.clone()))?),
        ("order_type".into(), js(v.order_type.value())),
        ("side".into(), js(v.side.value())),
        ("quantity".into(), jdec(&v.quantity)),
        ("submitted_at".into(), js(v.submitted_at.iso())),
        ("tif".into(), js(v.tif.value())),
        ("limit_price".into(), ojdec(&v.limit_price)),
        ("stop_price".into(), ojdec(&v.stop_price)),
        ("trail_amount".into(), ojdec(&v.trail_amount)),
        (
            "allocations".into(),
            Json::Arr(
                v.allocations
                    .iter()
                    .map(|a| {
                        Json::Obj(vec![
                            ("strategy_order_id".into(), js(a.strategy_order_id.as_str())),
                            ("account_id".into(), js(a.account_id.as_str())),
                            ("quantity".into(), jdec(&a.quantity)),
                        ])
                    })
                    .collect(),
            ),
        ),
        ("parent_order_id".into(), ojs(&v.parent_order_id)),
        ("oco_group".into(), ojs(&v.oco_group)),
    ]))
}

/// `OrderChanges` as plain fields (decimal strings or null).
pub fn changes_json(c: &OrderChanges) -> Json {
    Json::Obj(vec![
        ("new_quantity".into(), ojdec(&c.new_quantity)),
        ("new_limit_price".into(), ojdec(&c.new_limit_price)),
        ("new_stop_price".into(), ojdec(&c.new_stop_price)),
    ])
}

fn bracket_json(b: &Bracket) -> R<Json> {
    Ok(Json::Arr(vec![
        enc_order(&b.entry)?,
        enc_order(&b.stop)?,
        Json::Arr(b.targets.iter().map(enc_order).collect::<R<_>>()?),
    ]))
}

fn refusal_mode<'a>(j: &'a Json) -> R<Refusal<'a>> {
    Ok(match text(j, "mode")? {
        "child" => Refusal::Child(text(j, "parent")?),
        "protective" => Refusal::Protective,
        "native" => Refusal::Native,
        "limit" => Refusal::Limit,
        "trigger" => Refusal::Trigger,
        "tif" => Refusal::Tif,
        other => return err("value", format!("flow request: unknown refusal mode {other:?}")),
    })
}

fn none() -> R<Json> {
    Ok(Json::Null)
}

/// One `OrderManager` method by name. Requests and results are plain JSON: orders,
/// fills and instruments as codec trees, decimals as strings. An Order result is its
/// tree, a Bracket `[entry, stop, [targets]]`, an Event its encoded form.
pub fn run<H: Host>(op: &str, h: &H, j: &Json) -> R<Json> {
    let id = || text(j, "order_id");
    let command = || text(j, "command_id");
    match op {
        // plumbing
        "utc_now" => Ok(Json::Obj(vec![("T".into(), js(utc_now(h)?.iso()))])),
        "append" => {
            let kind = text(j, "kind")?;
            let kind = EventKind::parse(kind).ok_or_else(|| LErr { kind: "value", msg: format!("flow request: bad kind {kind:?}") })?;
            encode_event(&append(h, text(j, "account")?, kind, obj(field(j, "payload")?)?, command()?)?)
        }
        "get_order" => enc_order(&get_order(h, id()?)?),
        "context" => {
            let c = context(h, id()?)?;
            Ok(Json::Obj(vec![
                ("order".into(), enc_order(&c.order)?),
                ("filled".into(), jdec(&c.filled)),
                ("venue_order_id".into(), ojs(&c.venue_order_id)),
                ("emulation".into(), match &c.emulation {
                    Some(e) => enc_obj(&Obj::Emulated(e.clone()))?,
                    None => Json::Null,
                }),
            ]))
        }
        "planned_order" => enc_order(&planned_order(h, id()?)?),
        "refuse" => encode_event(&refuse(h, &order(j, "order")?, text(j, "reason")?, command()?)?),
        "planned_refusal" => {
            let o = order(j, "order")?;
            planned_refusal::<H, Json>(h, &o, refusal_mode(j)?, otext(j, "command_id")?)
        }
        "venue_order" => {
            let o = order(j, "order")?;
            let trigger = match otype(j, "order_type")? {
                None => None,
                Some(t) => Some((t, odec(j, "limit_price")?)),
            };
            venue_json(&venue_order(h, &o, trigger)?.0)
        }
        "trigger_order_type" => Ok(js(trigger_order_type(h, &order(j, "order")?)?.value())),
        "supports_native_type" => Ok(Json::Bool(supports_native_type(h, order_type(text(j, "type")?)?)?)),
        "require_tif" => {
            require_tif(h, &order(j, "order")?, otype(j, "venue_type")?)?;
            none()
        }
        "bracket_from_orders" => bracket_json(&bracket_from_orders(h, &orders(j, "orders")?)?),
        // commands
        "create_bracket" => bracket_json(&create_bracket(h, &intent(field(j, "intent")?)?, &dec(j, "quantity")?)?),
        "submit" => enc_order(&submit(h, &order(j, "order")?)?),
        "submit_trailing" => enc_order(&submit_trailing(h, &order(j, "order")?)?),
        "update_trailing" => enc_order(&update_trailing(h, id()?, &dec(j, "price")?, command()?)?),
        "update_emulated_order" => enc_order(&update_emulated_order(h, id()?, &dec(j, "price")?, command()?)?),
        "route_emulated_trigger" => enc_order(&route_emulated_trigger(h, &order(j, "order")?, odec(j, "price")?.as_ref(), command()?)?),
        "cancel_emulated_siblings" => {
            cancel_emulated_siblings(h, &order(j, "order")?, command()?)?;
            none()
        }
        "record_fill" => enc_order(&record_fill(h, &fill(j)?)?),
        "cancel" => enc_order(&cancel(h, id()?, command()?)?),
        "move_stop" => enc_order(&move_stop(h, text(j, "entry")?, &dec(j, "stop_price")?, command()?)?),
        "close_bracket" => enc_order(&close_bracket(h, text(j, "entry")?, command()?, text(j, "reason")?)?),
        "reduce_bracket" => enc_order(&reduce_bracket(h, text(j, "entry")?, &dec(j, "fraction")?, command()?, text(j, "reason")?)?),
        "send_reduce" => enc_order(&send_reduce(h, &order(j, "order")?, command()?)?),
        "bracket_children" => Ok(Json::Arr(bracket_children(h, text(j, "entry")?)?.iter().map(enc_order).collect::<R<_>>()?)),
        "open_bracket_stop" => {
            let (stop, open) = open_bracket_stop(h, text(j, "entry")?)?;
            Ok(Json::Arr(vec![enc_order(&stop)?, jdec(&open)]))
        }
        "replace" => enc_order(&replace(h, id()?, &changes(field(j, "changes")?)?, command()?)?),
        "replace_emulated_stop" => enc_order(&replace_emulated_stop(h, &order(j, "order")?, &changes(field(j, "changes")?)?, command()?)?),
        "reconcile_order" => enc_order(&reconcile_order(h, id()?)?),
        "ingest_venue_fills" => {
            ingest_venue_fills(h, &order(j, "order")?, &found(field(j, "found")?)?)?;
            none()
        }
        "submit_native" => enc_order(&submit_native(h, &order(j, "order")?)?),
        "start_emulation" => enc_order(&start_emulation(h, &order(j, "order")?)?),
        "submit_emulated" => enc_order(&submit_emulated(
            h,
            &order(j, "order")?,
            odec(j, "trigger_price")?.as_ref(),
            command()?,
            order_type(text(j, "venue_type")?)?,
            odec(j, "limit_price")?.as_ref(),
        )?),
        "synchronize_bracket" => {
            synchronize_bracket(h, &order(j, "order")?, text(j, "cause")?)?;
            none()
        }
        "ensure_child_quantity" => {
            ensure_child_quantity(h, &order(j, "order")?, &dec(j, "quantity")?, command()?)?;
            none()
        }
        "submit_child" => {
            submit_child(h, &order(j, "order")?)?;
            none()
        }
        "cancel_exits" => {
            cancel_exits(h, &orders(j, "orders")?, command()?)?;
            none()
        }
        "cancel_order" => enc_order(&cancel_order(h, &order(j, "order")?, command()?, text(j, "reason")?, flag(j, "oco")?)?),
        "mark_pending" => {
            mark_pending(h, &order(j, "order")?, text(j, "reason")?, command()?)?;
            none()
        }
        "ensure_stored" => {
            ensure_stored(h, &order(j, "order")?)?;
            none()
        }
        "has_unresolved_replace" => Ok(Json::Bool(has_unresolved_replace(h, id()?)?)),
        _ => err("value", format!("unknown flow operation {op}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn changes_repr_quotes_decimals_as_python_does() {
        let c = OrderChanges { new_quantity: PyDec::parse("5"), new_limit_price: None, new_stop_price: PyDec::parse("99.50") };
        assert_eq!(c.repr(), "OrderChanges(new_quantity=Decimal('5'), new_limit_price=None, new_stop_price=Decimal('99.50'))");
        assert_eq!(OrderChanges::default().repr(), "OrderChanges(new_quantity=None, new_limit_price=None, new_stop_price=None)");
    }
}
