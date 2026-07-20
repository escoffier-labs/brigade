fn main() {
    println!("cargo:rerun-if-env-changed=CARGO_FEATURE_MISELEDGER");

    if std::env::var_os("CARGO_FEATURE_MISELEDGER").is_some() {
        println!(
            "cargo:warning=GraphTrail's direct MiseLedger adapter is deprecated. Use `brigade code sync`, `brigade code context`, `brigade code impact`, `brigade evidence crawl`, `brigade evidence search`, and `brigade evidence doctor` instead. The adapter remains functional for at least two minor GraphTrail releases or 90 days after the first GraphTrail release containing this deprecation, whichever is longer. It will not be removed before that compatibility policy is satisfied."
        );
    }
}
