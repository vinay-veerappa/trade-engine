use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use std::path::{Path, PathBuf};
use te_host::lock::{LockError, SingleInstanceGuard};

fn io_error(py: Python<'_>, error: std::io::Error, path: &Path, mkdir: bool) -> PyResult<PyErr> {
    let name = path.into_pyobject(py)?;
    let raw = error.raw_os_error().unwrap_or(5);
    #[cfg(windows)]
    {
        let builtins = py.import("builtins")?;
        if mkdir {
            let message: String = py.import("ctypes")?.call_method1("FormatError", (raw,))?.extract()?;
            let exc = builtins.getattr("OSError")?.call1((0, message.trim().trim_end_matches('.'), name, raw))?;
            return Ok(PyErr::from_value(exc));
        }
        // Python open uses CRT errno, while mkdir exposes WinError. Convert the
        // native Windows code through Python's own errno mapping.
        let exc = builtins.getattr("OSError")?.call1((0, "", name.clone(), raw))?;
        let errno: i32 = exc.getattr("errno")?.extract()?;
        let message: String = py.import("os")?.call_method1("strerror", (errno,))?.extract()?;
        Ok(PyOSError::new_err((errno, message, name.unbind())))
    }
    #[cfg(not(windows))]
    {
        let _ = mkdir;
        let message: String = py.import("os")?.call_method1("strerror", (raw,))?.extract()?;
        Ok(PyOSError::new_err((raw, message, name.unbind())))
    }
}

#[pyclass]
struct LedgerLock {
    guard: Option<SingleInstanceGuard>,
}

#[pymethods]
impl LedgerLock {
    #[new]
    fn new() -> Self {
        Self { guard: None }
    }

    fn acquire(&mut self, py: Python<'_>, path: &Bound<'_, PyAny>, pid: &str) -> PyResult<bool> {
        if self.guard.is_some() {
            return Ok(true);
        }
        #[cfg(windows)]
        let path = {
            use std::os::windows::ffi::OsStringExt;
            // PyO3 0.23's PathBuf extractor panics on non-BMP paths. Preserve
            // Windows UTF-16, including unpaired surrogates, without that extractor.
            let bytes: Vec<u8> = path.call_method1("encode", ("utf-16-le", "surrogatepass"))?.extract()?;
            let wide: Vec<u16> = bytes.chunks_exact(2)
                .map(|pair| u16::from_le_bytes([pair[0], pair[1]])).collect();
            PathBuf::from(std::ffi::OsString::from_wide(&wide))
        };
        #[cfg(not(windows))]
        let path: PathBuf = path.extract()?;
        match SingleInstanceGuard::acquire(&path, pid) {
            Ok(guard) => {
                self.guard = Some(guard);
                Ok(true)
            }
            Err(LockError::Contended) => Ok(false),
            Err(LockError::NullPath { mkdir }) => Err(PyValueError::new_err(if cfg!(windows) && mkdir {
                "mkdir: embedded null character in path"
            } else if cfg!(windows) {
                "embedded null character"
            } else {
                "embedded null byte"
            })),
            Err(LockError::Io { error, path, mkdir }) => Err(io_error(py, error, &path, mkdir)?),
        }
    }

    fn release(&mut self) {
        if let Some(guard) = self.guard.take() {
            guard.release();
        }
    }

    #[getter]
    fn held(&self) -> bool {
        self.guard.is_some()
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<LedgerLock>()
}
