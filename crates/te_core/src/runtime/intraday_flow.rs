//! Session sequencing over injected observations and effects; never reads a clock.
pub trait Host {
    type Error;
    type Value;
    fn validate(&mut self) -> Result<(), Self::Error>;
    fn init_state(&mut self) -> Result<(), Self::Error>;
    fn settled(&mut self) -> Result<bool, Self::Error>;
    fn admit(&mut self) -> Result<(), Self::Error>;
    fn beat(&mut self, note: &str, alert: bool, exited: bool) -> Result<(), Self::Error>;
    fn connect_restore(&mut self) -> Result<(), Self::Error>;
    fn boundaries(&mut self) -> Result<(), Self::Error>;
    fn start(&mut self) -> Result<(), Self::Error>;
    fn can_advance(&mut self) -> Result<bool, Self::Error>;
    fn advance_open(&mut self) -> Result<(), Self::Error>;
    fn before_open(&mut self) -> Result<bool, Self::Error>;
    fn wait_beat(&mut self) -> Result<(), Self::Error>;
    fn wait_sleep(&mut self) -> Result<(), Self::Error>;
    fn wait(&mut self) -> Result<(), Self::Error>;
    fn before_close(&mut self) -> Result<bool, Self::Error>;
    fn stopped(&mut self) -> Result<bool, Self::Error>;
    fn result(&mut self, settled: bool, stopped: bool) -> Result<Self::Value, Self::Error>;
    fn now(&mut self) -> Result<Self::Value, Self::Error>;
    fn fetch(&mut self, now: &Self::Value) -> Result<Self::Value, Self::Error>;
    fn fresh(&mut self, snapshot: Self::Value, now: &Self::Value) -> Result<Self::Value, Self::Error>;
    fn stale(&self, error: &Self::Error) -> Result<bool, Self::Error>;
    fn exception(&self, error: &Self::Error) -> bool;
    fn stale_refusal(&mut self, error: Self::Error) -> Result<(), Self::Error>;
    fn clear_refusal(&mut self) -> Result<(), Self::Error>;
    fn deadlines(&mut self, now: &Self::Value) -> Result<(bool, bool), Self::Error>;
    fn cancel(&mut self, code: &str) -> Result<(), Self::Error>;
    fn flatten(&mut self, code: &str) -> Result<(), Self::Error>;
    fn gate(&mut self, allow: bool) -> Result<(), Self::Error>;
    fn manage(&mut self, snapshot: &Self::Value) -> Result<(), Self::Error>;
    fn record(&mut self, snapshot: &Self::Value) -> Result<(), Self::Error>;
    fn pause(&mut self) -> Result<(), Self::Error>;
    fn tick(&mut self) -> Result<(), Self::Error>;
    fn close(&mut self) -> Result<String, Self::Error>;
    fn emergency(&mut self, error: Self::Error) -> Result<Self::Value, Self::Error>;
}

pub fn wait<H: Host>(host: &mut H) -> Result<(), H::Error> {
    if host.can_advance()? {
        host.advance_open()?;
        return Ok(());
    }
    while host.before_open()? {
        host.wait_beat()?;
        host.wait_sleep()?;
    }
    Ok(())
}

pub fn tick<H: Host>(host: &mut H) -> Result<(), H::Error> {
    let requested = host.now()?;
    let pulled = (|| {
        let snapshot = host.fetch(&requested)?;
        let observed = host.now()?;
        let view = host.fresh(snapshot, &observed)?;
        Ok((view, observed))
    })();
    let (view, now) = match pulled {
        Ok(pair) => pair,
        Err(error) => {
            if host.stale(&error)? { return host.stale_refusal(error); }
            return Err(error);
        }
    };
    host.clear_refusal()?;
    let (flat_due, entries_open) = host.deadlines(&now)?;
    if !entries_open {
        host.cancel(if flat_due { "flat-sweep" } else { "entry-end" })?;
    }
    if flat_due { host.flatten("flat-sweep")?; }
    host.gate(entries_open)?;
    host.manage(&view)?;
    host.record(&view)?;
    host.beat(if entries_open { "ok" } else { "ok; entries closed" }, false, false)?;
    Ok(())
}

pub fn run<H: Host>(host: &mut H) -> Result<H::Value, H::Error> {
    host.validate()?;
    host.init_state()?;
    if host.settled()? { return host.result(true, false); }
    host.admit()?;
    host.beat("starting", false, false)?;
    host.connect_restore()?;
    host.boundaries()?;
    host.start()?;
    host.wait()?;
    let walked = (|| {
        while host.before_close()? {
            if host.stopped()? { return host.result(false, true).map(SomeNote::Stopped); }
            host.tick()?;
            host.pause()?;
        }
        let note = host.close()?;
        Ok::<_, H::Error>(SomeNote::Closed(note))
    })();
    // Keep admission/pre-open errors outside the emergency boundary.
    match walked {
        Ok(SomeNote::Stopped(value)) => Ok(value),
        Ok(SomeNote::Closed(note)) => {
            host.beat(&format!("closed; {note}"), false, true)?;
            host.result(false, false)
        }
        Err(error) if host.exception(&error) => host.emergency(error),
        Err(error) => Err(error),
    }
}

