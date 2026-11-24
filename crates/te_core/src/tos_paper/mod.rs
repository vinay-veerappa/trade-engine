//! The thinkorswim paperMoney mirror's pure decisions (docs/RUST_PORT.md P5; was
//! `src/trade_engine/tos_paper/`). Pure: no I/O, no clock (I7), money is `Money` carried
//! as strings (D6). The host transports and the broker's state machine stay Python until
//! T9/T10; what lives here is what the Python decided from plain data.
//!
//! A refusal is an `LErr` whose `kind` names the Python exception
//! (`tos_normalize` -> `NormalizeError`, `tos_slippage` -> `SlippageError`,
//! `tos_unsupported` -> `UnsupportedCapability`, `value` -> `ValueError`, and the
//! decimal kinds), its message exactly the Python's.

pub mod broker;
pub mod cover;
pub mod exits;
pub mod follow;
pub mod netting;
pub mod normalize;
pub mod pytables;
pub mod pytext;
pub mod reconcile;
pub mod session;
pub mod slippage;
pub mod transport;
pub mod wire;

/// The Python exception kinds a mirror refusal crosses as.
pub const NORMALIZE: &str = "tos_normalize";
pub const SLIPPAGE: &str = "tos_slippage";
pub const UNSUPPORTED: &str = "tos_unsupported";
pub const VALUE: &str = "value";
/// `int(Infinity)`: Python's `OverflowError`.
pub const OVERFLOW_ERROR: &str = "tos_overflow_error";
/// A host bug (a malformed door document), never a Python parity case.
/// `NettingError`.
pub const NETTING: &str = "tos_netting_error";
/// `ExitPlanError`.
pub const EXIT_PLAN: &str = "tos_exit_plan_error";
pub const WIRE: &str = "tos_wire";
/// `TosPaperBrokerError`.
pub const BROKER: &str = "tos_broker_error";
/// `VenueUnreadable`: the message is the drifting `VenueReconcile` as JSON.
pub const VENUE_UNREADABLE: &str = "tos_venue_unreadable";
/// An exception the host transport raised and the broker lets through (`TransportUnavailable`):
/// the host re-raises its own exception object.
pub const RAISED: &str = "tos_raised";
/// `MirrorSessionError`.
pub const SESSION: &str = "tos_session_error";
