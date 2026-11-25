//! Owner clocks; core decisions continue to receive time as an argument.
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub fn system_utc_microseconds() -> i128 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(value) => (value.as_nanos() / 1_000) as i128,
        Err(error) => -((error.duration().as_nanos().div_ceil(1_000)) as i128),
    }
}

/// Wall-clock UTC as RFC 3339 with milliseconds (`...Z`): owner-side bookkeeping stamps only.
pub fn wall_utc_rfc3339_millis() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

/// A monotonic deadline for I/O timeouts (never a decision input).
#[derive(Clone, Copy, Debug)]
pub struct Deadline(Instant);

impl Deadline {
    pub fn after(timeout: Duration) -> Self {
        Self(Instant::now() + timeout)
    }

    /// Time left, zero once passed.
    pub fn remaining(&self) -> Duration {
        self.0.saturating_duration_since(Instant::now())
    }
}

pub struct ReplayClock<T> {
    current: T,
}

impl<T> ReplayClock<T> {
    pub fn new(current: T) -> Self {
        Self { current }
    }

    pub fn current(&self) -> &T {
        &self.current
    }

    pub fn reset(&mut self, current: T) {
        self.current = current;
    }

    pub fn advance_to<E>(
        &mut self,
        target: T,
        compare: impl FnOnce(&T, &T) -> Result<bool, E>,
        refusal: impl FnOnce(&T, &T) -> E,
    ) -> Result<(), E> {
        if compare(&target, &self.current)? {
            return Err(refusal(&target, &self.current));
        }
        self.current = target;
        Ok(())
    }

    pub fn advance_by<E>(&mut self, add: impl FnOnce(&T) -> Result<T, E>) -> Result<(), E> {
        let target = add(&self.current)?;
        self.current = target;
        Ok(())
    }
}

pub fn wall_sleep<E>(positive: bool, sleep: impl FnOnce() -> Result<(), E>) -> Result<(), E> {
    if positive {
        sleep()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deadline_counts_down_and_wall_stamp_is_utc_millis() {
        let d = Deadline::after(Duration::from_secs(60));
        assert!(d.remaining() <= Duration::from_secs(60));
        assert!(d.remaining() > Duration::from_secs(50));
        assert_eq!(Deadline::after(Duration::ZERO).remaining(), Duration::ZERO);
        let s = wall_utc_rfc3339_millis();
        assert!(s.ends_with('Z') && s.len() == 24, "{s}");
    }

    #[test]
    fn replay_is_monotonic_and_failure_preserves_time() {
        let mut clock = ReplayClock::new(10);
        clock
            .advance_to(10, |a, b| Ok(a < b), |_, _| "backwards")
            .unwrap();
        clock
            .advance_to(12, |a, b| Ok(a < b), |_, _| "backwards")
            .unwrap();
        assert_eq!(
            clock.advance_to(11, |a, b| Ok(a < b), |_, _| "backwards"),
            Err("backwards")
        );
        assert_eq!(clock.advance_by(|_| Err("overflow")), Err("overflow"));
        assert_eq!(*clock.current(), 12);
        clock.advance_by(|a| Ok::<_, &str>(*a + 5)).unwrap();
        assert_eq!(*clock.current(), 17);
    }

    #[test]
    fn injected_wall_sleep_is_zero_noop_and_propagates_failure() {
        assert_eq!(wall_sleep(false, || Err("called")), Ok(()));
        assert_eq!(wall_sleep(true, || Err("called")), Err("called"));
    }
}
