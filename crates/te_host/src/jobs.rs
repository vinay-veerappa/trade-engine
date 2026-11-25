//! Durable control metadata over the owner's existing connection, never a writer open.
use crate::store::SharedConnection;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{collections::BTreeSet, fmt};

pub const PREFIX: &str = "te.runtime.jobs.v1:";
const TIMES_PREFIX: &str = "te.runtime.jobtimes.v1:";
const VERSION: u32 = 1;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    pub kind: &'static str,
    pub message: String,
}
impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.kind, self.message)
    }
}
impl std::error::Error for Error {}
impl From<rusqlite::Error> for Error {
    fn from(error: rusqlite::Error) -> Self {
        Self {
            kind: "RuntimeStoreError",
            message: error.to_string(),
        }
    }
}
fn refuse<T>(kind: &'static str, message: impl Into<String>) -> Result<T, Error> {
    Err(Error {
        kind,
        message: message.into(),
    })
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum State {
    Queued,
    Running,
    Completed,
    Refused,
    Failed,
    Uncertain,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Failure {
    pub r#type: String,
    pub message: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Record {
    pub version: u32,
    pub request_id: String,
    pub instance: String,
    pub payload: Value,
    pub state: State,
    pub observed_seq: i64,
    pub committed_seq: i64,
    pub result: Option<Value>,
    pub error: Option<Failure>,
}

/// Timestamps for one job, stored under a sibling meta key so an older te.exe
/// (which decodes every `te.runtime.jobs.v1:*` record with `deny_unknown_fields`)
/// never sees these fields on the record itself. Each value is an RFC 3339 UTC
/// string with milliseconds, or absent.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct JobTimes {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub admitted_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub started_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finished_at: Option<String>,
}

impl JobTimes {
    /// Whole milliseconds between `started_at` and `finished_at`, when both are
    /// known and parse as RFC 3339 instants; `None` otherwise.
    pub fn duration_ms(&self) -> Option<i64> {
        let started = chrono::DateTime::parse_from_rfc3339(self.started_at.as_deref()?).ok()?;
        let finished = chrono::DateTime::parse_from_rfc3339(self.finished_at.as_deref()?).ok()?;
        Some(finished.signed_duration_since(started).num_milliseconds())
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub version: u32,
    pub request_id: String,
    pub generation: String,
    pub role: String,
    pub job: String,
    pub session: Option<String>,
    pub pass: Option<String>,
    pub through: Option<String>,
    pub options: Value,
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_.:-".contains(&byte))
}
fn decode(text: &str, key: &str) -> Result<Record, Error> {
    let record: Record = serde_json::from_str(text).map_err(|error| Error {
        kind: "RuntimeRecordError",
        message: error.to_string(),
    })?;
    if record.version != VERSION {
        return refuse(
            "RuntimeVersionError",
            format!("unsupported job record version: {}", record.version),
        );
    }
    if !identifier(&record.request_id) || key != format!("{PREFIX}{}", record.request_id) {
        return refuse(
            "RuntimeRecordError",
            "job record key does not match its request ID",
        );
    }
    Ok(record)
}
fn sequence(connection: &Connection) -> Result<i64, Error> {
    Ok(
        connection.query_row("SELECT COALESCE(MAX(seq),0) FROM events", [], |row| {
            row.get(0)
        })?,
    )
}
fn load(connection: &Connection, id: &str) -> Result<Option<Record>, Error> {
    let key = format!("{PREFIX}{id}");
    let text: Option<String> = connection
        .query_row("SELECT value FROM meta WHERE key=?", [&key], |row| {
            row.get(0)
        })
        .optional()?;
    text.map(|text| decode(&text, &key)).transpose()
}
fn save(connection: &Connection, record: &Record) -> Result<(), Error> {
    let text = serde_json::to_string(record).map_err(|error| Error {
        kind: "RuntimeRecordError",
        message: error.to_string(),
    })?;
    connection.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        rusqlite::params![format!("{PREFIX}{}", record.request_id), text],
    )?;
    Ok(())
}

fn now_utc() -> String {
    crate::clock::wall_utc_rfc3339_millis()
}

fn times_key(id: &str) -> String {
    format!("{TIMES_PREFIX}{id}")
}

fn load_times(connection: &Connection, id: &str) -> Result<JobTimes, Error> {
    let key = times_key(id);
    let text: Option<String> = connection
        .query_row("SELECT value FROM meta WHERE key=?", [&key], |row| {
            row.get(0)
        })
        .optional()?;
    // A record written before this change has no times key: all-absent, never
    // an error.
    match text {
        Some(text) => serde_json::from_str(&text).map_err(|error| Error {
            kind: "RuntimeRecordError",
            message: error.to_string(),
        }),
        None => Ok(JobTimes::default()),
    }
}

fn save_times(connection: &Connection, id: &str, times: &JobTimes) -> Result<(), Error> {
    let text = serde_json::to_string(times).map_err(|error| Error {
        kind: "RuntimeRecordError",
        message: error.to_string(),
    })?;
    connection.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        rusqlite::params![times_key(id), text],
    )?;
    Ok(())
}

/// Deletes up to `needed` terminal job records, oldest `committed_seq` first
/// (ties by key), and returns how many were deleted.
fn prune_terminal(connection: &Connection, needed: u64) -> Result<u64, Error> {
    let records = {
        let mut statement = connection
            .prepare("SELECT key,value FROM meta WHERE key GLOB 'te.runtime.jobs.v1:*'")?;
        let rows = statement
            .query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)))?;
        rows.collect::<Result<Vec<_>, _>>()?
    };
    let mut terminal = Vec::new();
    for (key, text) in records {
        let record = decode(&text, &key)?;
        if matches!(record.state, State::Completed | State::Refused | State::Failed) {
            terminal.push((record.committed_seq, key));
        }
    }
    terminal.sort();
    let mut deleted = 0;
    for (_, key) in terminal.into_iter().take(needed as usize) {
        deleted += connection.execute("DELETE FROM meta WHERE key=?", [&key])? as u64;
        // The timestamps live under a sibling key; retire it with the record so
        // no orphaned times survive a pruned request ID.
        let id = key.strip_prefix(PREFIX).unwrap_or(&key);
        connection.execute("DELETE FROM meta WHERE key=?", [&times_key(id)])?;
    }
    Ok(deleted)
}

