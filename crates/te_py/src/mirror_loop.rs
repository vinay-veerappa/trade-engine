//! The mirror follower loop binding (P4c T12): the pacing/cutoff/collect
//! loop lives in `te_core::mirror`; this is the one door from Python. The
//! host object supplies now/sleep/probe/cycle/collect/log; the loop's
//! behavior (budgets, cutoffs, once-mode, classification) is Rust's,
//! mirroring the frozen Python loop byte for byte in effect.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyAnyMethods;
use te_core::mirror::{self, FollowOutcome, FollowPlan, FollowReport, Probe};
struct Host<'py> {
    object: pyo3::Bound<'py, PyAny>,
}

impl<'py> mirror::FollowHost for Host<'py> {
    fn now_local_seconds(&mut self) -> f64 {
        self.object
            .call_method0("now_local_seconds")
            .and_then(|value| value.extract())
            .expect("the mirror host's now_local_seconds must answer a float")
    }

    fn sleep(&mut self, seconds: f64) {
        self.object
            .call_method1("sleep", (seconds,))
            .expect("the mirror host's sleep must accept the interval");
    }

    fn probe(&mut self) -> Probe {
        let answer = self
            .object
            .call_method0("probe")
            .expect("the mirror host's probe must answer");
        if answer.is_none() {
            return Probe::Readable;
        }
        match answer.extract::<Option<String>>() {
            Ok(Some(reason)) => Probe::Unreadable(reason),
            Ok(None) => Probe::Readable,
            Err(_) => Probe::Readable,
        }
    }

    fn cycle(&mut self) -> Result<FollowReport, String> {
        let answer = self
            .object
            .call_method0("cycle")
            .map_err(|error| error.to_string())?;
        let text = answer
            .extract::<String>()
            .map_err(|error| error.to_string())?;
        let value: serde_json::Value =
            serde_json::from_str(&text).map_err(|error| error.to_string())?;
        mirror::FollowReport::from_wire(&value)
            .ok_or_else(|| "the cycle report could not be read".to_owned())
    }

    fn collect(&mut self) -> Result<FollowReport, String> {
        let answer = self
            .object
            .call_method0("collect")
            .map_err(|error| error.to_string())?;
        let text = answer
            .extract::<String>()
            .map_err(|error| error.to_string())?;
        let value: serde_json::Value =
            serde_json::from_str(&text).map_err(|error| error.to_string())?;
        mirror::FollowReport::from_collect_wire(&value)
            .ok_or_else(|| "the collect report could not be read".to_owned())
    }

    fn log(&mut self, line: &str) {
        self.object
            .call_method1("log", (line,))
            .expect("the mirror host's log must accept the line");
    }
}

/// Run one follower's loop. `host_json` configures the window and pacing:
/// {"send_open": seconds, "send_close": seconds, "collect_at": seconds,
/// "interval_seconds": float, "once": bool, "max_unreadable": int}.
/// The host object implements now_local_seconds/sleep/probe/cycle/collect/log;
/// probe answers None when the venue reads, the reason when it does not;
/// cycle/collect answer the report as its JSON text. Returns the report
/// line plus the outcome's ok flag as {"ok": bool}.
#[pyfunction]
#[pyo3(signature = (host, host_json))]
fn mirror_follow_loop(
    py: Python<'_>,
    host: pyo3::Bound<'_, PyAny>,
    host_json: String,
) -> PyResult<pyo3::Py<PyAny>> {
    let config: serde_json::Value = serde_json::from_str(&host_json)
        .map_err(|e| PyRuntimeError::new_err(format!("RuntimeRequestError: {e}")))?;
    let number = |key: &str| -> PyResult<f64> {
        config[key]
            .as_f64()
            .ok_or_else(|| PyRuntimeError::new_err(format!(
                "RuntimeRequestError: the mirror plan lacks '{key}'"
            )))
    };
    let plan = FollowPlan {
        send_open: number("send_open")?,
        send_close: number("send_close")?,
        collect_at: number("collect_at")?,
        interval_seconds: number("interval_seconds")?,
        once: config["once"].as_bool().unwrap_or(false),
        max_unreadable: config["max_unreadable"].as_u64().unwrap_or(10) as u32,
    };
    // The loop runs synchronously under the GIL, exactly as the Python
    // loop did: every host effect is a Python callback (the venue's
    // transport), so releasing the GIL buys nothing and the follow loop
    // was never concurrent with another thread's ledger work.
    let outcome = {
        let mut driver = Host { object: host };
        mirror::follow_loop(&mut driver, &plan)
    };
    let FollowOutcome::Done(ok) = outcome;
    let dict = pyo3::types::PyDict::new(py);
    dict.set_item("ok", ok)?;
    Ok(dict.into_any().unbind())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(mirror_follow_loop, m)?)?;
    Ok(())
}