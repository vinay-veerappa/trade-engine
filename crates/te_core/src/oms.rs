//! The OMS rules that read the folded account (docs/RUST_PORT.md P3a): which options
//! structures are open, and which short calls nothing covers. The command -> events core
//! (`oms/manager.py`, the rest of `oms/options.py`) follows in P3b.

pub mod options;
pub mod flow;
pub mod manager;
pub mod reconcile;
pub mod restore;
pub mod structures;
