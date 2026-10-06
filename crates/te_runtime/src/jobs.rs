//! Runtime owner composition: config, journal recovery, trusted job dispatch.
//! No submitted code, imports, SQL or arbitrary write paths are accepted.
use crate::config::{error, Config};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::path::PathBuf;
use te_host::http::ControlError;
use te_host::jobs::Journal;
use te_host::store::Store;

pub const RUNTIME_VERSION: u32 = 1;

/// The immutable owner identity a control request must match: role, exact
/// configuration generation, allowlisted jobs and the capability secret.
pub struct Identity {
    pub role: String,
    pub generation: String,
    pub instance: String,
    pub jobs: Vec<String>,
    pub capability: String,
}

impl Identity {
    pub fn authorize(&self, capability: Option<&str>) -> Result<(), ControlError> {
        if capability != Some(self.capability.as_str()) {
            return Err(ControlError::new(
                "RuntimeCapabilityError",
                "control capability does not match the owner",
            ));
        }
        Ok(())
    }
}

/// One dedicated owner per ledger: guard precedes any writable open, the
/// journal recovers foreign incomplete records before admission, and the
/// instance identity is the process identity.
pub struct Owner {
    pub store: Store,
    pub journal: Journal,
    pub identity: Identity,
    /// The configured offline ledger path; serve passes it to the HTTP host.
    pub config_ledger: PathBuf,
}

impl Owner {
    pub fn open(config: &Config) -> Result<Self, Value> {
        let Some(runtime) = &config.runtime else {
            return Err(error("runtime owner mode requires a runtime configuration"));
        };
        let role = runtime.role.trim().to_owned();
        let jobs: Vec<String> = runtime
            .jobs
            .iter()
            .map(|job| job.trim().to_owned())
            .collect();
        let allowed: BTreeSet<String> = jobs.iter().cloned().collect();
        let generation = generation(config);
        let instance = std::process::id().to_string();
        let capability = std::fs::read_to_string(&runtime.capability)
            .map(|text| text.trim().to_owned())
            .map_err(|e| error(format!("runtime capability could not be read: {e}")))?;
        let path = &config
            .owner
            .as_ref()
            .expect("validated owner config")
            .ledger_path;
        let store = Store::open(path, &sidecar(path), &instance).map_err(|e| {
            error(match e {
                te_host::store::OpenError::Lock(lock) => {
                    format!("owner store open failed: lock {lock:?}")
                }
                te_host::store::OpenError::Sql(sql) => {
                    format!("owner store open failed: sql {sql}")
                }
            })
        })?;
        let journal = Journal::new(
            store.connection.clone(),
            role.clone(),
            generation.clone(),
            instance.clone(),
            allowed,
            runtime.record_limit,
        )
        .map_err(|e| error(format!("owner journal refused: {e}")))?;
        // Restart: incomplete foreign records become uncertain before any new
        // admission; nothing is auto-replayed.
        journal
            .recover()
            .map_err(|e| error(format!("owner recovery refused: {e}")))?;
        Ok(Self {
            store,
            journal,
            config_ledger: path.clone(),
            identity: Identity {
                role,
                generation,
                instance,
                jobs,
                capability,
            },
        })
    }

    pub fn job_names(&self) -> BTreeSet<String> {
        BTreeSet::from_iter(self.identity.jobs.iter().cloned())
    }
}

/// The immutable configuration generation: content digest of the config the
/// owner was started with. Requests must carry this exact generation.
pub fn generation(config: &Config) -> String {
    let text = serde_json::to_string(config).unwrap_or_default();
    let mut digest: u64 = 0xcbf29ce484222325;
    for byte in text.as_bytes() {
        digest ^= *byte as u64;
        digest = digest.wrapping_mul(0x100000001b3);
    }
    format!("gen:{digest:016x}")
}

/// The sidecar lock path follows the existing T1 convention.
fn sidecar(ledger: &PathBuf) -> PathBuf {
    let mut path = ledger.as_os_str().to_os_string();
    path.push(".lock");
    PathBuf::from(path)
}

pub fn refusal(kind: &str, message: impl Into<String>) -> Value {
    json!({"error": {"type": kind, "message": message.into()}})
}