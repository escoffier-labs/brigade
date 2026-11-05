//! Code graph MCP server binary with read-only query connections and one opt-in `refresh: true`
//! incremental sync writer. Supported tools wait up to 10 seconds for refresh, then fail open with
//! a `refresh_error` note. A timed-out worker may finish concurrently with the query.
//!
//! The default database comes from `--db <path>`, `--db=<path>`, the `BRIGADE_CODE_GRAPH_DB` env
//! var (the legacy variable still works with a warning), or the working directory's index, as
//! resolved by `store::resolve_db`. Individual tool calls may override it with a `repo`/`db`
//! argument, so one server can answer for any indexed repo. The database is opened lazily per
//! call, so the server starts even if the default db does not exist yet.
//!
//! The entry point name (`argv[0]`) selects the identity reported as `serverInfo.name`.

use std::io;
use std::path::{Path, PathBuf};

use anyhow::Result;

use graphtrail::cli::Identity;
use graphtrail::mcp::serve_as;
use graphtrail::store::db_path;

pub(crate) fn main() -> Result<()> {
    let identity = Identity::mcp(std::env::args_os().next().as_deref());
    identity.warn_if_legacy();
    let default_db = db_path(explicit_db(), Path::new(""))?;
    let stdin = io::stdin();
    let stdout = io::stdout();
    serve_as(
        identity.server_name,
        &default_db,
        stdin.lock(),
        stdout.lock(),
    )
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
