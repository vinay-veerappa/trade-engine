//! Per-ledger serialized job actor. Exactly one running job, durable admission
//! before acknowledgment, no resend, restart uncertainty, guard released last.
use crate::jobs::{Error, Failure, Journal, Record, State};
use crate::store::Store;
use serde_json::Value;
use std::{
    collections::VecDeque,
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Condvar, Mutex,
    },
    thread::{self, JoinHandle},
    time::Duration,
};

/// One job's terminal outcome. The host never touches admission records.
pub enum Outcome {
    Completed(Value),
    Refused(Failure),
    Failed(Failure),
}

impl Outcome {
    fn parts(self) -> (State, Option<Value>, Option<Failure>) {
        match self {
            Outcome::Completed(result) => (State::Completed, Some(result), None),
            Outcome::Refused(error) => (State::Refused, None, Some(error)),
            Outcome::Failed(error) => (State::Failed, None, Some(error)),
        }
    }
}

/// Trusted execution adapter: configured factories only, never submitted code.
pub trait JobHost: Send + 'static {
    fn run(&mut self, record: &Record) -> Outcome;
}

struct Queue {
    ids: Mutex<VecDeque<String>>,
    signal: Condvar,
}

#[derive(Clone, Debug)]
pub struct Status {
    pub running: Option<String>,
    pub queued: usize,
    pub stopping: bool,
}

struct Shared {
    queue: Queue,
    stopping: AtomicBool,
    running: Mutex<Option<String>>,
    errors: Mutex<Vec<String>>,
}

impl Shared {
    fn failure(&self, error: impl Into<String>) {
        self.errors
            .lock()
            .expect("actor errors mutex poisoned")
            .push(error.into());
    }
}

fn execute<H: JobHost>(journal: &Journal, host: &mut H, shared: &Shared, id: &str) {
    let record = match journal.get(id) {
        Ok(Some(record)) => record,
        Ok(None) => {
            shared.failure(format!("admitted job vanished before execution: {id}"));
            return;
        }
        Err(error) => {
            shared.failure(format!("job {id} could not be loaded: {error}"));
            return;
        }
    };
    if let Err(error) = journal.start(id) {
        // The record keeps its durable state; never fabricate a start.
        shared.failure(format!("job {id} could not start: {error}"));
        return;
    }
    let outcome = match catch_unwind(AssertUnwindSafe(|| host.run(&record))) {
        Ok(outcome) => outcome,
        Err(panic) => {
            let message = panic
                .downcast_ref::<&str>()
                .map(|text| (*text).to_owned())
                .or_else(|| panic.downcast_ref::<String>().cloned())
                .unwrap_or_else(|| "job host panicked".into());
            Outcome::Failed(Failure {
                r#type: "RuntimeHostPanic".into(),
                message,
            })
        }
    };
    let (state, result, error) = outcome.parts();
    if let Err(failure) = journal.finish(id, state, result, error) {
        // Running record stays durable; restart must reconcile, never rerun.
        shared.failure(format!("job {id} could not record its outcome: {failure}"));
    }
}

/// One-shot inline execution for a CLI client on its own thread.
/// Recovers foreign unfinished records first; never auto-replays them.
/// The caller owns the store; closing it (guard last) stays with the caller.
pub fn run_once(
    journal: &Journal,
    host: &mut impl JobHost,
    payload: Value,
) -> Result<(Record, bool), Error> {
    journal.recover()?;
    let (record, inserted) = journal.admit(payload)?;
    if !inserted {
        // An exact duplicate never re-executes; report the persisted record.
        return Ok((record, false));
    }
    let shared = Shared {
        queue: Queue {
            ids: Mutex::new(VecDeque::new()),
            signal: Condvar::new(),
        },
        stopping: AtomicBool::new(false),
        running: Mutex::new(None),
        errors: Mutex::new(Vec::new()),
    };
    {
        let mut running = shared.running.lock().expect("running mutex poisoned");
        *running = Some(record.request_id.clone());
    }
    execute(journal, host, &shared, &record.request_id);
    {
        let mut running = shared.running.lock().expect("running mutex poisoned");
        *running = None;
    }
    let finished = journal.get(&record.request_id)?.unwrap_or(record);
    Ok((finished, inserted))
}

/// Dedicated-thread serialized owner. Client disconnects cannot cancel a job:
/// admission is durable before any acknowledgment is returned.
pub struct Actor {
    store: Store,
    journal: Arc<Journal>,
    shared: Arc<Shared>,
    thread: Option<JoinHandle<()>>,
    stop_timeout: Duration,
    done: mpsc::Receiver<bool>,
}