pub struct Journal {
    connection: SharedConnection,
    role: String,
    generation: String,
    instance: String,
    jobs: BTreeSet<String>,
    limit: usize,
}
impl Journal {
    pub fn new(
        connection: SharedConnection,
        role: String,
        generation: String,
        instance: String,
        jobs: BTreeSet<String>,
        limit: usize,
    ) -> Result<Self, Error> {
        if !identifier(&role)
            || !identifier(&generation)
            || !identifier(&instance)
            || jobs.is_empty()
            || jobs.iter().any(|job| !identifier(job))
            || limit == 0
        {
            return refuse(
                "RuntimeConfigError",
                "role/generation/instance/jobs and a positive record limit are required",
            );
        }
        Ok(Self {
            connection,
            role,
            generation,
            instance,
            jobs,
            limit,
        })
    }

    fn write<T>(
        &self,
        operation: impl FnOnce(&Connection) -> Result<T, Error>,
    ) -> Result<T, Error> {
        let mut owner = self.connection.lock().map_err(|_| Error {
            kind: "RuntimeStoreError",
            message: "owner connection mutex poisoned".into(),
        })?;
        let connection = owner.as_mut().ok_or_else(|| Error {
            kind: "RuntimeStoppedError",
            message: "owner connection is closed".into(),
        })?;
        // Never acknowledge control metadata hidden in a job's uncommitted batch.
        if !connection.is_autocommit() {
            return refuse(
                "RuntimeAdmissionBusyError",
                "owner transaction is active; no admission was attempted",
            );
        }
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let output = operation(&transaction)?;
        transaction.commit()?;
        Ok(output)
    }

