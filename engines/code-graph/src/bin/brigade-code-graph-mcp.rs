//! Code graph MCP server binary with read-only query connections and one opt-in `refresh: true`
//! incremental sync writer. Supported tools wait up to 10 seconds for refresh, then fail open with
//! a `refresh_error` note. A timed-out worker may finish concurrently with the query.
//!
//! The default database comes from `--db <path>`, `--db=<path>`, the `BRIGADE_CODE_GRAPH_DB` env
//! var, or the working directory's canonical index, as resolved by `store::resolve_db`.
//! The retired variable is ignored and the old index stays on disk outside default resolution.
//! Individual tool calls may override the database with a `repo`/`db` argument. It is opened lazily per
//! call, so the server starts even if the default db does not exist yet.

use std::io;
use std::path::{Path, PathBuf};

use anyhow::Result;

use brigade_code_graph::mcp::serve;
use brigade_code_graph::store::db_path;

fn main() -> Result<()> {
    let default_db = db_path(explicit_db(), Path::new(""))?;
    let stdin = io::stdin();
    let stdout = io::stdout();
    serve(&default_db, stdin.lock(), stdout.lock())
}

fn explicit_db() -> Option<PathBuf> {
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--db" {
            if let Some(path) = args.next() {
                return Some(PathBuf::from(path));
            }
        } else if let Some(path) = arg.strip_prefix("--db=") {
            return Some(PathBuf::from(path));
        }
    }
    None
}
