//! `ChainStoreBase`: the gzip JSON chain-snapshot store, moved out of Python
//! (W7-CORE-C3). The directory layout, file names and JSON text are byte-for-byte what
//! `trade_engine.market_data.chains.ChainSnapshotStore` produced; the gzip compression,
//! read-compare and file writes happen with the GIL released, so tv's parallel options
//! passes can compress and read concurrently.

use std::io::{Read, Write};
use std::path::{Path, PathBuf};

use pyo3::exceptions::{PyFileNotFoundError, PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::GILOnceCell;
use pyo3::types::{PyDict, PyTuple};

const SUFFIX: &str = ".json.gz";
const STAMP: &str = "%Y%m%dT%H%M%S%fZ";

// The Python carriers, imported once and cached (http.rs shows the pattern).
static CHAIN_SNAPSHOT_CLS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
static STALE_DATA_ERROR_CLS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
static PATHLIB_PATH_CLS: GILOnceCell<Py<PyAny>> = GILOnceCell::new();
static DATETIME_MOD: GILOnceCell<Py<PyAny>> = GILOnceCell::new();

fn chain_snapshot_cls(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    CHAIN_SNAPSHOT_CLS.get_or_try_init(py, || {
        Ok(py
            .import("trade_engine.market_data.chains")?
            .getattr("ChainSnapshot")?
            .unbind())
    })
}

fn stale_data_error_cls(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    STALE_DATA_ERROR_CLS.get_or_try_init(py, || {
        Ok(py
            .import("trade_engine.interfaces.market_data")?
            .getattr("StaleDataError")?
            .unbind())
    })
}

fn pathlib_path_cls(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    PATHLIB_PATH_CLS.get_or_try_init(py, || Ok(py.import("pathlib")?.getattr("Path")?.unbind()))
}

fn datetime_module(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    DATETIME_MOD.get_or_try_init(py, || Ok(py.import("datetime")?.into_any().unbind()))
}

fn datetime_class(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    Ok(datetime_module(py)?.bind(py).getattr("datetime")?)
}

fn utc_tz(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    Ok(datetime_module(py)?
        .bind(py)
        .getattr("timezone")?
        .getattr("utc")?)
}

fn io_error(e: std::io::Error) -> PyErr {
    if e.kind() == std::io::ErrorKind::NotFound {
        PyFileNotFoundError::new_err(e.to_string())
    } else {
        PyOSError::new_err(e.to_string())
    }
}

/// `_aware`: a timezone-aware datetime converted to UTC, or the exact I7 refusal.
fn aware_utc<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    what: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let tzinfo = value.getattr("tzinfo")?;
    if tzinfo.is_none() || tzinfo.call_method1("utcoffset", (value,))?.is_none() {
        return Err(PyValueError::new_err(format!(
            "{what} must be timezone-aware UTC (I7): {}",
            value.repr()?.to_string_lossy()
        )));
    }
    value.call_method1("astimezone", (utc_tz(py)?,))
}

/// `_name`: `as_of` (UTC) formatted `%Y%m%dT%H%M%S%fZ` plus the `.json.gz` suffix.
fn name_of(as_of: &Bound<'_, PyAny>) -> PyResult<String> {
    let stamped: String = as_of.call_method1("strftime", (STAMP,))?.extract()?;
    Ok(format!("{stamped}{SUFFIX}"))
}

fn gzip_compress(bytes: &[u8]) -> Result<Vec<u8>, std::io::Error> {
    use flate2::{write::GzEncoder, Compression, GzBuilder};
    // mtime 0 keeps the bytes a function of the snapshot alone (as Python did).
    let mut enc: GzEncoder<Vec<u8>> = GzBuilder::new().mtime(0).write(Vec::new(), Compression::new(6));
    enc.write_all(bytes)?;
    enc.finish()
}

fn gzip_decompress(bytes: &[u8]) -> Result<String, std::io::Error> {
    use flate2::read::GzDecoder;
    let mut dec = GzDecoder::new(bytes);
    let mut text = String::new();
    dec.read_to_string(&mut text)?;
    Ok(text)
}

#[pyclass(subclass, module = "trade_engine_rs")]
pub struct ChainStoreBase {
    root: PathBuf,
}

#[pymethods]
impl ChainStoreBase {
    #[new]
    fn new(root: PathBuf) -> Self {
        ChainStoreBase { root }
    }

    fn put(&self, py: Python<'_>, snapshot: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let underlying: String = snapshot.getattr("underlying")?.extract()?;
        // `_name` converted to UTC before formatting; keep that for any carrier.
        let as_of = snapshot.getattr("as_of")?.call_method1("astimezone", (utc_tz(py)?,))?;
        let folder = self.dir_of(&underlying);
        let name = name_of(&as_of)?;
        let path = folder.join(&name);
        let text: String = snapshot.call_method0("to_json")?.extract()?;

        if path.exists() {
            let existing = self.read_text(py, &path)?;
            if existing == text {
                return Ok(self.pathlib_path(py, &path)?);
            }
            return Err(PyValueError::new_err(format!(
                "A different {underlying} snapshot is already stored at {name}"
            )));
        }

        // `path.with_suffix(".tmp")`, as the old Python wrote it.
        let tmp = folder.join(format!("{}.tmp", &name[..name.len() - SUFFIX.len()]));
        let bytes = text.into_bytes();
        let gz = py.allow_threads(move || gzip_compress(&bytes))?;
        let path_for_write = path.clone();
        py.allow_threads(move || -> Result<(), std::io::Error> {
            std::fs::create_dir_all(&folder)?;
            std::fs::write(&tmp, &gz)?;
            std::fs::rename(&tmp, &path_for_write)?;
            Ok(())
        })
        .map_err(io_error)?;
        Ok(self.pathlib_path(py, &path)?)
    }