impl Actor {
    pub fn start<H: JobHost>(
        store: Store,
        journal: Arc<Journal>,
        mut host: H,
        stop_timeout: Duration,
    ) -> Self {
        let shared = Arc::new(Shared {
            queue: Queue {
                ids: Mutex::new(VecDeque::new()),
                signal: Condvar::new(),
            },
            stopping: AtomicBool::new(false),
            running: Mutex::new(None),
            errors: Mutex::new(Vec::new()),
        });
        let (done_sender, done) = mpsc::channel();
        let owner = shared.clone();
        let worker = journal.clone();
        let thread = thread::Builder::new()
            .name("te-runtime-actor".into())
            .spawn(move || {
                loop {
                    let id = {
                        let mut ids = owner.queue.ids.lock().expect("queue mutex poisoned");
                        loop {
                            // Stopping is checked before dequeuing: admitted but
                            // unstarted jobs stay durably queued, never executed.
                            if owner.stopping.load(Ordering::SeqCst) {
                                done_sender.send(true).ok();
                                return;
                            }
                            if let Some(id) = ids.pop_front() {
                                break id;
                            }
                            ids = owner
                                .queue
                                .signal
                                .wait(ids)
                                .expect("queue mutex poisoned");
                        }
                    };
                    {
                        let mut running = owner.running.lock().expect("running mutex poisoned");
                        *running = Some(id.clone());
                    }
                    execute(&worker, &mut host, &owner, &id);
                    {
                        let mut running = owner.running.lock().expect("running mutex poisoned");
                        *running = None;
                    }
                }
            })
            .expect("actor thread spawn");
        Self {
            store,
            journal,
            shared,
            thread: Some(thread),
            stop_timeout,
            done,
        }
    }

    pub fn submit(&self, payload: Value) -> Result<(Record, bool), Error> {
        if self.shared.stopping.load(Ordering::SeqCst) {
            return Err(Error {
                kind: "RuntimeStoppingError",
                message: "owner is stopping; no admission was attempted".into(),
            });
        }
        let (record, inserted) = self.journal.admit(payload)?;
        if inserted {
            self.shared
                .queue
                .ids
                .lock()
                .expect("queue mutex poisoned")
                .push_back(record.request_id.clone());
            self.shared.queue.signal.notify_one();
        }
        Ok((record, inserted))
    }

    pub fn get(&self, id: &str) -> Result<Option<Record>, Error> {
        self.journal.get(id)
    }

    pub fn status(&self) -> Status {
        Status {
            running: self
                .shared
                .running
                .lock()
                .expect("running mutex poisoned")
                .clone(),
            queued: self
                .shared
                .queue
                .ids
                .lock()
                .expect("queue mutex poisoned")
                .len(),
            stopping: self.shared.stopping.load(Ordering::SeqCst),
        }
    }

    pub fn errors(&self) -> Vec<String> {
        self.shared
            .errors
            .lock()
            .expect("actor errors mutex poisoned")
            .clone()
    }

    /// Stop admission, signal the loop, let the active job finish within the
    /// configured bound, then close the store and release the guard last.
    /// A hung host is reported as blocked, never as a successful stop.
    pub fn stop(&mut self) -> Result<(), String> {
        self.shared.stopping.store(true, Ordering::SeqCst);
        self.shared.queue.signal.notify_all();
        if let Some(thread) = self.thread.take() {
            match self.done.recv_timeout(self.stop_timeout) {
                Ok(_) => {
                    thread.join().map_err(|_| "actor thread panicked".to_owned())?;
                }
                Err(_) => {
                    // The thread is still inside a job host; do not fabricate
                    // success. Leave the guard to process teardown (a crash).
                    self.shared
                        .failure("actor stop timed out with a job still running");
                    return Err("owner stop blocked: job still running after timeout".into());
                }
            }
        }
        // The connection closes before the guard; Store::close does both in order.
        self.store.close();
        Ok(())
    }
}

