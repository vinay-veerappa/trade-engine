//! CPython datetime/numeric primitives preserve the oracle's exact conversions.
use pyo3::{
    basic::CompareOp,
    exceptions::{PyTypeError, PyValueError},
    prelude::*,
    types::{PyBool, PyDict, PyFloat, PyInt},
};
use std::sync::Mutex;
use te_host::clock::{self, ReplayClock};

fn utc<'py>(value: &Bound<'py, PyAny>, name: &str) -> PyResult<Bound<'py, PyAny>> {
    let tz = value.getattr("tzinfo")?;
    if tz.is_none() || tz.call_method1("utcoffset", (value,))?.is_none() {
        return Err(PyValueError::new_err(format!(
            "{name} must be a timezone-aware UTC datetime (I7)"
        )));
    }
    let zone = value
        .py()
        .import("datetime")?
        .getattr("timezone")?
        .getattr("utc")?;
    value.call_method1("astimezone", (zone,))
}

fn numeric(value: &Bound<'_, PyAny>) -> bool {
    value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>()
}

fn finite_nonnegative(value: &Bound<'_, PyAny>) -> PyResult<bool> {
    Ok(value
        .py()
        .import("math")?
        .call_method1("isfinite", (value,))?
        .is_truthy()?
        && !value.rich_compare(0, CompareOp::Lt)?.is_truthy()?)
}

fn sleep_valid(value: &Bound<'_, PyAny>, replay: bool) -> PyResult<()> {
    if !numeric(value)
        || (replay && value.is_instance_of::<PyBool>())
        || !finite_nonnegative(value)?
    {
        return Err(PyValueError::new_err(format!(
            "sleep seconds cannot be negative or non-finite, got: {}",
            value.repr()?
        )));
    }
    Ok(())
}

#[pyclass(module = "trade_engine_rs", frozen)]
struct NativeReplayClock {
    clock: Mutex<ReplayClock<Py<PyAny>>>,
}

impl NativeReplayClock {
    fn replace(&self, current: Py<PyAny>) {
        // Even decref/destructors may reenter Python; drop the old state unlocked.
        let old = {
            let mut clock = self.clock.lock().expect("clock mutex poisoned");
            std::mem::replace(&mut *clock, ReplayClock::new(current))
        };
        drop(old);
    }
}

#[pymethods]
impl NativeReplayClock {
    #[new]
    fn new(initial_time: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(Self {
            clock: Mutex::new(ReplayClock::new(
                utc(initial_time, "initial_time")?.unbind(),
            )),
        })
    }

    fn now_utc(&self, py: Python<'_>) -> Py<PyAny> {
        self.current(py)
    }

    #[getter]
    fn current(&self, py: Python<'_>) -> Py<PyAny> {
        self.clock
            .lock()
            .expect("clock mutex poisoned")
            .current()
            .clone_ref(py)
    }

    #[setter]
    fn set_current(&self, current: Py<PyAny>) {
        // Existing replay fixtures deliberately reset this private compatibility seam.
        self.replace(current);
    }

    fn advance_to(&self, target: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = target.py();
        let target = utc(target, "target time")?.unbind();
        let mut clock = ReplayClock::new(self.current(py));
        clock.advance_to(
            target,
            |a, b| {
                a.bind(py)
                    .rich_compare(b.bind(py), CompareOp::Lt)?
                    .is_truthy()
            },
            |a, _b| {
                let message: PyResult<String> = (|| {
                    Ok(format!(
                        "Cannot advance clock backwards in time: target {} < current {} (I5)",
                        a.bind(py).call_method0("isoformat")?,
                        self.current(py).bind(py).call_method0("isoformat")?
                    ))
                })();
                match message {
                    Ok(message) => PyValueError::new_err(message),
                    Err(error) => error,
                }
            },
        )?;
        self.replace(clock.current().clone_ref(py));
        Ok(())
    }

