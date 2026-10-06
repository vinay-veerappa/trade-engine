//! Versioned runtime control: authorize capability/generation, then admit or
//! read. Never self-HTTP; direct owner reads only.
use crate::jobs::Identity;
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use te_host::actor::Actor;
use te_host::http::{Control, ControlError};
use te_host::jobs::{Record, State};

/// JSON shape of one record, preserving exact refusal/result information.
fn record_json(record: &Record) -> Value {
    json!({
        "request_id": record.request_id,
        "state": serde_json::to_value(record.state).unwrap_or_default(),
        "observed_seq": record.observed_seq,
        "committed_seq": record.committed_seq,
        "result": record.result,
        "error": record.error.as_ref().map(|failure| json!({
            "type": failure.r#type,
            "message": failure.message,
        })),
        "instance": record.instance,
    })
}

/// Authorized control over one owner's actor. The capability is compared
/// exactly; a missing capability refuses without admission.
pub struct RuntimeControl {
    identity: Identity,
    actor: Arc<Mutex<Option<Actor>>>,
}

impl RuntimeControl {
    pub fn new(identity: Identity, actor: Arc<Mutex<Option<Actor>>>) -> Self {
        Self { identity, actor }
    }

    /// Authorized submission with the owner's own capability, for the serve
    /// composition check. This is a direct call, never self-HTTP.
    pub fn submit_direct(&self, payload: &Value) -> Result<(Value, bool), ControlError> {
        let body = serde_json::to_vec(payload).expect("payload serialization cannot fail");
        let capability = self.identity.capability.clone();
        self.submit(&body, Some(capability.as_str()))
            .map(|(record, inserted)| (json_value(&record), inserted))
    }

    /// Poll one job's record until it reaches a terminal state; direct read.
    pub fn wait_terminal(&self, id: &str) -> Result<Value, ControlError> {
        loop {
            let record = self.job(id)?;
            let text = te_core::ledger::json::dumps(&record);
            let value: Value = serde_json::from_str(&text).expect("codec JSON is valid");
            if matches!(
                value["state"].as_str(),
                Some("completed" | "refused" | "failed" | "uncertain")
            ) {
                return Ok(value);
            }
            std::thread::sleep(std::time::Duration::from_millis(5));
        }
    }

    /// Stop admission, let the active job finish, close the store and release
    /// the guard last. A blocked stop is reported, never fabricated.
    pub fn stop(&self) -> Result<(), String> {
        let mut actor = self.actor.lock().expect("actor mutex poisoned");
        match actor.as_mut() {
            Some(actor) => actor.stop(),
            None => Ok(()),
        }
    }

    /// Owner identity facts for the serve composition report.
    pub fn identity_facts(&self) -> &Identity {
        &self.identity
    }
}

impl Control for RuntimeControl {
    fn submit(
        &self,
        body: &[u8],
        capability: Option<&str>,
    ) -> Result<(te_core::ledger::json::Json, bool), ControlError> {
        self.identity.authorize(capability)?;
        let payload: Value = serde_json::from_slice(body)
            .map_err(|e| ControlError::new("RuntimeRequestError", e.to_string()))?;
        if !payload.is_object() {
            return Err(ControlError::new(
                "RuntimeRequestError",
                "request body must be a JSON object",
            ));
        }
        let actor = self.actor.lock().expect("actor mutex poisoned");
        let actor = actor
            .as_ref()
            .ok_or_else(|| ControlError::new("RuntimeStoppedError", "owner is stopped"))?;
        let (record, inserted) = actor
            .submit(payload)
            .map_err(|e| ControlError::new(e.kind, e.message))?;
        Ok((to_json(&record_json(&record)), inserted))
    }

    fn job(&self, id: &str) -> Result<te_core::ledger::json::Json, ControlError> {
        let actor = self.actor.lock().expect("actor mutex poisoned");
        let actor = actor
            .as_ref()
            .ok_or_else(|| ControlError::new("RuntimeStoppedError", "owner is stopped"))?;
        let record = actor
            .get(id)
            .map_err(|e| ControlError::new(e.kind, e.message))?;
        match record {
            Some(record) => Ok(to_json(&record_json(&record))),
            None => Err(ControlError::new(
                "RuntimeJobError",
                "job admission was not persisted",
            )),
        }
    }

    fn status(&self) -> Result<te_core::ledger::json::Json, ControlError> {
        let actor = self.actor.lock().expect("actor mutex poisoned");
        let actor = actor
            .as_ref()
            .ok_or_else(|| ControlError::new("RuntimeStoppedError", "owner is stopped"))?;
        let status = actor.status();
        Ok(to_json(&json!({
            "role": self.identity.role,
            "mode": "runtime-owner",
            "generation": self.identity.generation,
            "instance": self.identity.instance,
            "jobs": self.identity.jobs.iter().collect::<Vec<_>>(),
            "running": status.running,
            "queued": status.queued,
            "stopping": status.stopping,
        })))
    }
}

/// Convert a serde_json value into the reduced codec's Json, preserving the
/// exact serialized bytes rather than rebuilding shape.
fn to_json(value: &Value) -> te_core::ledger::json::Json {
    te_core::ledger::json::parse(&serde_json::to_string(value).expect("serde cannot fail"))
        .expect("record JSON is representable in the reduced codec")
}

/// Convert the reduced codec's Json back into a serde value; only used for
/// in-process composition checks, never for wire output.
fn json_value(record: &te_core::ledger::json::Json) -> Value {
    let text = te_core::ledger::json::dumps(record);
    serde_json::from_str(&text).expect("codec JSON is valid")
}

/// The state of one record as the journal spells it, for tests.
pub fn state_of(record: &Record) -> State {
    record.state
}