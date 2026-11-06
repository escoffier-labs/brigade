//! Reads `[memory]` from hub.toml through the matching Python runtime. The
//! hub calls this only for a native memory route, so a hub without memory
//! never needs `tomllib`.
use serde_json::{Value, json};
use std::{path::Path, time::Duration};
use tokio::process::Command;

/// The whole lookup, existence check and Python child together. A FIFO with
/// no writer or a stalled mount cannot hold startup past this.
pub const LOOKUP_DEADLINE: Duration = Duration::from_secs(10);

/// `Ok(Ok(table))` to open memory with, `Ok(Err(reason))` when memory should
/// answer as unavailable, or `Err` when the operator named the file with
/// `--memory-config` and it cannot be read, which stops startup.
pub async fn load(
    python: &str,
    path: &Path,
    explicit: bool,
) -> Result<Result<Value, String>, String> {
    load_within(python, path, explicit, LOOKUP_DEADLINE).await
}

pub(crate) async fn load_within(
    python: &str,
    path: &Path,
    explicit: bool,
    deadline: Duration,
) -> Result<Result<Value, String>, String> {
    // Dropping the lookup on timeout drops the child, and `kill_on_drop`
    // kills it.
    let lookup = match tokio::time::timeout(deadline, lookup(python, path)).await {
        Ok(lookup) => lookup,
        Err(_) => Lookup::Failed(format!(
            "reading hub.toml timed out after {} s",
            deadline.as_secs_f64()
        )),
    };
    match lookup {
        Lookup::Found(table) => Ok(Ok(table)),
        // A missing default file means lexical mode. A missing file the
        // operator named is a typo, so it stops startup rather than quietly
        // running lexical.
        Lookup::Missing if explicit => Err(format!(
            "memory configuration {} does not exist",
            path.display()
        )),
        Lookup::Missing => Ok(Ok(json!({}))),
        Lookup::Failed(reason) if explicit => Err(format!(
            "memory configuration {} could not be read: {reason}",
            path.display()
        )),
        Lookup::Failed(reason) => Ok(Err(reason)),
    }
}

enum Lookup {
    Found(Value),
    Missing,
    Failed(String),
}

async fn lookup(python: &str, path: &Path) -> Lookup {
    // `tokio::fs` runs the stat off the runtime, so the deadline covers it. A
    // missing file needs no interpreter.
    if matches!(tokio::fs::try_exists(path).await, Ok(false)) {
        return Lookup::Missing;
    }
    match read(python, path).await {
        Ok(table) => Lookup::Found(table),
        Err(reason) => Lookup::Failed(reason),
    }
}

async fn read(python: &str, path: &Path) -> Result<Value, String> {
    // `-I`: no working directory or script directory on sys.path and no
    // PYTHON* variables, so a planted `json.py` or `tomllib.py` is never run.
    // `--required`: the file existed a moment ago, so one gone by the time
    // Python opens it is a read failure, not lexical mode.
    let output = Command::new(python)
        .arg("-I")
        .arg("-c")
        .arg(include_str!("../resolve_memory.py"))
        .arg("--required")
        .arg(path)
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true)
        .output()
        .await
        .map_err(|_| format!("{python} is unavailable"))?;
    if !output.status.success() {
        return Err("reading hub.toml needs Python 3.11 or newer and a readable TOML file".into());
    }
    serde_json::from_slice(&output.stdout).map_err(|_| "invalid memory configuration".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dir() -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!(
            "hub-memory-config-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[tokio::test]
    async fn missing_interpreter_reports_memory_unavailable_unless_named() {
        let d = dir();
        let toml = d.join("hub.toml");
        std::fs::write(&toml, "[memory]\nretrieval = \"lexical\"\n").unwrap();
        let missing = d.join("no-such-python3");
        let missing = missing.to_str().unwrap();
        // The default file: memory is unavailable and startup goes on.
        let reason = load(missing, &toml, false).await.unwrap().unwrap_err();
        assert!(reason.contains("unavailable"), "{reason}");
        // A file the operator named: startup stops with the cause.
        let error = load(missing, &toml, true).await.unwrap_err();
        assert!(error.contains("could not be read"), "{error}");
        // No default file: lexical, with no interpreter at all.
        assert_eq!(
            load(missing, &d.join("absent.toml"), false)
                .await
                .unwrap()
                .unwrap(),
            json!({})
        );
        std::fs::remove_dir_all(d).unwrap();
    }

    #[tokio::test]
    async fn a_named_config_that_does_not_exist_stops_startup() {
        let d = dir();
        let error = load("python3", &d.join("typo.toml"), true)
            .await
            .unwrap_err();
        assert!(error.contains("does not exist"), "{error}");
        std::fs::remove_dir_all(d).unwrap();
    }

    #[tokio::test]
    async fn a_config_gone_after_the_existence_check_is_a_read_failure() {
        // The race: the hub saw the file, then it went before Python opened
        // it. Python must not answer `{}`, which would mean lexical mode.
        let d = dir();
        let reason = read("python3", &d.join("gone.toml")).await.unwrap_err();
        assert!(reason.contains("readable TOML file"), "{reason}");
        std::fs::remove_dir_all(d).unwrap();
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn a_fifo_without_a_writer_times_out_and_the_child_is_killed() {
        use std::os::unix::ffi::OsStrExt;
        let d = dir();
        let fifo = d.join("hub.toml");
        let name = std::ffi::CString::new(fifo.as_os_str().as_bytes()).unwrap();
        // SAFETY: `name` is a valid NUL-terminated path.
        assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
        let deadline = Duration::from_millis(500);
        let started = std::time::Instant::now();
        let reason = load_within("python3", &fifo, false, deadline)
            .await
            .unwrap()
            .unwrap_err();
        assert!(reason.contains("timed out"), "{reason}");
        let error = load_within("python3", &fifo, true, deadline)
            .await
            .unwrap_err();
        assert!(
            error.contains("could not be read") && error.contains("timed out"),
            "{error}"
        );
        assert!(started.elapsed() < Duration::from_secs(5));
        // A writer-side open fails with ENXIO while no process is opening or
        // holding the FIFO for reading, so a live child would make it succeed.
        // One check after a grace period: a successful open would itself
        // release a stuck child and hide it from a retry.
        tokio::time::sleep(Duration::from_secs(1)).await;
        // SAFETY: `name` is a valid NUL-terminated path.
        let fd = unsafe { libc::open(name.as_ptr(), libc::O_WRONLY | libc::O_NONBLOCK) };
        let enxio = std::io::Error::last_os_error().raw_os_error() == Some(libc::ENXIO);
        if fd >= 0 {
            // SAFETY: `fd` was just opened and is closed once.
            unsafe { libc::close(fd) };
        }
        assert!(
            fd < 0 && enxio,
            "a timed-out Python child still has the FIFO open"
        );
        std::fs::remove_dir_all(d).unwrap();
    }

    #[tokio::test]
    async fn unreadable_toml_reports_memory_unavailable() {
        let d = dir();
        let toml = d.join("hub.toml");
        std::fs::write(&toml, "[memory\n").unwrap();
        let reason = load("python3", &toml, false).await.unwrap().unwrap_err();
        assert!(reason.contains("Python 3.11"), "{reason}");
        assert!(load("python3", &toml, true).await.is_err());
        std::fs::write(&toml, "[memory]\nretrieval = \"lexical\"\n").unwrap();
        assert_eq!(
            load("python3", &toml, false).await.unwrap().unwrap(),
            json!({"retrieval": "lexical"})
        );
        std::fs::remove_dir_all(d).unwrap();
    }
}