    fn advance_by(&self, duration: &Bound<'_, PyAny>) -> PyResult<()> {
        let py = duration.py();
        let timedelta = py.import("datetime")?.getattr("timedelta")?;
        let delta = if numeric(duration) && !duration.is_instance_of::<PyBool>() {
            if !finite_nonnegative(duration)? {
                return Err(PyValueError::new_err(format!(
                    "Cannot advance clock by non-finite or negative duration: {}",
                    duration.repr()?
                )));
            }
            let kwargs = PyDict::new(py);
            kwargs.set_item("seconds", duration)?;
            timedelta.call((), Some(&kwargs))?
        } else if duration.is_instance(&timedelta)? {
            if duration
                .call_method0("total_seconds")?
                .rich_compare(0, CompareOp::Lt)?
                .is_truthy()?
            {
                return Err(PyValueError::new_err(format!(
                    "Cannot advance clock by negative duration: {duration}"
                )));
            }
            duration.clone()
        } else {
            return Err(PyTypeError::new_err(format!(
                "Expected timedelta, float or int, got {}",
                duration.get_type().name()?
            )));
        };
        let mut clock = ReplayClock::new(self.current(py));
        clock.advance_by(|current| {
            // In-place addition preserves datetime subclasses and reflected operators.
            unsafe {
                Bound::<PyAny>::from_owned_ptr_or_err(
                    py,
                    pyo3::ffi::PyNumber_InPlaceAdd(current.as_ptr(), delta.as_ptr()),
                )
                .map(Bound::unbind)
            }
        })?;
        self.replace(clock.current().clone_ref(py));
        Ok(())
    }
}

#[pyfunction]
fn clock_replay_sleep(owner: &Bound<'_, PyAny>, seconds: &Bound<'_, PyAny>) -> PyResult<()> {
    sleep_valid(seconds, true)?;
    // No native mutable borrow crosses an overridable Python callback.
    owner.call_method1("advance_by", (seconds,))?;
    Ok(())
}

#[pyclass(module = "trade_engine_rs")]
struct NativeWallClock {
    now_reader: Option<Py<PyAny>>,
    sleeper: Option<Py<PyAny>>,
    epoch: Py<PyAny>,
    timedelta: Py<PyAny>,
}

#[pymethods]
impl NativeWallClock {
    #[new]
    #[pyo3(signature = (now_reader=None, sleeper=None))]
    fn new(
        py: Python<'_>,
        now_reader: Option<Py<PyAny>>,
        sleeper: Option<Py<PyAny>>,
    ) -> PyResult<Self> {
        let dt = py.import("datetime")?;
        let kwargs = PyDict::new(py);
        kwargs.set_item("tzinfo", dt.getattr("timezone")?.getattr("utc")?)?;
        Ok(Self {
            now_reader,
            sleeper,
            epoch: dt
                .getattr("datetime")?
                .call((1970, 1, 1), Some(&kwargs))?
                .unbind(),
            timedelta: dt.getattr("timedelta")?.unbind(),
        })
    }

    fn now_utc(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        if let Some(read) = &self.now_reader {
            return Ok(read.call0(py)?);
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item("microseconds", clock::system_utc_microseconds())?;
        let delta = self.timedelta.bind(py).call((), Some(&kwargs))?;
        Ok(self
            .epoch
            .bind(py)
            .call_method1("__add__", (delta,))?
            .unbind())
    }

    fn sleep(&self, seconds: &Bound<'_, PyAny>) -> PyResult<()> {
        sleep_valid(seconds, false)?;
        clock::wall_sleep(seconds.rich_compare(0, CompareOp::Gt)?.is_truthy()?, || {
            // CPython's OS primitive preserves signal interruption and conversion errors.
            let sleeper = match &self.sleeper {
                Some(sleeper) => sleeper.bind(seconds.py()).clone(),
                None => seconds.py().import("time")?.getattr("sleep")?,
            };
            sleeper.call1((seconds,))?;
            Ok(())
        })
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<NativeReplayClock>()?;
    m.add_class::<NativeWallClock>()?;
    m.add_function(wrap_pyfunction!(clock_replay_sleep, m)?)?;
    Ok(())
}
