//! Owner clocks; core decisions continue to receive time as an argument.
use std::time::{SystemTime, UNIX_EPOCH};

pub fn system_utc_microseconds() -> i128 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(value) => (value.as_nanos() / 1_000) as i128,
        Err(error) => -((error.duration().as_nanos().div_ceil(1_000)) as i128),
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
