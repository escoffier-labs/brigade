//! Read-only query layer over the graph database.

pub mod blend;
pub mod context;
pub mod graph;
#[cfg(test)]
mod graph_tests;
pub mod search;
pub mod stats;

pub use blend::{ExternalHit, blend};
pub use context::{build_context_pack, render_markdown};
pub use graph::{file_neighbors, graph_edges};
pub use search::{search_symbols, search_symbols_with_path};
pub use stats::stats;
