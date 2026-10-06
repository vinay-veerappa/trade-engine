use crate::config::{error, Config};
use pyo3::{ffi, prelude::*, types::PyDict};
use serde_json::{json, Value};
use std::{
    ffi::{c_void, CStr, CString},
    os::windows::ffi::OsStrExt,
    path::Path,
};

#[link(name = "kernel32")]
extern "system" {
    fn GetModuleHandleW(name: *const u16) -> *mut c_void;
    fn GetProcAddress(module: *mut c_void, name: *const u8) -> *mut c_void;
    fn GetModuleFileNameW(module: *mut c_void, buffer: *mut u16, size: u32) -> u32;
}

fn wide(path: &Path) -> Vec<u16> {
    path.as_os_str().encode_wide().chain(Some(0)).collect()
}

unsafe fn status(status: ffi::PyStatus) -> Result<(), Value> {
    if ffi::PyStatus_Exception(status) != 0 {
        let message = if status.err_msg.is_null() {
            "unknown initialization failure".into()
        } else {
            CStr::from_ptr(status.err_msg)
                .to_string_lossy()
                .into_owned()
        };
        return Err(error(format!(
            "interpreter initialization failed: {message}"
        )));
    }
    Ok(())
}

unsafe fn load_dll(config: &Config) -> Result<(), Value> {
    // PyO3 imports data as well as functions, so MSVC cannot delay-load CPython.
    // The release bundle colocates this DLL; verify the loader's actual choice.
    let module = GetModuleHandleW(wide(Path::new("python313.dll")).as_ptr());
    if module.is_null() {
        return Err(error("Python DLL was not loaded"));
    }
    let mut loaded = vec![0u16; 32768];
    let length = GetModuleFileNameW(module, loaded.as_mut_ptr(), loaded.len() as u32) as usize;
    let actual = std::path::PathBuf::from(String::from_utf16_lossy(&loaded[..length]));
    if std::fs::canonicalize(actual).ok() != std::fs::canonicalize(&config.python_dll).ok() {
        return Err(error("loaded Python DLL is not the configured DLL"));
    }
    let address = GetProcAddress(module, b"Py_GetVersion\0".as_ptr());
    if address.is_null() {
        return Err(error("python_dll lacks Py_GetVersion"));
    }
    let version: unsafe extern "C" fn() -> *const std::ffi::c_char = std::mem::transmute(address);
    if !CStr::from_ptr(version()).to_bytes().starts_with(b"3.13.") {
        return Err(error("python_dll is not CPython 3.13"));
    }
    Ok(())
}

unsafe fn initialize(config: &Config) -> Result<(), Value> {
    if ffi::Py_IsInitialized() != 0 {
        return Err(error("interpreter already initialized"));
    }
    trade_engine_rs::register_embedded_module().map_err(error)?;
    let mut raw = std::mem::MaybeUninit::<ffi::PyConfig>::uninit();
    ffi::PyConfig_InitIsolatedConfig(raw.as_mut_ptr());
    let mut raw = raw.assume_init();
    let result = (|| {
        raw.site_import = 0;
        raw.write_bytecode = 0;
        raw.install_signal_handlers = 0;
        raw.parse_argv = 0;
        raw.module_search_paths_set = 1;
        status(ffi::PyConfig_SetString(
            &mut raw,
            &mut raw.home,
            wide(&config.python_home).as_ptr(),
        ))?;
        status(ffi::PyConfig_SetString(
            &mut raw,
            &mut raw.executable,
            wide(&config.python_executable).as_ptr(),
        ))?;
        for path in config.search_paths() {
            status(ffi::PyWideStringList_Append(
                &mut raw.module_search_paths,
                wide(&path).as_ptr(),
            ))?;
        }
        status(ffi::Py_InitializeFromConfig(&raw))?;
        // Release the initialization thread's GIL; PyO3 attaches for the proof.
        ffi::PyEval_SaveThread();
        Ok(())
    })();
    ffi::PyConfig_Clear(&mut raw);
    result
}

fn python_error(py: Python<'_>, err: PyErr) -> Value {
    let name = err
        .get_type(py)
        .name()
        .map(|s| s.to_string())
        .unwrap_or_else(|_| "PythonError".into());
    let message = err
        .value(py)
        .str()
        .map(|s| s.to_string())
        .unwrap_or_else(|_| err.to_string());
    json!({"error": {"type": name, "message": message}})
}

/// Initialize the embedded interpreter for the runtime owner's HTTP loop.
/// Idempotent: an already-initialized interpreter (proof mode) is accepted.
pub fn initialize_for_serve(config: &Config) -> Result<(), String> {
    unsafe {
        if ffi::Py_IsInitialized() == 0 {
            load_dll(config).map_err(|e| e.to_string())?;
            initialize(config).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// The interpreter's platform.python_version() string for the server identity.
pub fn python_version() -> String {
    Python::with_gil(|py| {
        py.import("platform")
            .and_then(|platform| platform.call_method0("python_version"))
            .and_then(|version| version.extract::<String>())
            .unwrap_or_else(|_| "3.13".into())
    })
}

pub fn proof(config: &Config) -> Result<Value, Value> {
    unsafe {
        load_dll(config)?;
        initialize(config)?;
    }
    Python::with_gil(|py| -> Result<Value, Value> {
        if config.mode == "factory-proof" {
            return crate::plugins::proof(py, config).map_err(|e| python_error(py, e));
        }
        let locals = PyDict::new(py);
        let script = CString::new(include_str!("proof.py")).unwrap();
        let result: PyResult<String> = (|| {
            locals.set_item("config_json", serde_json::to_string(config).unwrap())?;
            locals.set_item("build_python", env!("TE_BUILD_PYTHON"))?;
            py.run(&script, Some(&locals), Some(&locals))?;
            locals.get_item("report_json")?.unwrap().extract()
        })();
        let text = result.map_err(|e| python_error(py, e))?;
        let mut report: Value = serde_json::from_str(&text).map_err(|e| error(e.to_string()))?;
        report["native_executable"] =
            json!(std::env::current_exe().map_err(|e| error(e.to_string()))?);
        report["python_dll"] = json!(config.python_dll);
        let repeated = unsafe { initialize(config) }.expect_err("initialization must refuse twice");
        report["reinitialize"] = repeated["error"].clone();
        Ok(report)
    })
}
