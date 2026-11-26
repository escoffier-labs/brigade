use std::ffi::c_void;
use std::fs::{File, OpenOptions};
use std::io::{self, ErrorKind, Read, Write};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle, RawHandle};
use std::path::Path;

use anyhow::{Context, Result, bail};

use super::STALE_LOCK_MAX_AGE_SECS;

const DELETE: u32 = 0x0001_0000;
const GENERIC_READ: u32 = 0x8000_0000;
const GENERIC_WRITE: u32 = 0x4000_0000;
const FILE_SHARE_READ: u32 = 0x0000_0001;
const FILE_FLAG_DELETE_ON_CLOSE: u32 = 0x0400_0000;
const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
const FILE_DISPOSITION_INFO_CLASS: i32 = 4;
const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;
const STILL_ACTIVE: u32 = 259;
const ERROR_SHARING_VIOLATION: i32 = 32;
const ERROR_INVALID_PARAMETER: i32 = 87;

// These kernel32 entry points complement std's Windows file and handle APIs.
#[link(name = "kernel32")]
unsafe extern "system" {
    fn OpenProcess(access: u32, inherit: i32, pid: u32) -> RawHandle;
    fn GetExitCodeProcess(process: RawHandle, exit_code: *mut u32) -> i32;
    fn SetFileInformationByHandle(
        file: RawHandle,
        info_class: i32,
        info: *mut c_void,
        size: u32,
    ) -> i32;
}

pub(super) fn acquire(path: &Path) -> Result<File> {
    let mut file = match create(path) {
        Ok(file) => file,
        Err(err) if is_lock_conflict(&err) => {
            reclaim_legacy(path, err)?;
            create(path).map_err(|err| create_error(path, err))?
        }
        Err(err) => return Err(create_error(path, err)),
    };
    writeln!(file, "{}", std::process::id())
        .with_context(|| format!("failed to write lock {}", path.display()))?;
    Ok(file)
}

fn create(path: &Path) -> io::Result<File> {
    // std opens non-inheritable handles. Share reads for PID diagnostics, but
    // deny DELETE and writes even to an old engine using remove_file by name.
    OpenOptions::new()
        .write(true)
        .access_mode(GENERIC_WRITE | DELETE)
        .share_mode(FILE_SHARE_READ)
        .custom_flags(FILE_FLAG_DELETE_ON_CLOSE | FILE_FLAG_OPEN_REPARSE_POINT)
        .create_new(true)
        .open(path)
}

fn is_lock_conflict(err: &io::Error) -> bool {
    // A delete-pending file can also make CREATE_NEW return access denied.
    matches!(
        err.kind(),
        ErrorKind::AlreadyExists | ErrorKind::PermissionDenied
    ) || err.raw_os_error() == Some(ERROR_SHARING_VIOLATION)
}

fn reclaim_legacy(path: &Path, create_err: io::Error) -> Result<()> {
    let mut file = match OpenOptions::new()
        .access_mode(GENERIC_READ | DELETE)
        .share_mode(0)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
    {
        Ok(file) => file,
        // The previous owner or another reclaimer already closed the lock.
        Err(err) if err.kind() == ErrorKind::NotFound => return Ok(()),
        Err(_) => return Err(create_error(path, create_err)),
    };

    // This handle excludes competing readers, writers and deleters. Keep it
    // through the deletion so a racing contender cannot substitute a new file.
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        bail!(
            "refusing to reclaim a lock that is not a regular file: {}",
            path.display()
        );
    }
    if let Some(pid) = read_pid(&mut file)? {
        match pid_alive(pid) {
            Ok(false) => {}
            Ok(true) => return Err(busy(path, Some(pid))),
            Err(err) => {
                return Err(err).with_context(|| {
                    format!(
                        "could not verify lock owner PID {pid} for {}",
                        path.display()
                    )
                });
            }
        }
    } else if !metadata
        .modified()
        .ok()
        .and_then(|modified| modified.elapsed().ok())
        .is_some_and(|age| age.as_secs() > STALE_LOCK_MAX_AGE_SECS)
    {
        return Err(busy(path, None));
    }
    delete_on_close(&file).with_context(|| format!("failed to reclaim lock {}", path.display()))?;
    drop(file);
    let _ = writeln!(
        io::stderr().lock(),
        "reclaimed stale sync lock {}",
        path.display()
    );
    Ok(())
}

fn read_pid(file: &mut File) -> io::Result<Option<u32>> {
    let mut contents = Vec::new();
    file.take(64).read_to_end(&mut contents)?;
    if contents.len() == 64 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "lock PID record too long",
        ));
    }
    Ok(std::str::from_utf8(&contents)
        .ok()
        .and_then(|content| content.trim().parse::<u32>().ok())
        .filter(|pid| *pid != 0))
}

fn create_error(path: &Path, err: io::Error) -> anyhow::Error {
    if is_lock_conflict(&err) {
        // The reader shares DELETE with the holder. Its own access is read-only.
        let live_pid = OpenOptions::new()
            .read(true)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
            .open(path)
            .ok()
            .and_then(|mut file| read_pid(&mut file).ok().flatten())
            .filter(|pid| matches!(pid_alive(*pid), Ok(true)));
        if live_pid.is_some()
            || err.kind() == ErrorKind::AlreadyExists
            || err.raw_os_error() == Some(ERROR_SHARING_VIOLATION)
        {
            return busy(path, live_pid);
        }
    }
    anyhow::Error::new(err).context(format!("failed to create lock {}", path.display()))
}

fn busy(path: &Path, live_pid: Option<u32>) -> anyhow::Error {
    let owner = live_pid.map_or_else(String::new, |pid| format!(" (PID {pid} is live)"));
    anyhow::anyhow!(
        "another sync is already running for this database{owner} (lock: {})",
        path.display()
    )
}

fn pid_alive(pid: u32) -> io::Result<bool> {
    // SAFETY: OpenProcess takes only scalar arguments. It returns an owned
    // process handle on success, and null on failure.
    let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if process.is_null() {
        let err = io::Error::last_os_error();
        return match err.raw_os_error() {
            // PID zero is filtered out by read_pid. Here this means no such PID.
            Some(ERROR_INVALID_PARAMETER) => Ok(false),
            // In particular, access denied is not proof of death.
            _ => Err(err),
        };
    }
    // SAFETY: The handle was returned by OpenProcess and is owned by this call.
    let process = unsafe { OwnedHandle::from_raw_handle(process) };
    let mut exit_code = 0;
    // SAFETY: The handle is valid and exit_code is a writable DWORD.
    if unsafe { GetExitCodeProcess(process.as_raw_handle(), &mut exit_code) } == 0 {
        return Err(io::Error::last_os_error());
    }
    // 259 as a real exit code is indistinguishable from a running process.
    // In that case also err on the side of preserving the legacy lock.
    Ok(exit_code == STILL_ACTIVE)
}

fn delete_on_close(file: &File) -> io::Result<()> {
    // FILE_DISPOSITION_INFO contains a single BOOLEAN DeleteFile field.
    let mut delete_file = 1_u8;
    // SAFETY: The file was opened with DELETE access and the one-byte buffer
    // is live for the duration of this call.
    let result = unsafe {
        SetFileInformationByHandle(
            file.as_raw_handle(),
            FILE_DISPOSITION_INFO_CLASS,
            (&mut delete_file as *mut u8).cast(),
            1,
        )
    };
    if result == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(test)]
mod tests;
