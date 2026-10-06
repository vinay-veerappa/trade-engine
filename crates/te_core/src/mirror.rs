//! The mirror follower's pacing, cutoff and collect loop (P4c T12).
//!
//! The venue I/O and every P5 business decision (express, pricing,
//! follow_cycle's effect plan, collect) stay Python, behind the callbacks
//! this loop drives. Rust owns what the plan's T12 row names: the
//! send-window cutoff, the interval pacing, the unreadable/deferred
//! budget, the exact waiting/refusal/halt behavior, once-mode, the
//! collect wait, and the writer lifecycle (one mirror writer with a
//! concurrent read-only sim reader - the host opens/closes both, once,
//! in order; nothing here opens a ledger).

use serde_json::Value;

/// One follow report, exactly the fields the loop's behavior reads.
#[derive(Debug, Clone)]
pub struct FollowReport {
    pub halted: bool,
    pub refused: Option<String>,
    pub deferred: Option<String>,
    pub queued: usize,
    pub fills: usize,
    pub closes: usize,
    pub refused_count: usize,
    pub waiting: Vec<String>,
    pub line: String,
}

impl FollowReport {
    fn from_cycle(value: &Value) -> Option<Self> {
        let object = value.as_object()?;
        let strings = |key: &str| {
            object
                .get(key)
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .map(|row| match row {
                            Value::Array(pair) => pair.first().and_then(Value::as_str).unwrap_or("").to_owned(),
                            _ => row.as_str().unwrap_or("").to_owned(),
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default()
        };
        Some(Self {
            halted: object.get("halted").and_then(Value::as_bool).unwrap_or(false),
            refused: None,
            deferred: object
                .get("deferred")
                .and_then(Value::as_str)
                .map(str::to_owned),
            queued: object.get("queued").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
            fills: object.get("fills").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
            closes: object.get("closes").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
            refused_count: object.get("refused").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
            waiting: strings("waiting"),
            line: object.get("line").and_then(Value::as_str).map(str::to_owned).unwrap_or_default(),
        })
    }

    fn refused(reason: String) -> Self {
        Self {
            halted: false,
            refused: Some(reason),
            deferred: None,
            queued: 0,
            fills: 0,
            closes: 0,
            refused_count: 0,
            waiting: Vec::new(),
            line: String::new(),
        }
    }

    fn collect(line: String, halted: bool, deferred: Option<String>) -> Self {
        Self {
            halted,
            refused: None,
            deferred,
            queued: 0,
            fills: 0,
            closes: 0,
            refused_count: 0,
            waiting: Vec::new(),
            line,
        }
    }
}

/// What one probe of the venue answered.
#[derive(Debug, Clone)]
pub enum Probe {
    /// The venue reads; send to it.
    Readable,
    /// The venue cannot be read now: the reason.
    Unreadable(String),
}

/// The host effects the loop drives: clock, venue probe, one cycle, the
/// collect, and the log. The host owns the ledgers' lifetimes; this loop
/// never opens one.
pub trait FollowHost {
    fn now_local_seconds(&mut self) -> f64;
    fn sleep(&mut self, seconds: f64);
    fn probe(&mut self) -> Probe;
    /// One follow cycle. `Err` is a refusal from the venue side (broker or
    /// session): the loop fails exactly as the Python loop did.
    fn cycle(&mut self) -> Result<FollowReport, String>;
    /// The after-close collect.
    fn collect(&mut self) -> Result<FollowReport, String>;
    fn log(&mut self, line: &str);
}

/// The loop's exact configuration.
pub struct FollowPlan {
    pub send_open: f64,
    pub send_close: f64,
    pub collect_at: f64,
    pub interval_seconds: f64,
    pub once: bool,
    pub max_unreadable: u32,
}

/// One follow run's outcome.
#[derive(Debug, PartialEq)]
pub enum FollowOutcome {
    /// Done; true when nothing halted or refused the venue outright.
    Done(bool),
}

/// The send window and collect cutoffs as seconds past local midnight.
pub fn _hhmm(hour: u32, minute: u32) -> f64 {
    (hour * 3600 + minute * 60) as f64
}

/// The follower loop: the pacing, cutoff, budgets and classification the
/// T12 row names. Every branch mirrors the Python loop's exact behavior
/// (scan_engine/options/mirror_follow.py::run_follow), whose bytes are
/// frozen in the T12 oracle.
pub fn follow_loop(host: &mut impl FollowHost, plan: &FollowPlan) -> FollowOutcome {
    let mut unreadable_cycles: u32 = 0;
    let mut waiting_before: Vec<String> = Vec::new();
    let mut ok = true;
    let mut cycled = false;
    'session: loop {
        let now = host.now_local_seconds();
        if now >= plan.send_close {
            break;
        }
        if now >= plan.send_open {
            match host.probe() {
                Probe::Unreadable(why) => {
                    unreadable_cycles += 1;
                    if plan.once || unreadable_cycles > plan.max_unreadable {
                        host.log(&format!(
                            "giving up: venue unreadable for {unreadable_cycles} cycle(s) ({why})"
                        ));
                        ok = false;
                        break;
                    }
                    if unreadable_cycles == 1 {
                        host.log(&format!(
                            "venue unreadable ({why}); sending nothing until it reads"
                        ));
                    }
                }
                Probe::Readable => {
                    let report = match host.cycle() {
                        Ok(report) => report,
                        Err(reason) => {
                            host.log(&format!("REFUSED  {reason}"));
                            ok = false;
                            break 'session;
                        }
                    };
                    cycled = true;
                    if let Some(reason) = &report.refused {
                        host.log(&format!("REFUSED  {reason}"));
                        ok = false;
                        break;
                    }
                    if report.halted {
                        host.log(&format!("HALTED  {}", report.line));
                        ok = false;
                        break;
                    }
                    if report.deferred.is_some() {
                        // The venue read fine a moment ago and could not be
                        // asked inside the cycle: nothing was sent or
                        // recorded, the entry waits. Same budget as an
                        // unreadable venue.
                        unreadable_cycles += 1;
                        if plan.once || unreadable_cycles > plan.max_unreadable {
                            host.log(&format!(
                                "giving up: venue deferred {unreadable_cycles} cycle(s) ({})",
                                report.deferred.as_deref().unwrap_or("")
                            ));
                            ok = false;
                            break;
                        }
                        if unreadable_cycles == 1 {
                            host.log(&report.line);
                        }
                    } else {
                        if unreadable_cycles > 0 {
                            host.log(&format!(
                                "resumed after {unreadable_cycles} unreadable cycle(s)"
                            ));
                            unreadable_cycles = 0;
                        }
                        let waiting_changed = report.waiting != waiting_before;
                        if report.queued > 0
                            || report.refused_count > 0
                            || report.fills > 0
                            || report.closes > 0
                            || (!report.waiting.is_empty() && waiting_changed)
                        {
                            host.log(&report.line);
                        }
                        waiting_before = report.waiting.clone();
                    }
                }
            }
        }
        if plan.once {
            return FollowOutcome::Done(ok);
        }
        host.sleep(plan.interval_seconds);
    }
    if plan.once {
        // Started after the send close: one cycle is nothing to send, and
        // no collect.
        return FollowOutcome::Done(ok);
    }
    if !cycled && plan.once {
        return FollowOutcome::Done(ok);
    }
    // The collect wait: hold until the collect cutoff, then collect once.
    while ok && host.now_local_seconds() < plan.collect_at {
        host.sleep(plan.interval_seconds);
    }
    match host.collect() {
        Ok(report) => {
            host.log(&report.line);
            FollowOutcome::Done(ok && !report.halted && report.deferred.is_none())
        }
        Err(reason) => {
            host.log(&format!("REFUSED  {reason}"));
            FollowOutcome::Done(false)
        }
    }
}

/// Serialize a cycle report for the loop's Python seam: the plan's exact
/// line, computed where the counts live.
pub fn cycle_line(report: &Value) -> String {
    report
        .get("line")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned()
}

impl FollowReport {
    /// A cycle report as the loop's Python seam sends it: JSON text with
    /// halted/deferred/queued/fills/closes/refused/waiting/line (the counts
    /// may be numbers or arrays), or {"refused": "..."} for a refusal the
    /// cycle raised.
    pub fn from_wire(value: &Value) -> Option<Self> {
        if let Some(reason) = value.get("refused").and_then(Value::as_str) {
            return Some(Self::refused(reason.to_owned()));
        }
        let object = value.as_object()?;
        let count = |key: &str| {
            object
                .get(key)
                .map(|field| match field {
                    Value::Array(rows) => rows.len(),
                    Value::Number(number) => number.as_u64().unwrap_or(0) as usize,
                    _ => 0,
                })
                .unwrap_or(0)
        };
        let strings = |key: &str| {
            object
                .get(key)
                .and_then(Value::as_array)
                .map(|rows| {
                    rows.iter()
                        .map(|row| match row {
                            Value::Array(pair) => {
                                pair.first().and_then(Value::as_str).unwrap_or("").to_owned()
                            }
                            _ => row.as_str().unwrap_or("").to_owned(),
                        })
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default()
        };
        Some(Self {
            halted: object.get("halted").and_then(Value::as_bool).unwrap_or(false),
            refused: None,
            deferred: object
                .get("deferred")
                .and_then(Value::as_str)
                .map(str::to_owned),
            queued: count("queued"),
            fills: count("fills"),
            closes: count("closes"),
            refused_count: count("refused"),
            waiting: strings("waiting"),
            line: object.get("line").and_then(Value::as_str).map(str::to_owned).unwrap_or_default(),
        })
    }

    /// A collect report as the seam sends it: line/halted/deferred.
    pub fn from_collect_wire(value: &Value) -> Option<Self> {
        let line = value.get("line").and_then(Value::as_str)?.to_owned();
        let halted = value.get("halted").and_then(Value::as_bool).unwrap_or(false);
        let deferred = value
            .get("deferred")
            .and_then(Value::as_str)
            .map(str::to_owned);
        Some(Self::collect(line, halted, deferred))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A scriptable host: the loop's behavior tests drive it with a list
    /// of (now, probe, cycle) steps.
    struct Scripted {
        clock: f64,
        sleeps: Vec<f64>,
        logs: Vec<String>,
        probes: Vec<Probe>,
        cycles: Vec<Result<FollowReport, String>>,
        cycle_index: usize,
        collect: Result<FollowReport, String>,
        collected: bool,
    }

    impl Scripted {
        fn new(probes: Vec<Probe>, cycles: Vec<Result<FollowReport, String>>) -> Self {
            Self {
                clock: 9.0 * 3600.0,
                sleeps: Vec::new(),
                logs: Vec::new(),
                probes,
                cycles,
                cycle_index: 0,
                collect: Ok(FollowReport::collect("collected".into(), false, None)),
                collected: false,
            }
        }
    }

    impl FollowHost for Scripted {
        fn now_local_seconds(&mut self) -> f64 {
            self.clock
        }
        fn sleep(&mut self, seconds: f64) {
            self.sleeps.push(seconds);
            self.clock += seconds;
        }
        fn probe(&mut self) -> Probe {
            self.probes.pop().unwrap_or(Probe::Readable)
        }
        fn cycle(&mut self) -> Result<FollowReport, String> {
            let index = self.cycle_index;
            self.cycle_index += 1;
            self.cycles.get(index).cloned().unwrap_or(Ok(FollowReport {
                halted: false,
                refused: None,
                deferred: None,
                queued: 0,
                fills: 0,
                closes: 0,
                refused_count: 0,
                waiting: Vec::new(),
                line: String::new(),
            }))
        }
        fn collect(&mut self) -> Result<FollowReport, String> {
            self.collected = true;
            self.collect.clone()
        }
        fn log(&mut self, line: &str) {
            self.logs.push(line.to_owned());
        }
    }

    fn plan() -> FollowPlan {
        FollowPlan {
            send_open: _hhmm(9, 45),
            send_close: _hhmm(15, 55),
            collect_at: _hhmm(16, 5),
            interval_seconds: 30.0,
            once: false,
            max_unreadable: 10,
        }
    }

    #[test]
    fn a_clean_cycle_logs_its_line_and_collects_after_the_close() {
        let mut host = Scripted::new(
            vec![],
            vec![Ok(FollowReport {
                halted: false,
                refused: None,
                deferred: None,
                queued: 1,
                fills: 0,
                closes: 0,
                refused_count: 0,
                waiting: Vec::new(),
                line: "queued 1".into(),
            })],
        );
        host.clock = _hhmm(9, 46);
        let outcome = follow_loop(&mut host, &plan());
        assert_eq!(outcome, FollowOutcome::Done(true));
        assert!(host.collected, "the collect must run after the close");
        assert!(host.logs.iter().any(|line| line == "queued 1"));
    }

    #[test]
    fn a_halt_fails_the_run_and_stops_sending() {
        let halted = || {
            Ok(FollowReport {
                halted: true,
                refused: None,
                deferred: None,
                queued: 0,
                fills: 0,
                closes: 0,
                refused_count: 0,
                waiting: Vec::new(),
                line: "halted".into(),
            })
        };
        let mut host = Scripted::new(vec![], vec![halted()]);
        host.clock = _hhmm(9, 46);
        let outcome = follow_loop(&mut host, &plan());
        assert_eq!(outcome, FollowOutcome::Done(false));
        assert!(host.logs.iter().any(|line| line.starts_with("HALTED")));
        // The frozen loop collects immediately when not ok (the wait is the
        // only thing ok gates): a halted venue is collected, not waited on.
        assert!(host.collected, "the frozen loop collects after a halt");
        let waits: Vec<f64> = host.sleeps.clone();
        assert!(
            waits.iter().all(|s| *s == plan().interval_seconds),
            "no collect WAIT happens when not ok: {waits:?}"
        );
    }

    #[test]
    fn once_returns_after_one_readable_cycle_without_collecting() {
        let mut host = Scripted::new(
            vec![],
            vec![Ok(FollowReport {
                halted: false,
                refused: None,
                deferred: None,
                queued: 1,
                fills: 0,
                closes: 0,
                refused_count: 0,
                waiting: Vec::new(),
                line: "queued 1".into(),
            })],
        );
        host.clock = _hhmm(9, 46);
        let once = FollowPlan {
            once: true,
            ..plan()
        };
        let outcome = follow_loop(&mut host, &once);
        assert_eq!(outcome, FollowOutcome::Done(true));
        assert!(!host.collected, "once never collects");
    }

    #[test]
    fn the_unreadable_budget_gives_up_after_max_cycles() {
        let why = "read_positions: Timeout: gone".to_owned();
        let probes = vec![Probe::Unreadable(why.clone()); 12];
        let mut host = Scripted::new(probes, vec![]);
        host.clock = _hhmm(9, 46);
        let outcome = follow_loop(&mut host, &plan());
        assert_eq!(outcome, FollowOutcome::Done(false));
        assert!(
            host.logs
                .iter()
                .any(|line| line == &format!("giving up: venue unreadable for 11 cycle(s) ({why})")),
            "the give-up line must be exact: {:?}",
            host.logs
        );
        assert!(host.logs.iter().any(|line| line.starts_with("venue unreadable (")));
        // The frozen loop collects immediately when not ok (the wait is the
        // only thing ok gates).
        assert!(host.collected, "the frozen loop collects after a give-up");
    }

    #[test]
    fn a_deferred_cycle_spends_the_same_budget_and_resumes() {
        // Deferred, deferred, then a clean cycle: the budget resets on resume.
        let deferred = || Ok(FollowReport {
            deferred: Some("venue busy".into()),
            halted: false,
            refused: None,
            queued: 0,
            fills: 0,
            closes: 0,
            refused_count: 0,
            waiting: Vec::new(),
            line: "deferred".into(),
        });
        let clean = || {
            Ok(FollowReport {
                halted: false,
                refused: None,
                deferred: None,
                queued: 1,
                fills: 0,
                closes: 0,
                refused_count: 0,
                waiting: Vec::new(),
                line: "queued 1".into(),
            })
        };
        let mut host = Scripted::new(
            vec![],
            vec![deferred(), deferred(), clean(), clean()],
        );
        host.clock = _hhmm(9, 46);
        let outcome = follow_loop(&mut host, &plan());
        assert_eq!(outcome, FollowOutcome::Done(true));
        assert!(
            host.logs.iter().any(|line| line.starts_with("resumed after 2 unreadable")),
            "the resume line must appear: {:?}",
            host.logs
        );
    }

    #[test]
    fn once_when_started_after_the_send_close_is_nothing_and_no_collect() {
        let mut host = Scripted::new(vec![], vec![]);
        host.clock = _hhmm(16, 0);
        let once = FollowPlan {
            once: true,
            ..plan()
        };
        let outcome = follow_loop(&mut host, &once);
        assert_eq!(outcome, FollowOutcome::Done(true));
        assert!(!host.collected);
        assert!(host.sleeps.is_empty(), "nothing to wait for");
    }

    #[test]
    fn a_refusal_from_the_cycle_fails_the_run_immediately() {
        let mut host = Scripted::new(vec![], vec![Err("broker gone".into())]);
        host.clock = _hhmm(9, 46);
        let outcome = follow_loop(&mut host, &plan());
        assert_eq!(outcome, FollowOutcome::Done(false));
        assert!(host.logs.iter().any(|line| line == "REFUSED  broker gone"));
        // The frozen loop collects immediately when not ok (the wait is the
        // only thing ok gates).
        assert!(host.collected, "the frozen loop collects after a refusal");
    }

    #[test]
    fn waiting_orders_are_said_once_not_every_cycle() {
        let waiting = vec!["order-1".to_owned()];
        let with_waiting = || {
            Ok(FollowReport {
                halted: false,
                refused: None,
                deferred: None,
                queued: 0,
                fills: 0,
                closes: 0,
                refused_count: 0,
                waiting: waiting.clone(),
                line: "waiting 1".into(),
            })
        };
        let mut host = Scripted::new(vec![], vec![with_waiting(), with_waiting(), with_waiting()]);
        host.clock = _hhmm(9, 46);
        let outcome = follow_loop(&mut host, &plan());
        assert_eq!(outcome, FollowOutcome::Done(true));
        let said: usize = host.logs.iter().filter(|l| l.as_str() == "waiting 1").count();
        assert_eq!(said, 1, "a hold is said once: {:?}", host.logs);
    }
}