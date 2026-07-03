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
pub use graph::{
    DEFAULT_IMPACT_DEPTH, file_neighbors, graph_edges, graph_edges_with_depth, impact_edges,
    normalize_depth,
};
pub use search::{search_symbols, search_symbols_with_path};
pub use stats::stats;
