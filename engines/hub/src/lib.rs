//! Storage infrastructure. Route ownership remains with Python until S5.
pub mod maintenance;
pub mod read;
pub mod schema;

// S9 route and observation infrastructure.
pub mod modes;
pub mod shadow;
pub mod telemetry;
