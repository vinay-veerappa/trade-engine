use std::fs::{self, File, OpenOptions};
use std::io::{self, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

#[derive(Debug)]
pub enum LockError {
    Contended,
    NullPath { mkdir: bool },
    Io { error: io::Error, path: PathBuf, mkdir: bool },
}

fn mkdir(path: &Path) -> Result<(), LockError> {
    if path.as_os_str().as_encoded_bytes().contains(&0) {
        return Err(LockError::NullPath { mkdir: true });
    }
    match fs::create_dir(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            if let Some(parent) = path.parent().filter(|p| *p != path && !p.as_os_str().is_empty()) {
                mkdir(parent)?;
                match fs::create_dir(path) {
                    Ok(()) => Ok(()),
                    Err(_) if path.is_dir() => Ok(()),
                    Err(error) => Err(LockError::Io {
                        error, path: path.into(), mkdir: true,
                    }),
                }
            } else {
                Err(LockError::Io { error, path: path.into(), mkdir: true })
            }
        }
        Err(_) if path.is_dir() => Ok(()),
        Err(error) => Err(LockError::Io { error, path: path.into(), mkdir: true }),
    }
}

/// Closing this file releases the OS lock, including during process teardown.
#[derive(Debug)]
pub struct SingleInstanceGuard {
    file: File,
}

impl SingleInstanceGuard {
    pub fn acquire(path: &Path, pid: &str) -> Result<Self, LockError> {
        let parent = path.parent().filter(|p| !p.as_os_str().is_empty()).unwrap_or(Path::new("."));
        mkdir(parent)?;
        if path.as_os_str().as_encoded_bytes().contains(&0) {
            return Err(LockError::NullPath { mkdir: false });
        }
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true);
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            // Match Python's CRT open: share read/write, never delete a held
            // sidecar and permit a second writer to lock a replacement inode.
            options.share_mode(0x1 | 0x2);
        }
        let file = options.open(path)
            .map_err(|error| LockError::Io { error, path: path.into(), mkdir: false })?;
        let mut lock = fd_lock::RwLock::new(file);
        let mut guard = lock.try_write().map_err(|_| LockError::Contended)?;
        let write = (|| -> io::Result<()> {
            guard.seek(SeekFrom::Start(0))?;
            guard.set_len(0)?;
            guard.write_all(format!("pid={pid}\n").as_bytes())?;
            guard.flush()
        })();
        write.map_err(|error| LockError::Io { error, path: path.into(), mkdir: false })?;
        // fd-lock's guard borrows its RwLock. Transfer release to the owned File's
        // close instead of storing a self-reference or leaking the descriptor.
        std::mem::forget(guard);
        Ok(Self { file: lock.into_inner() })
    }

    pub fn release(self) {
        drop(self.file);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;

    #[test]
    fn native_guard_contention_pid_and_release() {
        let parent = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join(format!("../../.ci-local/native-lock-{}", std::process::id()));
        fs::create_dir_all(&parent).unwrap();
        let path = parent.join("book.lock");
        fs::write(&path, b"stale data that must be truncated").unwrap();
        let mut guard = SingleInstanceGuard::acquire(&path, "123").unwrap();
        assert!(matches!(SingleInstanceGuard::acquire(&path, "456"), Err(LockError::Contended)));
        guard.file.seek(SeekFrom::Start(0)).unwrap();
        let mut text = String::new();
        guard.file.read_to_string(&mut text).unwrap();
        assert_eq!(text, "pid=123\n");
        guard.release();
        SingleInstanceGuard::acquire(&path, "789").unwrap().release();
        assert_eq!(fs::read(&path).unwrap(), b"pid=789\n");
        fs::remove_file(path).unwrap();
        fs::remove_dir(parent).unwrap();
    }
}
