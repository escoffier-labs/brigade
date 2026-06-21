//! Read-only query layer over the graph database.

pub mod blend;
pub mod context;
pub mod graph;
pub mod search;
pub mod stats;

pub use blend::{ExternalHit, blend};
pub use context::{build_context_pack, render_markdown};
pub use graph::graph_edges;
pub use search::search_symbols;
pub use stats::stats;
