//! Reads `[memory]` from hub.toml through the matching Python runtime. The
//! hub calls this only for a native memory route, so a hub without memory
//! never needs `tomllib`.
use serde_json::{Value, json};
use std::path::Path;
use tokio::process::Command;

/// `Ok(Ok(table))` to open memory with, `Ok(Err(reason))` when memory should
/// answer as unavailable, or `Err` when the operator named the file with
/// `--memory-config` and it cannot be read, which stops startup.
pub async fn load(
    python: &str,
    path: &Path,
    explicit: bool,
) -> Result<Result<Value, String>, String> {
    // A missing default file means lexical mode and needs no interpreter. A
    // missing file the operator named is a typo, so it stops startup rather
    // than quietly running lexical.
    if matches!(path.try_exists(), Ok(false)) {
        if explicit {
            return Err(format!(
                "memory configuration {} does not exist",
                path.display()
            ));
        }
        return Ok(Ok(json!({})));
    }
    match read(python, path).await {
        Ok(table) => Ok(Ok(table)),
        Err(reason) if explicit => Err(format!(
            "memory configuration {} could not be read: {reason}",
            path.display()
        )),
        Err(reason) => Ok(Err(reason)),
    }
}

async fn read(python: &str, path: &Path) -> Result<Value, String> {
    // `-I`: no working directory or script directory on sys.path and no
    // PYTHON* variables, so a planted `json.py` or `tomllib.py` is never run.
    let output = Command::new(python)
        .arg("-I")
        .arg("-c")
        .arg(include_str!("../resolve_memory.py"))
        .arg(path)
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
