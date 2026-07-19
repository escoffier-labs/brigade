//! Optional, feature-gated integrations with external systems. The default build compiles none of
//! these, keeping the core sidecar free of network and cross-tool dependencies.

#[cfg(feature = "codesearch")]
pub mod codesearch;

#[cfg(feature = "miseledger")]
pub mod miseledger;