enum SomeNote<V> { Stopped(V), Closed(String) }

#[cfg(test)]
mod tests {
    use super::*;

    struct Probe { calls: Vec<&'static str>, stale: bool }
    impl Probe {
        fn log(&mut self, name: &'static str) -> Result<(), &'static str> {
            self.calls.push(name);
            Ok(())
        }
    }
    impl Host for Probe {
        type Error = &'static str;
        type Value = usize;
        fn validate(&mut self) -> Result<(), Self::Error> { self.log("validate") }
        fn init_state(&mut self) -> Result<(), Self::Error> { self.log("state") }
        fn settled(&mut self) -> Result<bool, Self::Error> { Ok(false) }
        fn admit(&mut self) -> Result<(), Self::Error> { self.log("admit") }
        fn beat(&mut self, _: &str, _: bool, _: bool) -> Result<(), Self::Error> { self.log("beat") }
        fn connect_restore(&mut self) -> Result<(), Self::Error> { self.log("restore") }
        fn boundaries(&mut self) -> Result<(), Self::Error> { self.log("boundaries") }
        fn start(&mut self) -> Result<(), Self::Error> { self.log("start") }
        fn can_advance(&mut self) -> Result<bool, Self::Error> { Ok(true) }
        fn advance_open(&mut self) -> Result<(), Self::Error> { self.log("advance") }
        fn before_open(&mut self) -> Result<bool, Self::Error> { Ok(false) }
        fn wait_beat(&mut self) -> Result<(), Self::Error> { self.log("wait-beat") }
        fn wait_sleep(&mut self) -> Result<(), Self::Error> { self.log("wait-sleep") }
        fn wait(&mut self) -> Result<(), Self::Error> { wait(self) }
        fn before_close(&mut self) -> Result<bool, Self::Error> { Ok(false) }
        fn stopped(&mut self) -> Result<bool, Self::Error> { Ok(false) }
        fn result(&mut self, _: bool, _: bool) -> Result<usize, Self::Error> { Ok(0) }
        fn now(&mut self) -> Result<usize, Self::Error> { self.log("now")?; Ok(self.calls.len()) }
        fn fetch(&mut self, _: &usize) -> Result<usize, Self::Error> { self.log("fetch")?; Ok(1) }
        fn fresh(&mut self, _: usize, _: &usize) -> Result<usize, Self::Error> {
            self.log("fresh")?;
            if self.stale { Err("stale") } else { Ok(1) }
        }
        fn stale(&self, error: &&'static str) -> Result<bool, Self::Error> { Ok(*error == "stale") }
        fn exception(&self, _: &&'static str) -> bool { true }
        fn stale_refusal(&mut self, _: Self::Error) -> Result<(), Self::Error> { self.log("refuse") }
        fn clear_refusal(&mut self) -> Result<(), Self::Error> { self.log("clear") }
        fn deadlines(&mut self, _: &usize) -> Result<(bool, bool), Self::Error> { self.log("deadlines")?; Ok((true, false)) }
        fn cancel(&mut self, _: &str) -> Result<(), Self::Error> { self.log("cancel") }
        fn flatten(&mut self, _: &str) -> Result<(), Self::Error> { self.log("flatten") }
        fn gate(&mut self, _: bool) -> Result<(), Self::Error> { self.log("gate") }
        fn manage(&mut self, _: &usize) -> Result<(), Self::Error> { self.log("manage") }
        fn record(&mut self, _: &usize) -> Result<(), Self::Error> { self.log("record") }
        fn pause(&mut self) -> Result<(), Self::Error> { self.log("pause") }
        fn tick(&mut self) -> Result<(), Self::Error> { tick(self) }
        fn close(&mut self) -> Result<String, Self::Error> { self.log("close")?; Ok("done".into()) }
        fn emergency(&mut self, error: Self::Error) -> Result<usize, Self::Error> { Err(error) }
    }
    #[test]
    fn rereads_clock_and_flattens_before_manage_and_reconcile() {
        let mut host = Probe { calls: vec![], stale: false };
        tick(&mut host).unwrap();
        assert_eq!(host.calls, ["now", "fetch", "now", "fresh", "clear", "deadlines",
            "cancel", "flatten", "gate", "manage", "record", "beat"]);
    }
    #[test]
    fn stale_tick_never_manages_or_records() {
        let mut host = Probe { calls: vec![], stale: true };
        tick(&mut host).unwrap();
        assert_eq!(host.calls, ["now", "fetch", "now", "fresh", "refuse"]);
    }
}
