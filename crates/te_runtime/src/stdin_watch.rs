//! The owner serves until its stdin closes; this is how it waits for that.
//!
//! A synchronous read parked on the stdin pipe holds that file object's I/O lock for the whole
//! serve on Windows, so any other thread's synchronous call on the same pipe waits behind it
//! forever. The C runtime's `fstat` on fd 0 is one (it `PeekNamedPipe`s a pipe), and it runs
//! inside numpy's OpenBLAS load: a job that imported numpy hung in `LoadLibrary`. A pipe is
//! therefore polled with `PeekNamedPipe`, which returns at once and never holds the lock;
//! stdin that is not a pipe (a file, NUL, a console) keeps the plain blocking read.
use std::io::Read;
use std::os::windows::io::AsRawHandle;
use std::time::Duration;

const ERROR_BROKEN_PIPE: i32 = 109;
const POLL: Duration = Duration::from_millis(100);

extern "system" {
    fn PeekNamedPipe(
        pipe: *mut core::ffi::c_void,
        buffer: *mut core::ffi::c_void,
        buffer_size: u32,
        bytes_read: *mut u32,
        bytes_available: *mut u32,
        bytes_left: *mut u32,
    ) -> i32;
}

enum Peek {
    /// The pipe is open; this many bytes are waiting to be read.
    Open(u32),
    /// The write end closed: stdin is finished.
    Closed,
    /// The handle is not a pipe.
    NotAPipe,
}

fn peek(handle: *mut core::ffi::c_void) -> Peek {
    let mut available: u32 = 0;
    // SAFETY: the handle is the process's live stdin handle; every pointer is null or local.
    let ok = unsafe {
        PeekNamedPipe(
            handle,
            core::ptr::null_mut(),
            0,
            core::ptr::null_mut(),
            &mut available,
            core::ptr::null_mut(),
        )
    };
    if ok != 0 {
        return Peek::Open(available);
    }
    match std::io::Error::last_os_error().raw_os_error() {
        Some(ERROR_BROKEN_PIPE) => Peek::Closed,
        _ => Peek::NotAPipe,
    }
}

/// Return when stdin has closed (or errored).
pub fn wait_for_close() {
    let mut stdin = std::io::stdin();
    let handle = stdin.as_raw_handle();
    let mut buffer = [0u8; 1024];
    loop {
        match peek(handle) {
            Peek::Closed => return,
            Peek::Open(0) => std::thread::sleep(POLL),
            // Data is waiting, so this read returns without parking.
            Peek::Open(_) => match stdin.read(&mut buffer) {
                Ok(0) | Err(_) => return,
                Ok(_) => {}
            },
            Peek::NotAPipe => break,
        }
    }
    loop {
        match stdin.read(&mut buffer) {
            Ok(0) | Err(_) => return,
            Ok(_) => {}
        }
    }
}