    pub fn admit(&self, payload: Value) -> Result<(Record, bool), Error> {
        let request: Request = serde_json::from_value(payload.clone()).map_err(|error| Error {
            kind: "RuntimeRequestError",
            message: error.to_string(),
        })?;
        if request.version != VERSION {
            return refuse(
                "RuntimeVersionError",
                format!("unsupported job request version: {}", request.version),
            );
        }
        if !identifier(&request.request_id) || !request.options.is_object() {
            return refuse(
                "RuntimeRequestError",
                "request_id must be an opaque identifier and options must be an object",
            );
        }
        if request.role != self.role || request.generation != self.generation {
            return refuse(
                "RuntimeGenerationError",
                "request role/configuration generation does not match the owner",
            );
        }
        if !self.jobs.contains(&request.job) {
            return refuse(
                "RuntimeJobError",
                format!("job is not configured: {}", request.job),
            );
        }
        self.write(|connection| {
            if let Some(record) = load(connection, &request.request_id)? {
                if record.payload != payload {
                    return refuse(
                        "RuntimeConflictError",
                        "request ID is already persisted with a different payload",
                    );
                }
                return Ok((record, false));
            }
            let count: i64 = connection.query_row(
                "SELECT COUNT(*) FROM meta WHERE key GLOB 'te.runtime.jobs.v1:*'",
                [],
                |row| row.get(0),
            )?;
            // P4C-CAP-1: at capacity, retire the oldest terminal records (completed,
            // refused, failed) so a long-lived owner keeps admitting. Queued, running
            // and uncertain records are never pruned. A pruned request ID loses its
            // duplicate-admission memory; only IDs older than the retained window are
            // affected, and job IDs are per-session so they are not resubmitted.
            if count as u64 >= self.limit as u64
                && prune_terminal(connection, count as u64 + 1 - self.limit as u64)?
                    < count as u64 + 1 - self.limit as u64
            {
                return refuse(
                    "RuntimeCapacityError",
                    "job record capacity reached; existing records are retained",
                );
            }
            let seq = sequence(connection)?;
            let record = Record {
                version: VERSION,
                request_id: request.request_id.clone(),
                instance: self.instance.clone(),
                payload: payload.clone(),
                state: State::Queued,
                observed_seq: seq,
                committed_seq: seq,
                result: None,
                error: None,
            };
            save(connection, &record)?;
            save_times(
                connection,
                &record.request_id,
                &JobTimes {
                    admitted_at: Some(now_utc()),
                    ..JobTimes::default()
                },
            )?;
            Ok((record, true))
        })
    }

    pub fn get(&self, id: &str) -> Result<Option<Record>, Error> {
        if !identifier(id) {
            return refuse(
                "RuntimeRequestError",
                "request_id must be an opaque identifier",
            );
        }
        let owner = self.connection.lock().map_err(|_| Error {
            kind: "RuntimeStoreError",
            message: "owner connection mutex poisoned".into(),
        })?;
        let connection = owner.as_ref().ok_or_else(|| Error {
            kind: "RuntimeStoppedError",
            message: "owner connection is closed".into(),
        })?;
        load(connection, id)
    }

    pub fn times(&self, id: &str) -> Result<JobTimes, Error> {
        if !identifier(id) {
            return refuse(
                "RuntimeRequestError",
                "request_id must be an opaque identifier",
            );
        }
        let owner = self.connection.lock().map_err(|_| Error {
            kind: "RuntimeStoreError",
            message: "owner connection mutex poisoned".into(),
        })?;
        let connection = owner.as_ref().ok_or_else(|| Error {
            kind: "RuntimeStoppedError",
            message: "owner connection is closed".into(),
        })?;
        load_times(connection, id)
    }

    pub fn start(&self, id: &str) -> Result<Record, Error> {
        self.write(|connection| {
            let mut record = load(connection, id)?.ok_or_else(|| Error {
                kind: "RuntimeJobError",
                message: "job admission was not persisted".into(),
            })?;
            if record.state != State::Queued || record.instance != self.instance {
                return refuse(
                    "RuntimeTransitionError",
                    "only this instance's queued admission may start",
                );
            }
            record.state = State::Running;
            record.observed_seq = sequence(connection)?;
            save(connection, &record)?;
            let mut times = load_times(connection, id)?;
            times.started_at = Some(now_utc());
            save_times(connection, id, &times)?;
            Ok(record)
        })
    }

