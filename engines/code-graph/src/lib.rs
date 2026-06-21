//! GraphTrail: a local code-graph sidecar. Library crate exposing the extraction, storage,
//! and query layers so the CLI binary, tests, and (later) the MCP server share one API.

pub mod cli;
pub mod extractors;
pub mod model;
pub mod query;
pub mod store;
