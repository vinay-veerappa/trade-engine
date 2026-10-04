//! Additive packaging proof. No clock, jobs, provider, store or server ownership.
mod config;
#[cfg(windows)]
mod python;

use serde_json::{json, Value};
use std::path::PathBuf;

fn run() -> Result<Value, Value> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() != 3 || args[0] != "--proof" || args[1] != "--config" {
        return Err(config::error(
            "usage: te --proof --config <absolute JSON path>",
        ));
    }
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