    pub fn finish(
        &self,
        id: &str,
        state: State,
        result: Option<Value>,
        error: Option<Failure>,
    ) -> Result<Record, Error> {
        if matches!(state, State::Queued | State::Running)
            || (state == State::Completed && (result.is_none() || error.is_some()))
            || (state != State::Completed && error.is_none())
        {
            return refuse(
                "RuntimeTransitionError",
                "completion requires an exact result or explicit failure",
            );
        }
        self.write(|connection| {
            let mut record = load(connection, id)?.ok_or_else(|| Error {
                kind: "RuntimeJobError",
                message: "job admission was not persisted".into(),
            })?;
            if record.state != State::Running || record.instance != self.instance {
                return refuse(
                    "RuntimeTransitionError",
                    "only this instance's running job may complete",
                );
            }
            record.state = state;
            record.result = result;
            record.error = error;
            record.committed_seq = sequence(connection)?;
            save(connection, &record)?;
            let mut times = load_times(connection, id)?;
            times.finished_at = Some(now_utc());
            save_times(connection, id, &times)?;
            Ok(record)
        })
    }

    pub fn recover(&self) -> Result<Vec<Record>, Error> {
        self.write(|connection| {
            let records = {
                let mut statement = connection.prepare(
                    "SELECT key,value FROM meta WHERE key GLOB 'te.runtime.jobs.v1:*' ORDER BY key",
                )?;
                let rows = statement.query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)))?;
                rows.collect::<Result<Vec<_>, _>>()?
            };
            let mut recovered = Vec::new();
            for (key, text) in records {
                let mut record = decode(&text, &key)?;
                if matches!(record.state, State::Queued | State::Running) {
                    record.state = State::Uncertain;
                    record.error = Some(Failure { r#type: "RuntimeResumeRequired".into(),
                        message: "admitted job has no durable completion; explicit reconciliation/resume is required".into() });
                    record.committed_seq = sequence(connection)?;
                    save(connection, &record)?;
                    recovered.push(record);
                }
            }

            Ok(recovered)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::Store;
    use serde_json::json;
    use std::{
        fs,
        path::PathBuf,
        sync::atomic::{AtomicUsize, Ordering},
    };