impl Drop for Actor {
    fn drop(&mut self) {
        if let Err(error) = self.stop() {
            self.shared.failure(error);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::{
        collections::BTreeSet,
        fs,
        path::PathBuf,
        sync::{
            atomic::{AtomicUsize, Ordering},
            mpsc,
        },
        time::Duration,
    };

    static SERIAL: AtomicUsize = AtomicUsize::new(0);
    struct World {
        path: PathBuf,
        store: Option<Store>,
    }
    impl World {
        fn new() -> Self {
            let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .parent()
                .unwrap()
                .join(".ci-local");
            let path = path.join(format!(
                "t9-actor-{}-{}",
                std::process::id(),
                SERIAL.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir_all(&path).unwrap();
            let store = Some(Store::open(
                &path.join("synthetic.db"),
                &path.join("synthetic.db.lock"),
                &std::process::id().to_string(),
            )
            .unwrap_or_else(|_| panic!("synthetic writer open failed")));
            Self { path, store }
        }
        fn journal(&self, instance: &str, limit: usize) -> Arc<Journal> {
            Arc::new(
                Journal::new(
                    self.store.as_ref().unwrap().connection.clone(),
                    "eod".into(),
                    "generation".into(),
                    instance.into(),
                    BTreeSet::from(["daily".into()]),
                    limit,
                )
                .unwrap(),
            )
        }
        fn request(id: &str) -> Value {
            json!({"version":1,"request_id":id,"generation":"generation","role":"eod",
                            "job":"daily","session":"2026-10-02","options":{}})
        }
        fn actor<H: JobHost>(
            &mut self,
            instance: &str,
            limit: usize,
            host: H,
            timeout: Duration,
        ) -> Actor {
            let journal = self.journal(instance, limit);
            Actor::start(
                self.store.take().expect("store already handed to an actor"),
                journal,
                host,
                timeout,
            )
        }
        fn held(&self) -> bool {
            self.store.as_ref().is_some_and(Store::held)
        }
        fn connection(&self) -> crate::store::SharedConnection {
            self.store.as_ref().expect("store present").connection.clone()
        }
        /// After stop, re-acquiring the sidecar must succeed: the guard was
        /// released last. (While a live owner holds it, acquire is Contended.)
        fn assert_guard_released(&self) {
            assert!(!self.held());
            let lock = self
                .path
                .join("synthetic.db.lock")
                .to_str()
                .unwrap()
                .to_owned();
            let guard = crate::lock::SingleInstanceGuard::acquire(
                std::path::Path::new(&lock),
                "probe",
            )
            .expect("sidecar guard must be acquirable after owner stop");
            guard.release();
        }
    }
    impl Drop for World {
        fn drop(&mut self) {
            if let Some(store) = self.store.take() {
                store.close();
            }
            fs::remove_dir_all(&self.path).expect("remove only this test's synthetic directory");
        }
    }

    /// A host that blocks each run until one release token arrives, counting
    /// starts. Deterministic for any number of submitted jobs.
    struct Gated {
        releases: Arc<(Mutex<mpsc::Receiver<()>>, mpsc::Sender<()>)>,
        started: Arc<AtomicUsize>,
        outcome: Box<dyn FnMut() -> Outcome + Send>,
    }
    impl Gated {
        fn new(
            started: Arc<AtomicUsize>,
            outcome: Box<dyn FnMut() -> Outcome + Send>,
        ) -> (Self, Release) {
            let (sender, receiver) = mpsc::channel();
            let releases = Arc::new((Mutex::new(receiver), sender));
            (
                Self {
                    releases: releases.clone(),
                    started,
                    outcome,
                },
                Release(releases),
            )
        }
    }
    impl JobHost for Gated {
        fn run(&mut self, _record: &Record) -> Outcome {
            self.started.fetch_add(1, Ordering::SeqCst);
            self.releases.0.lock().unwrap().recv().unwrap();
            (self.outcome)()
        }
    }
    /// Send one release token per blocked run, from any thread.
    struct Release(Arc<(Mutex<mpsc::Receiver<()>>, mpsc::Sender<()>)>);
    impl Release {
        fn one(&self) {
            self.0 .1.send(()).unwrap();
        }
    }

    #[test]
    fn serialization_admits_ahead_but_runs_exactly_one_at_a_time() {
        let mut world = World::new();
        let started = Arc::new(AtomicUsize::new(0));
        let (host, release) = Gated::new(
            started.clone(),
            Box::new(|| Outcome::Completed(json!({"bars": 1}))),
        );
        let mut actor = world.actor("first", 8, host, Duration::from_secs(30));
        for id in ["one", "two", "three"] {
            let (record, inserted) = actor.submit(World::request(id)).unwrap();
            assert!(inserted);
            assert_eq!(record.state, State::Queued);
        }
        // All three are admitted; wait until exactly one is executing while
        // the other two remain durably queued.
        loop {
            let status = actor.status();
            if status.running.is_some() && status.queued == 2 {
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        for _ in 0..3 {
            release.one();
        }
        while started.load(Ordering::SeqCst) < 3 {
            thread::sleep(Duration::from_millis(5));
        }
        while actor.status().running.is_some() || actor.status().queued > 0 {
            thread::sleep(Duration::from_millis(5));
        }
        for id in ["one", "two", "three"] {
            assert_eq!(
                actor.get(id).unwrap().unwrap().state,
                State::Completed
            );
        }
        actor.stop().unwrap();
        world.assert_guard_released();
    }

    #[test]
    fn disconnect_never_cancels_or_resends_an_admitted_job() {
        let mut world = World::new();
        let started = Arc::new(AtomicUsize::new(0));
        let (host, release) = Gated::new(
            started.clone(),
            Box::new(|| Outcome::Completed(json!({"cash": "0.00"}))),
        );
        let mut actor = world.actor("first", 4, host, Duration::from_secs(30));
        let (record, _) = actor.submit(World::request("one")).unwrap();
        // The submitting "client" drops every handle; the job still completes.
        drop(record);
        release.one();
        while actor.get("one").unwrap().unwrap().state != State::Completed {
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(started.load(Ordering::SeqCst), 1);
        actor.stop().unwrap();
    }

    #[test]
    fn duplicate_submission_reports_the_persisted_record_and_never_reruns() {
        let mut world = World::new();
        let started = Arc::new(AtomicUsize::new(0));
        let (host, release) = Gated::new(
            started.clone(),
            Box::new(|| Outcome::Completed(json!({"bars": 2}))),
        );
        let mut actor = world.actor("first", 4, host, Duration::from_secs(30));
        let (record, inserted) = actor.submit(World::request("one")).unwrap();
        assert!(inserted);
        // The duplicate returns the same persisted admission; the worker may
        // already have transitioned it to running, so compare identity, and
        // prove the payload-equality rule separately in the journal tests.
        let (duplicate, inserted) = actor.submit(World::request("one")).unwrap();
        assert!(!inserted);
        assert_eq!(duplicate.request_id, record.request_id);
        assert_eq!(duplicate.payload, record.payload);
        release.one();
        while actor.get("one").unwrap().unwrap().state != State::Completed {
            thread::sleep(Duration::from_millis(5));
        }
        // A duplicate after completion reports the persisted record and the
        // owner never re-executes the finished job.
        let (duplicate, inserted) = actor.submit(World::request("one")).unwrap();
        assert!(!inserted);
        assert_eq!(duplicate.state, State::Completed);
        assert_eq!(started.load(Ordering::SeqCst), 1);
        thread::sleep(Duration::from_millis(20));
        assert_eq!(started.load(Ordering::SeqCst), 1);
        drop(actor);
    }

    #[test]
    fn stop_lets_the_active_job_finish_then_releases_the_guard() {
        let mut world = World::new();
        let started = Arc::new(AtomicUsize::new(0));
        let (host, release) = Gated::new(
            started.clone(),
            Box::new(|| Outcome::Completed(json!({"bars": 3}))),
        );
        let mut actor = world.actor("first", 4, host, Duration::from_secs(30));
        actor.submit(World::request("one")).unwrap();
        actor.submit(World::request("two")).unwrap();
        while actor.status().running.is_none() {
            thread::sleep(Duration::from_millis(5));
        }
        // Stop lands while the first job is still active inside the host; a
        // helper releases it so stop() observes a bounded finish.
        let releaser = thread::spawn(move || {
            thread::sleep(Duration::from_millis(50));
            release.one();
        });
        actor.stop().unwrap();
        releaser.join().unwrap();
        // After shutdown the store is closed: every read refuses honestly.
        assert_eq!(
            actor.get("one").unwrap_err().kind,
            "RuntimeStoppedError"
        );
        assert_eq!(
            actor.get("two").unwrap_err().kind,
            "RuntimeStoppedError"
        );
        // The durable records survive on disk; a fresh reader confirms them:
        // one completed, two queued-but-never-started.
        let reader = Store::reader(world.path.join("synthetic.db").to_str().unwrap()).unwrap();
        let record = |id: &str| {
            let connection = reader.connection.lock().unwrap();
            let connection = connection.as_ref().unwrap();
            let text: String = connection
                .query_row(
                    "SELECT value FROM meta WHERE key=?",
                    [format!("{}{}", crate::jobs::PREFIX, id)],
                    |row| row.get(0),
                )
                .unwrap();
            let record: Record = serde_json::from_str(&text).unwrap();
            record
        };
        assert_eq!(record("one").state, State::Completed);
        assert_eq!(record("two").state, State::Queued);
        reader.close();
        assert_eq!(started.load(Ordering::SeqCst), 1);
        world.assert_guard_released();
        let refused = actor.submit(World::request("three")).unwrap_err();
        assert_eq!(refused.kind, "RuntimeStoppingError");
    }

    #[test]
    fn hung_host_makes_stop_report_blocked_and_keeps_uncertainty_durable() {
        let mut world = World::new();
        let started = Arc::new(AtomicUsize::new(0));
        let (host, release) = Gated::new(
            started.clone(),
            Box::new(|| Outcome::Completed(json!({}))),
        );
        let mut actor = world.actor("first", 4, host, Duration::from_millis(150));
        actor.submit(World::request("one")).unwrap();
        while started.load(Ordering::SeqCst) == 0 {
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(
            actor.stop().unwrap_err(),
            "owner stop blocked: job still running after timeout"
        );
        // The record stays Running, never fabricated success; release the host.
        assert_eq!(actor.get("one").unwrap().unwrap().state, State::Running);
        release.one();
        while actor.get("one").unwrap().unwrap().state != State::Completed {
            thread::sleep(Duration::from_millis(5));
        }
        actor.stop().unwrap();
        world.assert_guard_released();
    }

    #[test]
    fn restart_makes_foreign_incomplete_records_uncertain_before_new_work() {
        let mut world = World::new();
        // The durable-record recovery is what this gate exercises; the physical
        // guard handoff between dead instances is T1's proven surface.
        let connection = world.connection();
        let started = Arc::new(AtomicUsize::new(0));
        let (host, release) = Gated::new(
            started.clone(),
            Box::new(|| Outcome::Completed(json!({"bars": 4}))),
        );
        let mut first = Actor::start(
            world.store.take().expect("store already handed to an actor"),
            Arc::new(
                Journal::new(
                    connection.clone(),
                    "eod".into(),
                    "generation".into(),
                    "first".into(),
                    BTreeSet::from(["daily".into()]),
                    4,
                )
                .unwrap(),
            ),
            host,
            Duration::from_millis(150),
        );
        first.submit(World::request("one")).unwrap();
        while started.load(Ordering::SeqCst) == 0 {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(first.stop().is_err());
        // The instance hung with a job running; a new instance must recover it
        // as uncertain, then run its own new job without replaying the old one.
        let journal = Journal::new(
            connection,
            "eod".into(),
            "generation".into(),
            "second".into(),
            BTreeSet::from(["daily".into()]),
            4,
        )
        .unwrap();
        let (fresh, inserted) = run_once(&journal, &mut Once, World::request("fresh")).unwrap();
        assert!(inserted);
        assert_eq!(fresh.state, State::Completed);
        assert_eq!(
            journal.get("one").unwrap().unwrap().state,
            State::Uncertain
        );
        release.one();
        // The hung worker's late finish attempt must not fabricate success:
        // its record already transitioned to uncertain, so finish refuses.
        thread::sleep(Duration::from_millis(50));
        assert_eq!(
            journal.get("one").unwrap().unwrap().state,
            State::Uncertain
        );
    }

    struct Once;
    impl JobHost for Once {
        fn run(&mut self, _record: &Record) -> Outcome {
            Outcome::Completed(json!({"bars": 5}))
        }
    }

    #[test]
    fn panicking_host_records_failure_without_poisoning_the_owner() {
        let mut world = World::new();
        let started = Arc::new(AtomicUsize::new(0));
        let (host, release) = Gated::new(
            started.clone(),
            Box::new(|| panic!("synthetic host failure")),
        );
        let mut actor = world.actor("first", 4, host, Duration::from_secs(30));
        actor.submit(World::request("one")).unwrap();
        release.one();
        while actor.get("one").unwrap().unwrap().state != State::Failed {
            thread::sleep(Duration::from_millis(5));
        }
        let failed = actor.get("one").unwrap().unwrap();
        assert_eq!(
            failed.error.unwrap().r#type,
            "RuntimeHostPanic"
        );
        let (record, inserted) = actor.submit(World::request("two")).unwrap();
        assert!(inserted);
        assert_eq!(record.state, State::Queued);
        release.one();
        // The owner continues serving jobs after a panic; the second job also
        // fails through the same recorded path.
        while actor.get("two").unwrap().unwrap().state != State::Failed {
            thread::sleep(Duration::from_millis(5));
        }
        actor.stop().unwrap();
    }
}