//! Heartbeat file effects. The binding supplies the existing JSON/date codecs.
use std::{fs, io, path::{Path, PathBuf}};

pub struct FileError {
    pub error: io::Error,
    pub path: PathBuf,
    pub destination: Option<PathBuf>,
}

pub fn read(path: &Path) -> Result<Option<Vec<u8>>, FileError> {
    match fs::metadata(path) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(FileError { error, path: path.into(), destination: None }),
        Ok(_) => {}
    }
    fs::read(path).map(Some)
        .map_err(|error| FileError { error, path: path.into(), destination: None })
}

pub fn write(path: &Path, temporary: &Path, body: &[u8]) -> Result<(), FileError> {
    fs::write(temporary, body)
        .map_err(|error| FileError { error, path: temporary.into(), destination: None })?;
    fs::rename(temporary, path)
        .map_err(|error| FileError { error, path: temporary.into(), destination: Some(path.into()) })
}