    fn stamps(&self, py: Python<'_>, underlying: &str) -> PyResult<Py<PyTuple>> {
        let folder = self.dir_of(underlying);
        let names = py
            .allow_threads(move || -> Result<Vec<String>, std::io::Error> {
                if !folder.is_dir() {
                    return Ok(Vec::new());
                }
                let mut found = Vec::new();
                for entry in std::fs::read_dir(&folder)? {
                    let entry = entry?;
                    let name = entry.file_name().to_string_lossy().into_owned();
                    if name.ends_with(SUFFIX) {
                        found.push(name);
                    }
                }
                Ok(found)
            })
            .map_err(io_error)?;
        let dt_cls = datetime_class(py)?;
        let utc = utc_tz(py)?;
        let mut stamps = Vec::new();
        for name in names {
            if let Some(dt) = parse_stamp(py, &dt_cls, &utc, &name)? {
                stamps.push(dt);
            }
        }
        // Sorted by datetime after parsing, as the Python did (strptime accepts a few
        // non-padded names, so name order is not always time order).
        let sorted = py.import("builtins")?.getattr("sorted")?.call1((stamps,))?;
        Ok(PyTuple::new(py, sorted.try_iter()?.collect::<PyResult<Vec<_>>>()?)?.unbind())
    }

    fn load(
        &self,
        py: Python<'_>,
        underlying: &str,
        as_of: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        let as_of = aware_utc(py, as_of, "as_of")?;
        let folder = self.dir_of(underlying);
        let name = name_of(&as_of)?;
        let path = folder.join(&name);
        let text = self.read_text(py, &path)?;
        let cls = chain_snapshot_cls(py)?.bind(py);
        Ok(cls.call_method1("from_json", (text,))?.unbind())
    }

    fn latest(
        slf: PyRef<'_, Self>,
        underlying: &str,
        now: &Bound<'_, PyAny>,
        max_age_seconds: f64,
    ) -> PyResult<Py<PyAny>> {
        let py = slf.py();
        let utc_now = aware_utc(py, now, "now")?;
        let slf_any = slf.into_pyobject(py)?.into_any();
        // Called through Python, so a subclass override of stamps/load still runs.
        let stamps = slf_any.call_method1("stamps", (underlying,))?;
        let mut latest: Option<Bound<'_, PyAny>> = None;
        for stamp in stamps.try_iter()? {
            let stamp = stamp?;
            if stamp.le(&utc_now)? {
                latest = Some(stamp);
            }
        }
        let Some(latest_stamp) = latest else {
            let iso: String = utc_now.call_method0("isoformat")?.extract()?;
            let msg = format!(
                "No {} chain snapshot at or before {} (I5)",
                underlying.to_uppercase(),
                iso
            );
            let exc = stale_data_error_cls(py)?.bind(py).call1((msg,))?;
            return Err(PyErr::from_value(exc));
        };
        let snapshot = slf_any.call_method1("load", (underlying, &latest_stamp))?;
        Ok(snapshot
            .call_method1("require_fresh", (&utc_now, max_age_seconds))?
            .unbind())
    }
}

impl ChainStoreBase {
    /// `_dir`: `<root>/<UNDERLYING>`.
    fn dir_of(&self, underlying: &str) -> PathBuf {
        self.root.join(underlying.trim().to_uppercase())
    }

    /// `_read`: read the file and gunzip it, with the GIL released.
    fn read_text(&self, py: Python<'_>, path: &Path) -> PyResult<String> {
        let path = path.to_path_buf();
        py.allow_threads(move || -> Result<String, std::io::Error> {
            let bytes = std::fs::read(&path)?;
            gzip_decompress(&bytes)
        })
        .map_err(io_error)
    }

    /// `put`'s return value: the stored `pathlib.Path`.
    fn pathlib_path(&self, py: Python<'_>, path: &Path) -> PyResult<Py<PyAny>> {
        let cls = pathlib_path_cls(py)?;
        let text = path.to_string_lossy().into_owned();
        Ok(cls.bind(py).call1((text,))?.unbind())
    }
}

/// One `stamps` file name to a timezone-aware UTC `datetime`; `None` skips foreign files.
fn parse_stamp<'py>(
    py: Python<'py>,
    dt_cls: &Bound<'py, PyAny>,
    utc: &Bound<'py, PyAny>,
    name: &str,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let prefix = &name[..name.len() - SUFFIX.len()];
    let naive = match dt_cls.call_method1("strptime", (prefix, STAMP)) {
        Ok(dt) => dt,
        Err(_) => return Ok(None),
    };
    let kwargs = PyDict::new(py);
    kwargs.set_item("tzinfo", utc)?;
    Ok(Some(naive.call_method("replace", (), Some(&kwargs))?))
}
