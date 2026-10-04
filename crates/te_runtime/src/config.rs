use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};

pub fn error(message: impl Into<String>) -> Value {
    json!({"error": {"type": "RuntimeConfigError", "message": message.into()}})
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub mode: String,
    pub python_home: PathBuf,
    pub python_dll: PathBuf,
    pub python_executable: PathBuf,
    pub site_packages: PathBuf,
    pub engine_source: PathBuf,
    pub plugin_paths: Vec<PathBuf>,
    pub plugin_module: String,
    pub plugin_factory: String,
    pub plugin_config: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub owner: Option<OwnerConfig>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct OwnerConfig {
    pub ledger_path: PathBuf,
    pub clock: String,
    pub initial_time: Option<String>,
}

fn directory(path: &Path, name: &str) -> Result<(), Value> {
    if !path.is_absolute() || !path.is_dir() {
        return Err(error(format!("{name} must be an absolute directory")));
    }
    Ok(())
}

fn file(path: &Path, name: &str) -> Result<(), Value> {
    if !path.is_absolute() || !path.is_file() {
        return Err(error(format!("{name} must be an absolute file")));
    }
    Ok(())
}

fn identifier(name: &str) -> bool {
    !name.is_empty()
        && name
            .bytes()
            .enumerate()
            .all(|(i, c)| c == b'_' || c.is_ascii_alphabetic() || (i > 0 && c.is_ascii_digit()))
}

impl Config {
    pub fn read(path: &Path) -> Result<Self, Value> {
        if !path.is_absolute() {
            return Err(error("config path must be absolute"));
        }
        let bytes = std::fs::read(path).map_err(|e| error(format!("config read failed: {e}")))?;
        let config: Self = serde_json::from_slice(&bytes)
            .map_err(|e| error(format!("config parse failed: {e}")))?;
        config.validate()?;
        if let Some(owner) = &config.owner {
            let parent = owner
                .ledger_path
                .parent()
                .ok_or_else(|| error("owner.ledger_path must be an absolute offline path"))?;
            let actual = std::fs::canonicalize(parent)
                .map_err(|_| error("owner.ledger_path parent must exist"))?;
            let root =
                std::fs::canonicalize(path.parent().unwrap()).map_err(|e| error(e.to_string()))?;
            if !actual.starts_with(&root) {
                return Err(error(
                    "factory-proof ledger must be inside the offline config directory",
                ));
            }
            if owner.ledger_path.exists() {
                let ledger =
                    std::fs::canonicalize(&owner.ledger_path).map_err(|e| error(e.to_string()))?;
                if !ledger.starts_with(&root) {
                    return Err(error(
                        "factory-proof ledger must be inside the offline config directory",
                    ));
                }
            }
        }
        Ok(config)
    }

    fn validate(&self) -> Result<(), Value> {
        if self.mode != "packaging-proof" && self.mode != "factory-proof" {
            return Err(error(format!("unsupported mode: {}", self.mode)));
        }
        directory(&self.python_home, "python_home")?;
        file(&self.python_dll, "python_dll")?;
        file(&self.python_executable, "python_executable")?;
        directory(&self.site_packages, "site_packages")?;
        directory(&self.engine_source, "engine_source")?;
        if !self
            .python_home
            .join("Lib")
            .join("encodings")
            .join("__init__.py")
            .is_file()
            || !self.python_home.join("DLLs").is_dir()
        {
            return Err(error("python_home lacks Python 3.13 standard library"));
        }
        let executable = std::env::current_exe().map_err(|e| error(e.to_string()))?;
        if self.python_dll != executable.parent().unwrap().join("python313.dll") {
            return Err(error("python_dll must be bundled beside te.exe"));
        }
        let bundled = std::fs::read(&self.python_dll).map_err(|e| error(e.to_string()))?;
        let original = std::fs::read(self.python_home.join("python313.dll"))
            .map_err(|_| error("python_home lacks python313.dll"))?;
        if bundled != original {
            return Err(error("bundled Python DLL differs from configured home"));
        }
        let prefix = self.prefix()?;
        if self.site_packages != prefix.join("Lib").join("site-packages") {
            return Err(error(
                "site_packages must belong to the configured private venv",
            ));
        }
        let venv = std::fs::read_to_string(prefix.join("pyvenv.cfg"))
            .map_err(|_| error("python_executable must belong to a private venv"))?;
        let home = venv.lines().find_map(|line| line.strip_prefix("home = "));
        let version = venv
            .lines()
            .find_map(|line| line.strip_prefix("version = "));
        if home.map(Path::new) != Some(self.python_home.as_path())
            || !version.is_some_and(|v| v.starts_with("3.13."))
        {
            return Err(error("private venv must use configured Python 3.13 home"));
        }
        if !self
            .engine_source
            .join("trade_engine")
            .join("__init__.py")
            .is_file()
        {
            return Err(error("engine_source lacks trade_engine"));
        }
        if self.plugin_paths.is_empty() {
            return Err(error("plugin_paths must not be empty"));
        }
        for path in &self.plugin_paths {
            directory(path, "plugin_path")?;
        }
        if self.plugin_module == "trade_engine_rs" || self.plugin_module == "trade_engine" {
            return Err(error("plugin_module is reserved"));
        }
        if !identifier(&self.plugin_module) {
            return Err(error("plugin_module must be a plain module identifier"));
        }
        if !identifier(&self.plugin_factory) {
            return Err(error("plugin_factory must be a plain identifier"));
        }
        if !self.plugin_config.is_object() {
            return Err(error("plugin_config must be an object"));
        }
        match (&self.owner, self.mode.as_str()) {
            (None, "factory-proof") => return Err(error("factory-proof requires owner config")),
            (Some(_), "packaging-proof") => {
                return Err(error("packaging-proof cannot configure an owner"))
            }
            (Some(owner), "factory-proof") => {
                if !owner.ledger_path.is_absolute() {
                    return Err(error("owner.ledger_path must be an absolute offline path"));
                }
                match (owner.clock.as_str(), &owner.initial_time) {
                    ("replay", Some(_)) | ("wall", None) => {}
                    ("replay", None) => return Err(error("replay clock requires initial_time")),
                    ("wall", Some(_)) => {
                        return Err(error("wall clock cannot configure initial_time"))
                    }
                    _ => return Err(error("owner.clock must be replay or wall")),
                }
            }
            _ => {}
        }
        Ok(())
    }

    pub fn prefix(&self) -> Result<PathBuf, Value> {
        let scripts = self
            .python_executable
            .parent()
            .ok_or_else(|| error("python_executable must belong to a private venv"))?;
        if scripts.file_name().and_then(|s| s.to_str()) != Some("Scripts")
            || self.python_executable.file_name().and_then(|s| s.to_str()) != Some("python.exe")
        {
            return Err(error("python_executable must belong to a private venv"));
        }
        Ok(scripts.parent().unwrap().to_owned())
    }

    pub fn search_paths(&self) -> Vec<PathBuf> {
        let mut paths = vec![
            self.python_home.join("Lib"),
            self.python_home.join("DLLs"),
            self.site_packages.clone(),
            self.engine_source.clone(),
        ];
        paths.extend(self.plugin_paths.clone());
        paths
    }
}
