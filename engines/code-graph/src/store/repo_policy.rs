//! Repository safety, git metadata, and index directory ignore policy.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use ignore::gitignore::GitignoreBuilder;
use rusqlite::Connection;

use super::env::read_renamed;

pub const ALLOW_UNSAFE_ROOT_ENV: &str = "BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT";
pub const LEGACY_ALLOW_UNSAFE_ROOT_ENV: &str = "GRAPHTRAIL_ALLOW_UNSAFE_ROOT";

/// Refuse to sync roots that are never a real project: the filesystem root and the user's home
/// directory. Outside a git repo the walker has no gitignore to lean on, so a sync there parses
/// every cache, toolchain, and vendored source tree on the machine into one giant graph nobody
/// asked for. Set `BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT=1` to bypass (the legacy variable still
/// works for the rename window, with a warning).
pub(crate) fn guard_unsafe_root(root: &Path) -> Result<()> {
    if read_renamed(ALLOW_UNSAFE_ROOT_ENV, LEGACY_ALLOW_UNSAFE_ROOT_ENV)?
        .is_some_and(|value| value == "1")
    {
        return Ok(());
    }
    let home = std::env::var_os("HOME").map(|home| {
        let home = PathBuf::from(home);
        home.canonicalize().unwrap_or(home)
    });
    if let Some(reason) = unsafe_root_reason(root, home.as_deref()) {
        anyhow::bail!(
            "refusing to sync {}: {reason}. Run sync from a project directory, or set \
             {ALLOW_UNSAFE_ROOT_ENV}=1 to override.",
            root.display()
        );
    }
    Ok(())
}

fn unsafe_root_reason(root: &Path, home: Option<&Path>) -> Option<&'static str> {
    if root.parent().is_none() {
        return Some("root is the filesystem root");
    }
    if home.is_some_and(|home| root == home) {
        return Some("root is the home directory");
    }
    None
}

pub(super) fn has_git_context(root: &Path) -> bool {
    root.ancestors().any(has_git_marker)
}

pub(super) fn has_git_marker(dir: &Path) -> bool {
    let git = dir.join(".git");
    git.is_file() || git.join("HEAD").is_file()
}

/// Record which branch and commit the graph describes, so `doctor` can flag a
/// checkout of a different branch as drift and report a moved HEAD. Each key
/// is removed when it cannot be read, so a repo that stops being one does not
/// pin a stale branch or commit forever.
pub(super) fn write_branch_meta(tx: &Connection, root: &Path) -> Result<()> {
    for (key, value) in [
        ("synced_branch", current_git_branch(root)),
        ("synced_head", current_git_head(root)),
    ] {
        match value {
            Some(value) => crate::store::meta::upsert(tx, key, &value)?,
            None => {
                tx.execute("DELETE FROM meta WHERE key = ?1", [key])?;
            }
        }
    }
    Ok(())
}

/// Current branch name from `.git/HEAD`, without spawning git. Follows the
/// `gitdir:` pointer of linked worktrees. Detached heads report the short
/// commit as `detached@<12 hex>`.
pub(crate) fn current_git_branch(root: &Path) -> Option<String> {
    let git_dir = git_dir(root)?;
    let head = fs::read_to_string(git_dir.join("HEAD")).ok()?;
    let head = head.trim();
    if let Some(reference) = head.strip_prefix("ref:") {
        let reference = reference.trim();
        let branch = reference.strip_prefix("refs/heads/").unwrap_or(reference);
        return Some(branch.to_string());
    }
    let short: String = head.chars().take(12).collect();
    if short.chars().all(|character| character.is_ascii_hexdigit()) && !short.is_empty() {
        Some(format!("detached@{short}"))
    } else {
        None
    }
}

/// Full commit id HEAD points at, without spawning git. Resolves a symbolic
/// HEAD through loose refs in the worktree and common git dirs, then
/// `packed-refs`. `None` for an unborn branch or an unreadable repository.
pub(crate) fn current_git_head(root: &Path) -> Option<String> {
    let git_dir = git_dir(root)?;
    let head = fs::read_to_string(git_dir.join("HEAD")).ok()?;
    let head = head.trim();
    let Some(reference) = head.strip_prefix("ref:") else {
        return is_object_id(head).then(|| head.to_string());
    };
    let reference = reference.trim();
    if !reference.starts_with("refs/") || reference.split('/').any(|part| part == "..") {
        return None;
    }
    let common = common_git_dir(&git_dir);
    for dir in [&git_dir, &common] {
        if let Ok(value) = fs::read_to_string(dir.join(reference)) {
            let value = value.trim();
            if is_object_id(value) {
                return Some(value.to_string());
            }
        }
    }
    let packed = fs::read_to_string(common.join("packed-refs")).ok()?;
    packed.lines().find_map(|line| {
        let (id, name) = line.split_once(' ')?;
        (name.trim() == reference && is_object_id(id)).then(|| id.to_string())
    })
}

