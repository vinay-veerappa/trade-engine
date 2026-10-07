//! Generic trusted-plugin composition; no runner invocation and no new writer.
use pyo3::{
    exceptions::{PyRuntimeError, PyTypeError, PyValueError},
    prelude::*,
    types::{PyDict, PyTuple},
};
use std::{
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
};

static FACTORIES: AtomicUsize = AtomicUsize::new(0);

struct FactoryScope;
impl FactoryScope {
    fn enter() -> Self {
        FACTORIES.fetch_add(1, Ordering::SeqCst);
        Self
    }
}
impl Drop for FactoryScope {
    fn drop(&mut self) {
        FACTORIES.fetch_sub(1, Ordering::SeqCst);
    }
}

pub(crate) fn refuse_factory_writer() -> PyResult<()> {
    if FACTORIES.load(Ordering::SeqCst) != 0 {
        return Err(PyRuntimeError::new_err(
            "Plugin factories cannot open an engine writer; use the injected owner view (I4)",
        ));
    }
    Ok(())
}

#[pyclass(module = "trade_engine_rs", frozen)]
struct StoreView {
    owner: Py<PyAny>,
}

#[pymethods]
impl StoreView {
    fn state(&self, py: Python<'_>, account: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        self.owner.call_method1(py, "state", (account,))
    }
    #[pyo3(signature = (*, after=None, account=None))]
    fn events(
        &self,
        py: Python<'_>,
        after: Option<Py<PyAny>>,
        account: Option<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let kwargs = PyDict::new(py);
        kwargs.set_item("after", after)?;
        kwargs.set_item("account", account)?;
        self.owner
            .bind(py)
            .call_method("events", (), Some(&kwargs))
            .map(Bound::unbind)
    }
    fn accounts(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.owner.call_method0(py, "accounts")
    }
    fn event_by_command(
        &self,
        py: Python<'_>,
        command_id: &Bound<'_, PyAny>,
    ) -> PyResult<Py<PyAny>> {
        self.owner
            .call_method1(py, "event_by_command", (command_id,))
    }
    fn get_meta(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        self.owner.call_method1(py, "get_meta", (key,))
    }
    fn count(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.owner.call_method0(py, "count")
    }
    fn next_seq(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.owner.call_method0(py, "next_seq")
    }
}

#[pyclass(module = "trade_engine_rs", frozen)]
struct ClockView {
    owner: Py<PyAny>,
}

#[pymethods]
impl ClockView {
    fn now_utc(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.owner.call_method0(py, "now_utc")
    }
    fn sleep(&self, py: Python<'_>, seconds: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        self.owner.call_method1(py, "sleep", (seconds,))
    }
}

#[pyclass(module = "trade_engine_rs", frozen)]
struct FactoryContext {
    #[pyo3(get)]
    ledger: Py<StoreView>,
    #[pyo3(get)]
    clock: Py<ClockView>,
}

fn identifier(name: &str) -> bool {
    !name.is_empty()
        && name
            .bytes()
            .enumerate()
            .all(|(i, c)| c == b'_' || c.is_ascii_alphabetic() || (i > 0 && c.is_ascii_digit()))
}

fn resolved(path: &Path) -> PyResult<PathBuf> {
    std::fs::canonicalize(path).map_err(|e| {
        PyValueError::new_err(format!(
            "Cannot resolve plugin path {}: {e}",
            path.display()
        ))
    })
}

fn check_origin(module: &Bound<'_, PyAny>, allowed: &[PathBuf]) -> PyResult<()> {
    let origin = module.getattr("__spec__")?.getattr("origin")?;
    let path = origin
        .extract::<PathBuf>()
        .map_err(|_| PyRuntimeError::new_err("plugin source is outside configured plugin_paths"))?;
    if !allowed.contains(&resolved(&path)?) {
        return Err(PyRuntimeError::new_err(
            "plugin source is outside configured plugin_paths",
        ));
    }
    Ok(())
}

