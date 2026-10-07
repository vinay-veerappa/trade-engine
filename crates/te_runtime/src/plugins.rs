//! Offline composition proof over one native store. No strategy/provider job runs.
use crate::config::{Config, OwnerConfig};
use pyo3::{exceptions::PyRuntimeError, prelude::*, types::PyDict};
use serde_json::Value;

fn compose(py: Python<'_>, config: &Config, owner: &OwnerConfig) -> PyResult<Value> {
    let native = py.import("trade_engine_rs")?;
    if native
        .getattr("__spec__")?
        .getattr("origin")?
        .extract::<String>()?
        != "built-in"
        || native.hasattr("__file__")?
    {
        return Err(PyRuntimeError::new_err(
            "trade_engine_rs must be the built-in module",
        ));
    }
    let engine = py.import("trade_engine")?;
    let actual: std::path::PathBuf = engine.getattr("__file__")?.extract()?;
    if std::fs::canonicalize(actual)?
        != std::fs::canonicalize(
            config
                .engine_source
                .join("trade_engine")
                .join("__init__.py"),
        )?
    {
        return Err(PyRuntimeError::new_err(
            "trade_engine source provenance mismatch",
        ));
    }
    let clock = match owner.clock.as_str() {
        "replay" => {
            let initial = py
                .import("datetime")?
                .getattr("datetime")?
                .call_method1("fromisoformat", (owner.initial_time.as_ref().unwrap(),))?;
            py.import("trade_engine.clock")?
                .getattr("ReplayClock")?
                .call1((initial,))?
        }
        "wall" => py
            .import("trade_engine.clock")?
            .getattr("WallClock")?
            .call0()?,
        _ => unreachable!("owner clock validated before Python initialization"),
    };
    let ledger = py
        .import("trade_engine.ledger.store")?
        .getattr("Ledger")?
        .call1((owner.ledger_path.to_string_lossy().as_ref(),))?;
    ledger.call_method0("open")?;
    let result = (|| -> PyResult<Value> {
        let json = py.import("json")?;
        let cfg = json.call_method1("loads", (config.plugin_config.to_string(),))?;
        let bundle = trade_engine_rs::plugins::runtime_load_factory(
            py,
            &config.plugin_module,
            &config.plugin_factory,
            config.plugin_paths.clone(),
            &ledger,
            &clock,
            &cfg,
        )?;
        let report = PyDict::new(py);
        report.set_item("result", bundle.bind(py).getattr("config")?)?;
        report.set_item(
            "strategy_count",
            bundle.bind(py).getattr("strategies")?.len()?,
        )?;
        let adapters = bundle.bind(py).getattr("adapters")?;
        report.set_item(
            "adapter_count",
            if adapters.is_none() {
                0
            } else {
                adapters.len()?
            },
        )?;
        report.set_item(
            "module_origin",
            native.getattr("__spec__")?.getattr("origin")?,
        )?;
        report.set_item("module_file", py.None())?;
        report.set_item(
            "builtin_module_count",
            py.import("sys")?
                .getattr("builtin_module_names")?
                .call_method1("count", ("trade_engine_rs",))?,
        )?;
        report.set_item(
            "clock_now",
            clock.call_method0("now_utc")?.call_method0("isoformat")?,
        )?;
        report.set_item("event_count", ledger.call_method0("count")?)?;
        report.set_item("owner_held", ledger.getattr("_native")?.getattr("held")?)?;
        let encoded: String = json.call_method1("dumps", (report,))?.extract()?;
        serde_json::from_str(&encoded).map_err(|e| PyRuntimeError::new_err(e.to_string()))
    })();
    ledger.call_method0("close")?;
    result
}

pub fn proof(py: Python<'_>, config: &Config) -> PyResult<Value> {
    compose(
        py,
        config,
        config.owner.as_ref().expect("validated owner config"),
    )
}
