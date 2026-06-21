//! Storage layer: schema, connection lifecycle, and the sync write path.

pub mod db;
pub mod schema;
pub mod sync;

pub use db::{db_path, open_db, open_default};
pub use schema::init_schema;
pub use sync::sync_repo;
