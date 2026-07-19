//! Repository safety, git metadata, and `.graphtrail` ignore policy.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use ignore::gitignore::GitignoreBuilder;
use rusqlite::Connection;

/// Refuse to sync roots that are never a real project: the filesystem root and the user's home
/// directory. Outside a git repo the walker has no gitignore to lean on, so a sync there parses
/// every cache, toolchain, and vendored source tree on the machine into one giant graph nobody
/// asked for. Set `GRAPHTRAIL_ALLOW_UNSAFE_ROOT=1` to bypass.
pub(crate) fn guard_unsafe_root(root: &Path) -> Result<()> {
    if std::env::var_os("GRAPHTRAIL_ALLOW_UNSAFE_ROOT").is_some_and(|value| value == "1") {
        return Ok(());
    }
    let home = std::env::var_os("HOME").map(|home| {
        let home = PathBuf::from(home);
        home.canonicalize().unwrap_or(home)
    });
    if let Some(reason) = unsafe_root_reason(root, home.as_deref()) {
        anyhow::bail!(
            "refusing to sync {}: {reason}. Run sync from a project directory, or set \
             GRAPHTRAIL_ALLOW_UNSAFE_ROOT=1 to override.",
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

/// Record which branch the graph describes, so `doctor` can flag a checkout
/// of a different branch as drift. Removed when the root has no git context,
/// so a repo that stops being one does not pin a stale branch forever.
pub(super) fn write_branch_meta(tx: &Connection, root: &Path) -> Result<()> {
    match current_git_branch(root) {
        Some(branch) => crate::store::meta::upsert(tx, "synced_branch", &branch)?,
        None => {
            tx.execute("DELETE FROM meta WHERE key = 'synced_branch'", [])?;
        }
    }
    Ok(())
}

/// Current branch name from `.git/HEAD`, without spawning git. Follows the
/// `gitdir:` pointer of linked worktrees. Detached heads report the short
/// commit as `detached@<12 hex>`.
pub(crate) fn current_git_branch(root: &Path) -> Option<String> {
    let git_dir = root.ancestors().find_map(|dir| {
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
    })?;
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

pub(super) fn ensure_graphtrail_ignored(root: &Path) -> Result<()> {
    let gitignore = root.join(".gitignore");
    if gitignore_covers_graphtrail(root, &gitignore)? {
        return Ok(());
    }

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
    writeln!(file, ".graphtrail/")?;
    println!("updated {} to ignore .graphtrail/", gitignore.display());
    Ok(())
}

fn gitignore_covers_graphtrail(root: &Path, gitignore: &Path) -> Result<bool> {
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
    Ok(matcher.matched(root.join(".graphtrail"), true).is_ignore())
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
