//! Storage layer: schema, connection lifecycle, and the sync write path.

pub mod db;
pub mod lock;
pub mod meta;
pub mod schema;
pub mod sync;

pub use db::{db_path, open_db, open_default, open_default_read_only, open_read_only};
pub use schema::{SCHEMA_VERSION, init_schema};
pub(crate) use sync::{current_git_branch, guard_unsafe_root};
pub use sync::{pending_changes, sync_repo, sync_repo_force};
