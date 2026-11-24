//! Offline composition proof over one native store. No strategy/provider job runs.
use crate::config::{Config, OwnerConfig};
use pyo3::{exceptions::PyRuntimeError, prelude::*, types::PyDict};
use serde_json::Value;
use std::path::Path;

/// The build/serve skew guard (W6): te.exe embeds `trade_engine_rs` at the
/// commit it was built from, while `trade_engine` is imported from the
/// configured `engine_source` checkout — in production a separate git
/// worktree. When the two differ, jobs fail at run time with attribute
/// errors (`trade_engine_rs has no attribute oms_brackets_to_resync`,
/// 2026-11-24), so the owner refuses to serve instead. Pure and unit-tested.
fn skew_refusal(built: &str, head: Option<&str>, dirty_head: bool) -> Option<String> {
    let head_text = head.unwrap_or("unknown");
    if built == "unknown" {
        return Some(format!(
            "te.exe was built without a git sha (built sha: unknown, engine_source HEAD: {head_text})"
        ));
    }
    if built.ends_with("-dirty") {
        return Some(format!(
            "te.exe was built from a dirty checkout (built sha: {built}, engine_source HEAD: {head_text})"
        ));
    }
    let Some(head) = head else {
        return Some(format!(
            "engine_source is not a git checkout (built sha: {built}, engine_source HEAD: unknown)"
        ));
    };
    if dirty_head {
        return Some(format!(
            "engine_source has uncommitted changes (built sha: {built}, engine_source HEAD: {head})"
        ));
    }
    if built != head {
        return Some(format!(
            "te.exe and engine_source are on different commits (built sha: {built}, engine_source HEAD: {head})"
        ));
    }
    None
}

fn git(engine_source: &Path, args: &[&str]) -> Option<String> {
    let output = std::process::Command::new("git")
        .arg("-C")
        .arg(engine_source)
        .args(args)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

/// The skew check itself (W6): the built-in module's `__git_sha__` against
/// `engine_source`'s HEAD and tracked working tree.
fn verify_no_skew(py: Python<'_>, config: &Config) -> PyResult<()> {
    let built: String = py
        .import("trade_engine_rs")?
        .getattr("__git_sha__")?
        .extract()?;
    let head = git(&config.engine_source, &["rev-parse", "HEAD"]);
    // A failed status cannot prove a clean tree; treat it as dirty.
    let dirty_head = match git(
        &config.engine_source,
        &["status", "--porcelain", "--untracked-files=no"],
    ) {
        Some(status) => !status.is_empty(),
        None => true,
    };
    match skew_refusal(&built, head.as_deref(), dirty_head) {
        Some(message) => Err(PyRuntimeError::new_err(format!("build skew: {message}"))),
        None => Ok(()),
    }
}

/// The imported `trade_engine` is the configured `engine_source` checkout, never an installed copy.
/// The proof and the owner's serve both refuse otherwise: until serve checked, an owner ran a stale
/// copy installed in the client's venv beside the checkout it was configured to run.
pub fn verify_engine_source(py: Python<'_>, config: &Config) -> PyResult<()> {
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
    if config.skew_guard.unwrap_or(false) {
        verify_no_skew(py, config)?;
    }
    Ok(())
}

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
    verify_engine_source(py, config)?;
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

#[cfg(test)]
mod skew_tests {
    use super::skew_refusal;

    const BUILT: &str = "58190de92b39b8f6b298e3c7ccd8e62b0e88ee5c";
    const OTHER: &str = "0123456789abcdef0123456789abcdef01234567";

    #[test]
    fn a_clean_matching_head_serves() {
        assert_eq!(skew_refusal(BUILT, Some(BUILT), false), None);
    }

    #[test]
    fn a_build_without_git_refuses() {
        assert_eq!(
            skew_refusal("unknown", Some(BUILT), false),
            Some(
                "te.exe was built without a git sha (built sha: unknown, \
engine_source HEAD: 58190de92b39b8f6b298e3c7ccd8e62b0e88ee5c)"
                    .to_owned()
            )
        );
    }

    #[test]
    fn a_dirty_build_refuses() {
        assert_eq!(
            skew_refusal(&format!("{BUILT}-dirty"), Some(BUILT), false),
            Some(
                "te.exe was built from a dirty checkout \
(built sha: 58190de92b39b8f6b298e3c7ccd8e62b0e88ee5c-dirty, \
engine_source HEAD: 58190de92b39b8f6b298e3c7ccd8e62b0e88ee5c)"
                    .to_owned()
            )
        );
    }

    #[test]
    fn an_engine_source_without_git_refuses() {
        assert_eq!(
            skew_refusal(BUILT, None, false),
            Some(
                "engine_source is not a git checkout \
(built sha: 58190de92b39b8f6b298e3c7ccd8e62b0e88ee5c, engine_source HEAD: unknown)"
                    .to_owned()
            )
        );
    }

    #[test]
    fn a_dirty_engine_source_refuses() {
        assert_eq!(
            skew_refusal(BUILT, Some(BUILT), true),
            Some(
                "engine_source has uncommitted changes \
(built sha: 58190de92b39b8f6b298e3c7ccd8e62b0e88ee5c, \
engine_source HEAD: 58190de92b39b8f6b298e3c7ccd8e62b0e88ee5c)"
                    .to_owned()
            )
        );
    }

    #[test]
    fn a_skewed_head_refuses() {
        assert_eq!(
            skew_refusal(BUILT, Some(OTHER), false),
            Some(
                "te.exe and engine_source are on different commits \
(built sha: 58190de92b39b8f6b298e3c7ccd8e62b0e88ee5c, \
engine_source HEAD: 0123456789abcdef0123456789abcdef01234567)"
                    .to_owned()
            )
        );
    }
}
