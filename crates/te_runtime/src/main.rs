//! Offline packaging/composition proofs plus the opt-in runtime owner entry.
mod config;
mod control;
mod jobs;
#[cfg(windows)]
mod plugins;
#[cfg(windows)]
mod python;
mod routes;
#[cfg(windows)]
mod stdin_watch;

use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use te_host::actor::Actor;

fn run() -> Result<Value, Value> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() == 3 && args[0] == "--proof" && args[1] == "--config" {
        let path = PathBuf::from(&args[2]);
        let config = config::Config::read(&path)?;
        #[cfg(windows)]
        {
            python::proof(&config)
        }
        #[cfg(not(windows))]
        {
            let _ = config;
            Err(config::error(
                "embedded packaging is only verified on Windows",
            ))
        }
    } else if args.len() == 3 && args[0] == "serve" && args[1] == "--config" {
        let path = PathBuf::from(&args[2]);
        serve(&path)
    } else if !args.is_empty() && args[0] == "serve" {
        Err(config::error(
            "usage: te serve --config <absolute JSON path>",
        ))
    } else {
        Err(config::error(
            "usage: te --proof --config <absolute JSON path>",
        ))
    }
}

/// The runtime owner composes store, journal and actor, recovers foreign
/// incomplete records, then serves authorized control requests until stop.
/// No trading job, venue or live ledger is touched here.
fn serve(path: &PathBuf) -> Result<Value, Value> {
    let config = config::Config::read(path)?;
    if config.mode != "runtime-owner" {
        return Err(config::error(format!(
            "serve requires runtime-owner mode, got: {}",
            config.mode
        )));
    }
    let runtime_config = config.runtime.as_ref().expect("validated runtime config");
    let stop_timeout = runtime_config.stop_timeout_seconds;
    let port = runtime_config.port;
    let entry = match (&runtime_config.entry_module, &runtime_config.entry) {
        (Some(module), Some(member)) => Some((module.clone(), member.clone())),
        _ => None,
    };
    let mut owner = jobs::Owner::open(&config)?;
    let identity = owner.identity;
    // The dispatcher seam: a configured entry point executes real jobs in
    // this owner process over the attached ledger; without one, the synthetic
    // executor acknowledges each configured job (offline proof mode).
    let dispatcher = match &entry {
        Some((module, member)) => control::Dispatcher::start_jobs(
            &identity.jobs,
            control::factory_job_result(module.clone(), member.clone()),
        ),
        None => control::Dispatcher::start_jobs(&identity.jobs, control::synthetic_job_result),
    };
    let journal = Arc::new(owner.journal);
    let ledger = owner
        .config_ledger
        .to_string_lossy()
        .to_string();
    let actor = Actor::start(
        owner.store,
        journal,
        dispatcher,
        Duration::from_secs_f64(stop_timeout),
    );
    let control = Arc::new(routes::RuntimeControl::new(identity, Arc::new(Mutex::new(Some(actor)))));
    let facts = control.identity_facts();
    let mut report = json!({
        "role": facts.role.clone(),
        "generation": facts.generation.clone(),
        "instance": facts.instance.clone(),
        "jobs": facts.jobs.clone(),
        "recovered": true,
    });
    match port {
        // Without a port, serve proves the composition once and exits. The
        // one-shot check belongs to the synthetic proof executor: a
        // configured real entry point only executes admitted session jobs.
        None if entry.is_none() => {
            let payload = json!({
                "version": jobs::RUNTIME_VERSION,
                "request_id": "serve-composition-check",
                "generation": facts.generation,
                "role": facts.role,
                "job": facts.jobs[0],
                "options": {},
            });
            let (record, inserted) = control
                .submit_direct(&payload)
                .map_err(|failure| jobs::refusal(failure.kind, failure.message))?;
            let finished = control
                .wait_terminal("serve-composition-check")
                .map_err(|failure| jobs::refusal(failure.kind, failure.message))?;
            report["submitted"] = json!({"record": record, "inserted": inserted});
            report["finished"] = finished;
            control
                .stop()
                .map_err(|message| jobs::refusal("RuntimeStopError", message))?;
        }
        // A configured real entry without a port is a composition error: the
        // owner would start and immediately exit without serving jobs.
        None => {
            return Err(config::error(
                "runtime-owner with an entry point requires a port to serve jobs",
            ));
        }
        // With a port, bind loopback HTTP with the control attached and serve
        // until stdin closes: stop admission, drain the active job, close the
        // store and release the guard last.
        Some(port) => {
            #[cfg(windows)]
            {
                // The embedded interpreter initializes; the stdlib adapter
                // supplies header/URL conversions, never ownership. The owner's
                // one held store is attached (shared connection/guard Arcs) so
                // factory execution composes over it without a second writer.
                crate::python::initialize_for_serve(&config)
                    .map_err(|e| jobs::refusal("RuntimePythonError", e))?;
                pyo3::Python::with_gil(|py| crate::plugins::verify_engine_source(py, &config))
                    .map_err(|e: pyo3::PyErr| jobs::refusal("RuntimePythonError", e.to_string()))?;
                let owner_parts = owner
                    .store_parts
                    .take()
                    .expect("owner store parts are attached exactly once");
                pyo3::Python::with_gil(|py| {
                    trade_engine_rs::store::attach_owner_parts(
                        py,
                        owner_parts.0,
                        owner_parts.1,
                        ledger.clone(),
                    )
                })
                .map_err(|e: pyo3::PyErr| jobs::refusal("RuntimePythonError", e.to_string()))?;
                let text = pyo3::Python::with_gil(|py| {
                    trade_engine_rs::http::stdlib_text(py)
                        .map_err(|e: pyo3::PyErr| e.to_string())
                })
                .map_err(|e: String| jobs::refusal("RuntimePythonError", e))?;
                let identity_text = format!(
                    "te-runtime/{} Python/{}",
                    env!("CARGO_PKG_VERSION"),
                    crate::python::python_version(),
                );
                let mut server = te_host::http::Server::start(
                    ledger,
                    "127.0.0.1",
                    port,
                    15.0,
                    identity_text,
                    text,
                )
                .map_err(|e| jobs::refusal("RuntimeBindError", e.to_string()))?;
                server.attach_control(control.clone());
                println!(
                    "{}",
                    serde_json::to_string(&json!({
                        "serving": true,
                        "port": server.port,
                        "generation": facts.generation,
                        "role": facts.role,
                    }))
                    .expect("report serialization cannot fail")
                );
                // Serve until stdin closes; a remote owner stops explicitly.
                stdin_watch::wait_for_close();
                server.stop().map_err(|e| {
                    jobs::refusal("RuntimeStopError", e)
                })?;
                control
                    .stop()
                    .map_err(|message| jobs::refusal("RuntimeStopError", message))?;
                report["served"] = json!({"port": server.port});
            }
            #[cfg(not(windows))]
            {
                let _ = (port, &ledger, &control);
                return Err(config::error(
                    "runtime-owner HTTP serving is only verified on Windows",
                ));
            }
        }
    }
    Ok(report)
}

fn main() {
    match run() {
        Ok(report) => println!("{}", json!(report)),
        Err(error) => {
            eprintln!("{}", error);
            std::process::exit(2);
        }
    }
}