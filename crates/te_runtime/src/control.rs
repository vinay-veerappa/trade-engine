//! Trusted job-family dispatch: allowlisted names map to configured factories.
//! The host never accepts submitted code, imports, SQL or arbitrary writes.
use crate::jobs::Owner;
use pyo3::types::{PyAnyMethods, PyModuleMethods};
use serde_json::{json, Value};
use std::sync::mpsc;
use te_host::actor::Outcome;
use te_host::jobs::{Failure, Record};

/// A job request accepted by the owner. Built only from an admitted record.
pub struct Job {
    pub request_id: String,
    pub job: String,
    pub session: Option<String>,
    pub pass: Option<String>,
    pub through: Option<String>,
    pub options: Value,
}

impl Job {
    fn from_record(record: &Record) -> Result<Self, Failure> {
        let payload = &record.payload;
        let job = payload["job"]
            .as_str()
            .ok_or_else(|| Failure {
                r#type: "RuntimeRequestError".into(),
                message: "admitted payload lacks its job name".into(),
            })?
            .to_owned();
        Ok(Self {
            request_id: record.request_id.clone(),
            job,
            session: payload["session"].as_str().map(str::to_owned),
            pass: payload["pass"].as_str().map(str::to_owned),
            through: payload["through"].as_str().map(str::to_owned),
            options: payload["options"].clone(),
        })
    }
}

/// Commands sent to the dedicated interpreter execution thread.
enum Command {
    Run {
        job: Job,
        reply: mpsc::SyncSender<Result<Value, Failure>>,
    },
    Stop,
}

/// The dispatch host translates one admitted record into exactly one
/// configured factory invocation on the execution thread. It is generic over
/// the executor seam so tests can prove refusal paths without a ledger.
pub struct Dispatcher {
    jobs: Vec<String>,
    sender: Option<mpsc::SyncSender<Command>>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Dispatcher {
    /// Start the execution thread with the allowlisted job names. The executor
    /// receives one job at a time; it must never open a second writer or
    /// touch admission records.
    pub fn start_jobs<E>(jobs: &[String], executor: E) -> Self
    where
        E: FnMut(Job) -> Result<Value, Failure> + Send + 'static,
    {
        let (sender, receiver) = mpsc::sync_channel::<Command>(0);
        let jobs: Vec<String> = jobs.to_vec();
        let thread = std::thread::Builder::new()
            .name("te-runtime-jobs".into())
            .spawn(move || {
                let mut executor = executor;
                while let Ok(command) = receiver.recv() {
                    match command {
                        Command::Run { job, reply } => {
                            let _ = reply.send(executor(job));
                        }
                        Command::Stop => return,
                    }
                }
            })
            .expect("execution thread spawn");
        Self {
            jobs,
            sender: Some(sender),
            thread: Some(thread),
        }
    }
}

impl te_host::actor::JobHost for Dispatcher {
    fn run(&mut self, record: &Record) -> Outcome {
        let job = match Job::from_record(record) {
            Ok(job) => job,
            Err(failure) => return Outcome::Refused(failure),
        };
        if !self.jobs.iter().any(|name| name == &job.job) {
            return Outcome::Refused(Failure {
                r#type: "RuntimeJobError".into(),
                message: format!("job is not configured: {}", job.job),
            });
        }
        let Some(sender) = &self.sender else {
            return Outcome::Failed(Failure {
                r#type: "RuntimeStoppedError".into(),
                message: "execution thread is shut down".into(),
            });
        };
        let (reply, done) = mpsc::sync_channel(1);
        if sender
            .send(Command::Run {
                job,
                reply,
            })
            .is_err()
        {
            return Outcome::Failed(Failure {
                r#type: "RuntimeStoppedError".into(),
                message: "execution thread is shut down".into(),
            });
        }
        match done.recv() {
            Ok(Ok(result)) => Outcome::Completed(result),
            Ok(Err(failure)) => Outcome::Refused(failure),
            Err(_) => Outcome::Failed(Failure {
                r#type: "RuntimeHostPanic".into(),
                message: "execution thread did not answer".into(),
            }),
        }
    }
}

impl Drop for Dispatcher {
    fn drop(&mut self) {
        if let Some(sender) = self.sender.take() {
            let _ = sender.send(Command::Stop);
        }
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// The one configured synthetic job used by packaging/refusal tests.
pub fn synthetic_job_result(job: Job) -> Result<Value, Failure> {
    Ok(json!({
        "job": job.job,
        "options": job.options,
    }))
}

/// The trusted owner-side executor: the owner's CONFIGURED client entry point
/// (plain identifiers, validated at config load) receives the owner's ONE
/// attached ledger handle plus the job's session/options and assembles the
/// runner over it and runs it. The request carries data, never code; the
/// entry names never come from the request. Executes on the dedicated thread
/// with the GIL held only for the synchronous call.
pub fn factory_job_result(entry_module: String, entry: String) -> impl FnMut(Job) -> Result<Value, Failure> {
    move |job: Job| {
        pyo3::Python::with_gil(|py| {
            let outcome = (|| -> pyo3::PyResult<String> {
                // The full admitted job crosses: session and options, exactly
                // as persisted; a pass job's name and through-time ride in the
                // options (the one entry signature stays (ledger, session,
                // options)). The request carries data, never code.
                let mut options = job.options.clone();
                if let Some(map) = options.as_object_mut() {
                    if let Some(pass) = &job.pass {
                        map.insert("pass".into(), json!(pass));
                    }
                    if let Some(through) = &job.through {
                        map.insert("through".into(), json!(through));
                    }
                }
                let payload = json!({
                    "session": job.session,
                    "options": options,
                });
                let options = serde_json::to_string(&payload)
                    .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
                let module = py.import("trade_engine_rs")?;
                let function = module.getattr("owner_job_run")?;
                function.call1((options, &entry_module, &entry))?.extract()
            })();
            match outcome {
                Ok(text) => Ok(serde_json::from_str(&text).unwrap_or(Value::Null)),
                Err(error) => {
                    let message = error
                        .value(py)
                        .str()
                        .map(|s| s.to_string())
                        .unwrap_or_else(|_| error.to_string());
                    Err(Failure {
                        r#type: "RuntimeHostError".into(),
                        message,
                    })
                }
            }
        })
    }
}