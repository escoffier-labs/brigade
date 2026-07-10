//! Storage layer: schema, connection lifecycle, and the sync write path.

pub mod db;
pub mod lock;
pub mod meta;
mod persist;
mod repo_policy;
mod resolve;
pub mod schema;
pub mod sync;
mod walk;

pub use db::{db_path, open_db, open_default, open_default_read_only, open_read_only};
pub(crate) use repo_policy::{current_git_branch, guard_unsafe_root};
pub use schema::{SCHEMA_VERSION, init_schema};
pub use sync::{pending_changes, sync_repo, sync_repo_force};