    static SERIAL: AtomicUsize = AtomicUsize::new(0);
    struct World {
        path: PathBuf,
        owner: Store,
    }
    impl World {
        fn new() -> Self {
            let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .parent()
                .unwrap()
                .join(".ci-local");
            let path = root.join(format!(
                "t9-journal-{}-{}",
                std::process::id(),
                SERIAL.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();
            let owner = Store::open(
                &path.join("synthetic.db"),
                &path.join("synthetic.db.lock"),
                &std::process::id().to_string(),
            )
            .unwrap_or_else(|_| panic!("synthetic writer open failed"));
            Self { path, owner }
        }
        fn journal(&self, instance: &str, limit: usize) -> Journal {
            Journal::new(
                self.owner.connection.clone(),
                "eod".into(),
                "generation".into(),
                instance.into(),
                BTreeSet::from(["daily".into()]),
                limit,
            )
            .unwrap()
        }
    }
    impl Drop for World {
        fn drop(&mut self) {
            self.owner.close();
            fs::remove_dir_all(&self.path).expect("remove only this test's synthetic directory");
        }
    }
    fn request(id: &str) -> Value {
        json!({"version":1,"request_id":id,"generation":"generation","role":"eod",
                        "job":"daily","session":"2026-10-02","options":{}})
    }

    #[test]
    fn admission_is_durable_and_duplicate_payload_conflicts_preserve_history() {
        let world = World::new();
        let journal = world.journal("first", 1);
        let (record, inserted) = journal.admit(request("one")).unwrap();
        assert!(inserted);
        assert_eq!(record.state, State::Queued);
        let reader = Store::reader(world.path.join("synthetic.db").to_str().unwrap()).unwrap();
        let text: String = reader
            .connection
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .query_row(
                "SELECT value FROM meta WHERE key=?",
                [format!("{PREFIX}one")],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(decode(&text, &format!("{PREFIX}one")).unwrap(), record);
        assert_eq!(
            journal.admit(request("one")).unwrap(),
            (record.clone(), false)
        );
        let mut changed = request("one");
        changed["options"] = json!({"changed":true});
        assert_eq!(
            journal.admit(changed).unwrap_err().kind,
            "RuntimeConflictError"
        );
        assert_eq!(
            journal.admit(request("two")).unwrap_err().kind,
            "RuntimeCapacityError"
        );
        assert_eq!(journal.get("one").unwrap(), Some(record));
        reader.close();
    }

    #[test]
    fn capacity_prunes_oldest_terminal_records_and_keeps_in_flight_and_uncertain() {
        let world = World::new();
        let journal = world.journal("first", 3);
        let failure = || Failure {
            r#type: "EodRunnerError".into(),
            message: "refused".into(),
        };
        journal.admit(request("a")).unwrap();
        journal.start("a").unwrap();
        journal
            .finish("a", State::Completed, Some(json!({"ok":true})), None)
            .unwrap();
        journal.admit(request("b")).unwrap();
        journal.start("b").unwrap();
        journal
            .finish("b", State::Refused, None, Some(failure()))
            .unwrap();
        journal.admit(request("c")).unwrap();
        let count = |j: &Journal| {
            ["a", "b", "c", "d", "e", "f"]
                .iter()
                .filter(|id| j.get(id).unwrap().is_some())
                .count()
        };
        // At the limit: "a" (oldest terminal) is retired, "d" admitted.
        assert!(journal.admit(request("d")).unwrap().1);
        assert_eq!(journal.get("a").unwrap(), None);
        assert!(journal.get("b").unwrap().is_some());
        assert_eq!(count(&journal), 3);
        // Next: "b" retired.
        assert!(journal.admit(request("e")).unwrap().1);
        assert_eq!(journal.get("b").unwrap(), None);
        assert_eq!(count(&journal), 3);
        // Only queued records left: refuse, nothing deleted.
        assert_eq!(
            journal.admit(request("f")).unwrap_err().kind,
            "RuntimeCapacityError"
        );
        assert_eq!(count(&journal), 3);
        // After restart the in-flight records become uncertain; still never pruned.
        let restarted = world.journal("second", 3);
        assert_eq!(restarted.recover().unwrap().len(), 3);
        assert_eq!(
            restarted.admit(request("f")).unwrap_err().kind,
            "RuntimeCapacityError"
        );
        for id in ["c", "d", "e"] {
            assert_eq!(restarted.get(id).unwrap().unwrap().state, State::Uncertain);
        }
    }

    #[test]
    fn omission_and_explicit_null_are_not_the_same_payload() {
        let world = World::new();
        let journal = world.journal("first", 3);
        journal.admit(request("one")).unwrap();
        let mut changed = request("one");
        changed["through"] = Value::Null;
        assert_eq!(
            journal.admit(changed).unwrap_err().kind,
            "RuntimeConflictError"
        );
    }

    #[test]
    fn control_admission_never_joins_or_commits_the_jobs_active_transaction() {
        let world = World::new();
        let journal = world.journal("first", 3);
        world.owner.execute("BEGIN IMMEDIATE", &[]).unwrap();
        world
            .owner
            .execute(
                "INSERT INTO meta(key,value) VALUES('business','uncommitted')",
                &[],
            )
            .unwrap();
        assert_eq!(
            journal.admit(request("one")).unwrap_err().kind,
            "RuntimeAdmissionBusyError"
        );
        assert!(!world
            .owner
            .connection
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .is_autocommit());
        world.owner.execute("ROLLBACK", &[]).unwrap();
        assert!(journal.get("one").unwrap().is_none());
        assert!(journal.admit(request("one")).unwrap().1);
    }

    #[test]
    fn completion_and_restart_never_redrive_or_fabricate_success() {
        let world = World::new();
        let journal = world.journal("first", 6);
        for id in ["queued", "running", "done", "refused"] {
            journal.admit(request(id)).unwrap();
        }
        journal.start("running").unwrap();
        journal.start("done").unwrap();
        assert_eq!(
            journal
                .finish("queued", State::Completed, Some(json!({})), None)
                .unwrap_err()
                .kind,
            "RuntimeTransitionError"
        );
        journal
            .finish(
                "done",
                State::Completed,
                Some(json!({"cash":"-0.00","bars":390})),
                None,
            )
            .unwrap();
        journal.start("refused").unwrap();
        let failure = Failure {
            r#type: "EodRunnerError".into(),
            message: "exact refusal".into(),
        };
        journal
            .finish("refused", State::Refused, None, Some(failure.clone()))
            .unwrap();
        let restarted = world.journal("second", 6);
        let recovered = restarted.recover().unwrap();
        assert_eq!(recovered.len(), 2);
        assert!(recovered
            .iter()
            .all(|record| record.state == State::Uncertain));
        assert_eq!(
            restarted.get("done").unwrap().unwrap().result,
            Some(json!({"cash":"-0.00","bars":390}))
        );
        assert_eq!(
            restarted.get("refused").unwrap().unwrap().error,
            Some(failure)
        );
        assert_eq!(
            restarted.start("running").unwrap_err().kind,
            "RuntimeTransitionError"
        );
        assert!(!restarted.admit(request("running")).unwrap().1);
        assert!(restarted.recover().unwrap().is_empty());
    }

    #[test]
    fn unsupported_versions_make_recovery_atomic() {
        let world = World::new();
        let journal = world.journal("first", 3);
        let queued = journal.admit(request("a-valid")).unwrap().0;
        let mut invalid = serde_json::to_value(queued).unwrap();
        invalid["request_id"] = json!("z-invalid");
        invalid["version"] = json!(2);
        world
            .owner
            .execute(
                "INSERT INTO meta(key,value) VALUES(?,?)",
                &[
                    rusqlite::types::Value::Text(format!("{PREFIX}z-invalid")),
                    rusqlite::types::Value::Text(invalid.to_string()),
                ],
            )
            .unwrap();
        assert_eq!(
            world.journal("second", 3).recover().unwrap_err().kind,
            "RuntimeVersionError"
        );
        assert_eq!(
            journal.get("a-valid").unwrap().unwrap().state,
            State::Queued
        );
    }

    #[test]
    fn request_version_generation_role_and_allowlist_refuse_without_admission() {
        let world = World::new();
        let journal = world.journal("first", 3);
        for (field, value, kind) in [
            ("version", json!(2), "RuntimeVersionError"),
            ("generation", json!("stale"), "RuntimeGenerationError"),
            ("role", json!("mirror"), "RuntimeGenerationError"),
            ("job", json!("unconfigured"), "RuntimeJobError"),
            ("options", json!("not an object"), "RuntimeRequestError"),
            ("request_id", json!("../path"), "RuntimeRequestError"),
        ] {
            let mut payload = request("one");
            payload[field] = value;
            assert_eq!(journal.admit(payload).unwrap_err().kind, kind);
        }
        assert!(journal.get("one").unwrap().is_none());
        assert!(journal.admit(request("one")).unwrap().1);
    }

    #[test]
    fn admit_start_finish_stamp_times_in_order() {
        let world = World::new();
        let journal = world.journal("first", 3);
        journal.admit(request("one")).unwrap();
        let admitted = journal.times("one").unwrap();
        assert!(admitted.admitted_at.is_some());
        assert!(admitted.started_at.is_none());
        assert!(admitted.finished_at.is_none());

        journal.start("one").unwrap();
        let started = journal.times("one").unwrap();
        assert!(started.started_at.is_some());
        assert!(started.finished_at.is_none());

        journal
            .finish("one", State::Completed, Some(json!({"ok": true})), None)
            .unwrap();
        let times = journal.times("one").unwrap();
        let admitted_at = times.admitted_at.as_deref().unwrap();
        let started_at = times.started_at.as_deref().unwrap();
        let finished_at = times.finished_at.as_deref().unwrap();
        // RFC 3339 UTC, millisecond precision, monotonically non-decreasing.
        for stamp in [admitted_at, started_at, finished_at] {
            assert_eq!(stamp.len(), "2026-11-24T21:45:03.123Z".len());
            assert!(stamp.ends_with('Z'));
            chrono::DateTime::parse_from_rfc3339(stamp).unwrap();
        }
        assert!(admitted_at <= started_at);
        assert!(started_at <= finished_at);
        assert!(times.duration_ms().is_some());
    }

    #[test]
    fn duplicate_admit_keeps_admitted_at() {
        let world = World::new();
        let journal = world.journal("first", 3);
        journal.admit(request("one")).unwrap();
        // Overwrite the stored time with a sentinel, then admit the same request
        // again: the duplicate must keep the original time untouched.
        world
            .owner
            .execute(
                "UPDATE meta SET value=? WHERE key=?",
                &[
                    rusqlite::types::Value::Text(
                        r#"{"admitted_at":"2000-01-01T00:00:00.000Z"}"#.into(),
                    ),
                    rusqlite::types::Value::Text("te.runtime.jobtimes.v1:one".into()),
                ],
            )
            .unwrap();
        let (record, inserted) = journal.admit(request("one")).unwrap();
        assert!(!inserted);
        assert_eq!(record.state, State::Queued);
        assert_eq!(
            journal.times("one").unwrap().admitted_at.as_deref(),
            Some("2000-01-01T00:00:00.000Z")
        );
    }

    #[test]
    fn pruning_a_terminal_record_removes_its_times_key() {
        let world = World::new();
        let journal = world.journal("first", 1);
        journal.admit(request("a")).unwrap();
        journal.start("a").unwrap();
        journal
            .finish("a", State::Completed, Some(json!({})), None)
            .unwrap();
        // "a" is terminal and at capacity; admitting "b" prunes "a".
        assert!(journal.admit(request("b")).unwrap().1);
        assert!(journal.get("a").unwrap().is_none());
        let count: i64 = world
            .owner
            .connection
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .query_row(
                "SELECT COUNT(*) FROM meta WHERE key=?",
                ["te.runtime.jobtimes.v1:a"],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
        assert!(journal.times("a").unwrap().admitted_at.is_none());
    }

    #[test]
    fn missing_times_key_reads_as_all_none() {
        let world = World::new();
        let journal = world.journal("first", 3);
        journal.admit(request("one")).unwrap();
        // Simulate a record written before this change: no times key.
        world
            .owner
            .execute(
                "DELETE FROM meta WHERE key=?",
                &[rusqlite::types::Value::Text(
                    "te.runtime.jobtimes.v1:one".into(),
                )],
            )
            .unwrap();
        let times = journal.times("one").unwrap();
        assert!(times.admitted_at.is_none());
        assert!(times.started_at.is_none());
        assert!(times.finished_at.is_none());
        assert_eq!(times.duration_ms(), None);
        // The record itself is still present and readable.
        assert!(journal.get("one").unwrap().is_some());
    }

    #[test]
    fn new_records_keep_the_existing_record_shape_and_decode() {
        let world = World::new();
        let journal = world.journal("first", 3);
        let (record, inserted) = journal.admit(request("one")).unwrap();
        assert!(inserted);
        let text: String = world
            .owner
            .connection
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .query_row(
                "SELECT value FROM meta WHERE key=?",
                [format!("{PREFIX}one")],
                |row| row.get(0),
            )
            .unwrap();
        // The record JSON keeps its pre-change shape: the existing
        // deny_unknown_fields decoder accepts it, and no times field is present.
        assert_eq!(decode(&text, &format!("{PREFIX}one")).unwrap(), record);
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert!(parsed.get("admitted_at").is_none());
        assert!(parsed.get("started_at").is_none());
        assert!(parsed.get("finished_at").is_none());
        assert!(parsed.get("duration_ms").is_none());
    }
}
