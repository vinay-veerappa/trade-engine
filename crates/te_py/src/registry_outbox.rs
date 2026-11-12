//! BROKER_PLATFORM phase 4a: the door into `te_core::registry_outbox`.
//!
//! Every rule (ordering, ack, gap, compaction, cap, halt) lives in `te_core`. A refusal
//! crosses as `ValueError((kind, message))`; the message never carries a response body
//! or a token. The class is `Send`: the Python sink serialises calls with its own lock.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use te_core::ledger::model::LErr;
use te_core::registry_outbox as ro;

fn refuse(e: LErr) -> PyErr {
    PyValueError::new_err((e.kind.to_string(), e.msg))
}

#[pyclass]
struct RegistryOutbox {
    inner: ro::Outbox,
}

#[pymethods]
impl RegistryOutbox {
    #[new]
    fn new(broker: &str, stale_after_s: i64, cap_bytes: usize, max_tail_events: usize) -> PyResult<RegistryOutbox> {
        Ok(RegistryOutbox { inner: ro::Outbox::new(broker, stale_after_s, cap_bytes, max_tail_events).map_err(refuse)? })
    }

    #[staticmethod]
    fn from_json(text: &str, broker: &str, stale_after_s: i64, cap_bytes: usize, max_tail_events: usize) -> PyResult<RegistryOutbox> {
        Ok(RegistryOutbox {
            inner: ro::Outbox::from_json(text, broker, stale_after_s, cap_bytes, max_tail_events).map_err(refuse)?,
        })
    }

    fn observe(&mut self, snapshot_json: &str, events_json: &str, head_seq: u64, confirmed_at: &str) -> PyResult<()> {
        self.inner.observe(snapshot_json, events_json, head_seq, confirmed_at).map_err(refuse)
    }

    fn next_envelope(&mut self, now: &str) -> PyResult<Option<String>> {
        self.inner.next_envelope(now).map_err(refuse)
    }

    fn on_response(&mut self, status: u16, body: &str, now: &str) -> PyResult<String> {
        self.inner.on_response(status, body, now).map(str::to_string).map_err(refuse)
    }

    fn to_json(&self) -> String {
        self.inner.to_json()
    }

    fn stats(&self) -> String {
        self.inner.stats_json()
    }

    fn reset(&mut self) {
        self.inner.reset();
    }

    fn resume(&mut self) {
        self.inner.resume();
    }

    fn halted(&self) -> Option<String> {
        self.inner.halted().map(str::to_string)
    }

    fn head_seq(&self) -> u64 {
        self.inner.head_seq()
    }

    fn acked_seq(&self) -> u64 {
        self.inner.acked_seq()
    }
}

pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<RegistryOutbox>()?;
    Ok(())
}
