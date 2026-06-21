//! GraphTrail MCP server binary: a read-only stdio JSON-RPC server over a synced graph db.
//!
//! Pick the database via `--db <path>`, `--db=<path>`, the `GRAPHTRAIL_DB` env var, or the
//! default `.graphtrail/graphtrail.db` in the working directory.

use std::io;
use std::path::PathBuf;

use anyhow::{Context, Result};

use graphtrail::mcp::serve;
use graphtrail::store::open_read_only;

fn main() -> Result<()> {
    let db = resolve_db();
    let conn =
        open_read_only(&db).with_context(|| format!("failed to open graph db {}", db.display()))?;
    let stdin = io::stdin();
    let stdout = io::stdout();
    serve(&conn, stdin.lock(), stdout.lock())
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
