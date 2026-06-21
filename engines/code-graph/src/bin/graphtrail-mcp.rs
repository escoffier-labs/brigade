//! GraphTrail MCP server binary: a read-only stdio JSON-RPC server over graph databases.
//!
//! The default database comes from `--db <path>`, `--db=<path>`, the `GRAPHTRAIL_DB` env var, or
//! `.graphtrail/graphtrail.db` in the working directory. Individual tool calls may override it with
//! a `repo`/`db` argument, so one server can answer for any indexed repo. The database is opened
//! lazily per call, so the server starts even if the default db does not exist yet.

use std::io;
use std::path::PathBuf;

use anyhow::Result;

use graphtrail::mcp::serve;

fn main() -> Result<()> {
    let default_db = resolve_db();
    let stdin = io::stdin();
    let stdout = io::stdout();
    serve(&default_db, stdin.lock(), stdout.lock())
}

fn resolve_db() -> PathBuf {
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--db" {
            if let Some(path) = args.next() {
                return PathBuf::from(path);
            }
        } else if let Some(path) = arg.strip_prefix("--db=") {
            return PathBuf::from(path);
        }
    }
    if let Ok(path) = std::env::var("GRAPHTRAIL_DB") {
        return PathBuf::from(path);
    }
    PathBuf::from(".graphtrail/graphtrail.db")
}
