//! Read-only query layer over the graph database.

pub mod affected;
pub mod blend;
pub mod context;
pub mod diff;
pub mod doctor;
pub mod export;
pub mod graph;
#[cfg(test)]
mod graph_tests;
pub mod health;
pub mod search;
pub mod stats;

pub use affected::{AffectedReport, DEFAULT_AFFECTED_DEPTH, affected};
pub use blend::{ExternalHit, blend};
pub use context::{build_context_pack, build_context_pack_from_entry_points, render_markdown};
pub use diff::diff_graphs;
pub use doctor::{DoctorReport, doctor, missing_db_report};
pub use export::{ExportFormat, ExportScope, export_graph};
pub use graph::{
    DEFAULT_IMPACT_DEPTH, file_neighbors, graph_edges, graph_edges_with_depth, impact_edges,
    normalize_depth,
};
pub use health::{CycleReport, DeadCodeReport, cycles, dead_code};
pub use search::{search_symbols, search_symbols_with_path};
pub use stats::stats;
