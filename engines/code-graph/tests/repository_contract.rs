use std::{fs, path::Path};

fn repository_file(path: impl AsRef<Path>) -> String {
    fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join(path))
        .expect("repository contract file should be readable")
}

#[test]
fn personalized_ranking_has_a_checked_in_benchmark_contract() {
    for path in [
        "benchmarks/context-ranking/corpus.json",
        "tests/context_ranking_benchmark.rs",
        "docs/context-ranking-benchmark.md",
    ] {
        assert!(
            Path::new(env!("CARGO_MANIFEST_DIR")).join(path).is_file(),
            "personalized ranking benchmark contract must include {path}"
        );
    }
}

#[test]
fn sync_orchestration_is_split_into_focused_modules() {
    for module in ["walk", "persist", "repo_policy", "resolve"] {
        let path = format!("src/store/{module}.rs");
        assert!(
            Path::new(env!("CARGO_MANIFEST_DIR")).join(&path).is_file(),
            "sync responsibility must live in {path}"
        );
    }

    let sync = repository_file("src/store/sync.rs");
    assert!(
        sync.lines().count() <= 400,
        "sync.rs must remain a small orchestration facade; found {} lines",
        sync.lines().count()
    );
}

#[test]
fn docker_context_excludes_private_state() {
    let dockerfile = repository_file("Dockerfile");
    assert!(
        !dockerfile.lines().any(|line| line.trim() == "COPY . ."),
        "Dockerfile must copy only the files needed to build"
    );

    let dockerignore = repository_file(".dockerignore");
    let rules: Vec<_> = dockerignore
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .collect();
    assert_eq!(
        rules,
        [
            "**",
            "!Dockerfile",
            "!.dockerignore",
            "!Cargo.toml",
            "!Cargo.lock",
            "!src/",
            "!src/**",
        ],
        ".dockerignore must deny all context inputs except the files copied by Dockerfile"
    );
}

#[test]
fn supported_toolchain_and_agent_workflow_are_documented() {
    let ci = repository_file(".github/workflows/ci.yml");
    assert!(
        ci.contains("uses: dtolnay/rust-toolchain@1.85.0"),
        "CI must install the exact Rust 1.85.0 toolchain"
    );
    assert!(
        !ci.lines().any(|line| line.trim().starts_with("toolchain:")),
        "the MSRV action ref must not be overridden by a toolchain input"
    );
    assert!(
        !ci.lines().any(|line| {
            line.trim()
                .strip_prefix("- ")
                .map(|pattern| pattern.trim_matches(['\'', '"']))
                .is_some_and(|pattern| matches!(pattern, "*.md" | "**/*.md"))
        }),
        "CI must run when root Markdown contracts change"
    );
    assert!(
        ci.contains("cargo check --locked --all-features"),
        "the Rust 1.85 CI job must check the locked all-features build"
    );

    let readme = repository_file("README.md");
    assert!(
        readme.contains("Rust 1.85 or newer"),
        "README must state the supported Rust version"
    );
    assert!(
        readme.contains("`refresh: true` starts an incremental graph-index write"),
        "README must name when the opt-in MCP write starts"
    );
    assert!(
        readme.contains("waits up to 10 seconds before opening the query read-only"),
        "README must state the refresh wait limit"
    );
    assert!(
        readme.contains("the query proceeds and appends a `refresh_error` note"),
        "README must document fail-open refresh errors"
    );
    assert!(
        readme.contains("A timed-out worker may finish concurrently"),
        "README must document the timed-out worker overlap"
    );
    assert!(
        readme.contains("Without `refresh`, query tools do not write the graph"),
        "README must document the default no-write behavior"
    );

    let agents = repository_file("AGENTS.md");
    for required in [
        "brigade work brief --target .",
        "brigade work verify run --target . --command",
        "brigade outcome capture",
        ".claude/memory-handoffs/",
    ] {
        assert!(
            agents.contains(required),
            "AGENTS.md must document the Brigade workflow step: {required}"
        );
    }
    for required in [
        "`refresh: true` starts an incremental graph-index write",
        "waits up to 10 seconds before opening the query read-only",
        "the query proceeds and appends a `refresh_error` note",
        "A timed-out worker may finish concurrently",
        "Without `refresh`, query tools do not write the graph",
    ] {
        assert!(
            agents.contains(required),
            "AGENTS.md must document the refresh contract: {required}"
        );
    }
}

#[test]
fn mcp_read_only_contract_distinguishes_opt_in_refresh_writes() {
    let security = repository_file("SECURITY.md");
    for required in [
        "Query connections must always use `SQLITE_OPEN_READ_ONLY`",
        "writes without `refresh: true`",
        "Expected graph-index writes from `refresh: true` are not vulnerabilities",
    ] {
        assert!(
            security.contains(required),
            "SECURITY.md must distinguish unauthorized writes from supported refresh: {required}"
        );
    }

    let contributing = repository_file("CONTRIBUTING.md");
    for required in [
        "query connections are read-only",
        "`refresh: true` incremental sync",
        "waits up to 10 seconds",
        "fails open with a `refresh_error` note",
        "timed-out worker may finish concurrently",
    ] {
        assert!(
            contributing.contains(required),
            "CONTRIBUTING.md must state the MCP refresh boundary: {required}"
        );
    }

    let manifest = repository_file("Cargo.toml");
    for required in [
        "Local code-graph CLI and MCP server for coding agents",
        "tree-sitter callers, callees, impact, context",
        "read-only queries, opt-in index refresh",
        "no network in the default build",
    ] {
        assert!(
            manifest.contains(required),
            "package description must preserve the GraphTrail contract: {required}"
        );
    }

    for path in ["src/bin/graphtrail-mcp.rs", "Dockerfile", "tests/mcp.rs"] {
        let content = repository_file(path);
        assert!(
            content.contains("read-only query connections"),
            "{path} must describe read-only MCP query connections"
        );
        assert!(
            content.contains("opt-in `refresh: true`"),
            "{path} must describe the opt-in refresh writer"
        );
    }
}