fn is_object_id(value: &str) -> bool {
    matches!(value.len(), 40 | 64) && value.chars().all(|character| character.is_ascii_hexdigit())
}

/// A linked worktree's git dir names the shared repository in `commondir`;
/// branch refs and `packed-refs` live there.
fn common_git_dir(git_dir: &Path) -> PathBuf {
    let Ok(content) = fs::read_to_string(git_dir.join("commondir")) else {
        return git_dir.to_path_buf();
    };
    let pointed = Path::new(content.trim());
    if pointed.is_absolute() {
        pointed.to_path_buf()
    } else {
        git_dir.join(pointed)
    }
}

/// The git dir for `root`, following the `gitdir:` pointer of linked worktrees.
fn git_dir(root: &Path) -> Option<PathBuf> {
    root.ancestors().find_map(|dir| {
        let git = dir.join(".git");
        if git.join("HEAD").is_file() {
            return Some(git);
        }
        if git.is_file() {
            let content = fs::read_to_string(&git).ok()?;
            let pointed = content.strip_prefix("gitdir:")?.trim();
            let pointed = if Path::new(pointed).is_absolute() {
                PathBuf::from(pointed)
            } else {
                dir.join(pointed)
            };
            if pointed.join("HEAD").is_file() {
                return Some(pointed);
            }
        }
        None
    })
}

/// Append `index_dir` (a directory under `root`) to `root/.gitignore` unless it, or a parent of
/// it, is already ignored.
pub(super) fn ensure_index_ignored(root: &Path, index_dir: &Path) -> Result<()> {
    let gitignore = root.join(".gitignore");
    if gitignore_covers(root, &gitignore, index_dir)? {
        return Ok(());
    }
    let entry = index_dir
        .strip_prefix(root)
        .unwrap_or(index_dir)
        .components()
        .map(|part| part.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/");

    let mut needs_leading_newline = false;
    if gitignore.exists() {
        let content = fs::read_to_string(&gitignore)
            .with_context(|| format!("failed to read {}", gitignore.display()))?;
        needs_leading_newline = !content.is_empty() && !content.ends_with('\n');
    }

    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&gitignore)
        .with_context(|| format!("failed to open {}", gitignore.display()))?;
    if needs_leading_newline {
        writeln!(file)?;
    }
    writeln!(file, "{entry}/")?;
    println!("updated {} to ignore {entry}/", gitignore.display());
    Ok(())
}

fn gitignore_covers(root: &Path, gitignore: &Path, index_dir: &Path) -> Result<bool> {
    if !gitignore.exists() {
        return Ok(false);
    }

    let mut builder = GitignoreBuilder::new(root);
    if let Some(error) = builder.add(gitignore) {
        return Err(error).with_context(|| format!("failed to parse {}", gitignore.display()));
    }
    let matcher = builder
        .build()
        .with_context(|| format!("failed to parse {}", gitignore.display()))?;
    Ok(matcher
        .matched_path_or_any_parents(index_dir, true)
        .is_ignore())
}

#[cfg(test)]
mod tests {
    use super::unsafe_root_reason;
    use std::path::Path;

    #[test]
    fn filesystem_root_is_unsafe() {
        assert_eq!(
            unsafe_root_reason(Path::new("/"), None),
            Some("root is the filesystem root")
        );
    }

    #[test]
    fn home_directory_is_unsafe() {
        let home = Path::new("/home/someone");
        assert_eq!(
            unsafe_root_reason(home, Some(home)),
            Some("root is the home directory")
        );
    }

    #[test]
    fn project_directory_under_home_is_safe() {
        let home = Path::new("/home/someone");
        assert_eq!(
            unsafe_root_reason(Path::new("/home/someone/repos/project"), Some(home)),
            None
        );
    }

    #[test]
    fn any_directory_is_safe_without_home() {
        assert_eq!(unsafe_root_reason(Path::new("/srv/project"), None), None);
    }
}