fn check_result(result: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = result.py();
    let carrier = py
        .import("trade_engine.runtime.plugins")?
        .getattr("FactoryResult")?;
    if !result.is_instance(&carrier)? {
        return Err(PyTypeError::new_err(
            "plugin factory must return FactoryResult",
        ));
    }
    let mapping = py.import("collections.abc")?.getattr("Mapping")?;
    if !result.getattr("config")?.is_instance(&mapping)? {
        return Err(PyTypeError::new_err(
            "FactoryResult.config must be a mapping",
        ));
    }
    let strategies = result.getattr("strategies")?;
    if !strategies.is_instance_of::<PyTuple>() {
        return Err(PyTypeError::new_err(
            "FactoryResult.strategies must be a tuple",
        ));
    }
    let adapters = result.getattr("adapters")?;
    if !adapters.is_none() && !adapters.is_instance(&mapping)? {
        return Err(PyTypeError::new_err(
            "FactoryResult.adapters must be a mapping or None",
        ));
    }
    let owning = PyTuple::new(
        py,
        [
            py.import("trade_engine.ledger.store")?.getattr("Ledger")?,
            py.import("trade_engine.eod.runner")?.getattr("EodRunner")?,
            py.import("trade_engine.intraday.service")?
                .getattr("IntradayService")?,
            py.import("trade_engine_rs")?.getattr("LedgerStore")?,
        ],
    )?;
    for strategy in strategies.try_iter()? {
        if strategy?.is_instance(&owning)? {
            return Err(PyTypeError::new_err(
                "FactoryResult cannot contain an owning ledger or runner",
            ));
        }
    }
    if !adapters.is_none() {
        for adapter in adapters.call_method0("values")?.try_iter()? {
            if adapter?.is_instance(&owning)? {
                return Err(PyTypeError::new_err(
                    "FactoryResult cannot contain an owning ledger or runner",
                ));
            }
        }
    }
    Ok(())
}

#[pyfunction]
pub fn runtime_load_factory(
    py: Python<'_>,
    module: &str,
    factory: &str,
    plugin_paths: Vec<PathBuf>,
    ledger: &Bound<'_, PyAny>,
    clock: &Bound<'_, PyAny>,
    config: &Bound<'_, PyAny>,
) -> PyResult<Py<PyAny>> {
    if !identifier(module) || module == "trade_engine" || module == "trade_engine_rs" {
        return Err(PyValueError::new_err(
            "plugin_module must be a non-reserved plain identifier",
        ));
    }
    if !identifier(factory) {
        return Err(PyValueError::new_err(
            "plugin_factory must be a plain identifier",
        ));
    }
    if plugin_paths.is_empty() {
        return Err(PyValueError::new_err("plugin_paths must not be empty"));
    }
    let mut allowed = Vec::new();
    for path in &plugin_paths {
        if !path.is_absolute() || !path.is_dir() {
            return Err(PyValueError::new_err(
                "plugin_path must be an absolute directory",
            ));
        }
        allowed.push(resolved(path)?.join(format!("{module}.py")));
    }
    let cls = py.import("trade_engine.ledger.store")?.getattr("Ledger")?;
    if !ledger.is_instance(&cls)? {
        return Err(PyTypeError::new_err(
            "factory ledger must be the open owner Ledger",
        ));
    }
    ledger.getattr("conn")?;
    for method in ["now_utc", "sleep"] {
        if !clock.getattr(method)?.is_callable() {
            return Err(PyTypeError::new_err(format!(
                "factory clock.{method} must be callable"
            )));
        }
    }
    if !config.is_instance(&py.import("collections.abc")?.getattr("Mapping")?)? {
        return Err(PyTypeError::new_err("plugin_config must be a mapping"));
    }
    let _scope = FactoryScope::enter();
    let sys = py.import("sys")?;
    let original_path = sys.getattr("path")?;
    let paths = py
        .import("builtins")?
        .getattr("list")?
        .call1((plugin_paths,))?;
    paths.call_method1("extend", (&original_path,))?;
    sys.setattr("path", paths)?;
    let loaded = (|| -> PyResult<Py<PyAny>> {
        // Inspect the spec before importing: refused source files never execute.
        let spec = py
            .import("importlib.util")?
            .call_method1("find_spec", (module,))?;
        if !spec.is_none() {
            let origin: PathBuf = spec.getattr("origin")?.extract().map_err(|_| {
                PyRuntimeError::new_err("plugin source is outside configured plugin_paths")
            })?;
            if !allowed.contains(&resolved(&origin)?) {
                return Err(PyRuntimeError::new_err(
                    "plugin source is outside configured plugin_paths",
                ));
            }
        }
        let plugin = py.import(module)?;
        check_origin(plugin.as_any(), &allowed)?;
        let function = plugin.getattr(factory)?;
        if !function.is_callable() {
            return Err(PyTypeError::new_err(
                "configured plugin factory is not callable",
            ));
        }
        let context = Py::new(
            py,
            FactoryContext {
                ledger: Py::new(
                    py,
                    StoreView {
                        owner: ledger.clone().unbind(),
                    },
                )?,
                clock: Py::new(
                    py,
                    ClockView {
                        owner: clock.clone().unbind(),
                    },
                )?,
            },
        )?;
        let result = function.call1((context, config))?;
        check_result(&result)?;
        Ok(result.unbind())
    })();
    sys.setattr("path", original_path)?;
    loaded
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<StoreView>()?;
    m.add_class::<ClockView>()?;
    m.add_class::<FactoryContext>()?;
    m.add_function(wrap_pyfunction!(runtime_load_factory, m)?)?;
    Ok(())
}
