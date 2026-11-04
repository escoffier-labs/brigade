//! Storage infrastructure. Route ownership remains with Python until S5.
pub mod maintenance;
pub mod read;
pub mod schema;
pub mod writer;

// S9 route and observation infrastructure.
pub mod modes;
pub mod shadow;
pub mod telemetry;

// S5 and S7 native route domains.
pub mod claims;
pub mod wire;
