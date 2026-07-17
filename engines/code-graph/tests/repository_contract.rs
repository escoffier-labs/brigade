use std::{collections::HashSet, fs, path::Path};

use serde_json::Value;

fn repository_file(path: impl AsRef<Path>) -> String {
    fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join(path))
        .expect("repository contract file should be readable")
}

fn assert_supported_read_only_command(command: &Value, context: &str) {
    let command = command
        .as_array()
        .filter(|command| !command.is_empty())
        .unwrap_or_else(|| panic!("{context} must be a non-empty command"));
    let command: Vec<_> = command
        .iter()
        .map(|part| {
            part.as_str()
                .unwrap_or_else(|| panic!("{context} entries must be strings"))
        })
        .collect();

    let supported = match command.as_slice() {
        ["graphtrail", "--version"] => true,
        ["graphtrail", subcommand, ..] => matches!(
            *subcommand,
            "search"
                | "neighbors"
                | "callers"
                | "callees"
                | "impact"
                | "context"
                | "dead-code"
                | "cycles"
                | "affected"
                | "evaluate"
                | "explain"
                | "stats"
                | "doctor"
                | "diff"
                | "blend"
                | "links"
        ),
        ["graphtrail-mcp"] | ["graphtrail-mcp", "--help" | "--version"] => true,
        ["brigade", "status", ..] | ["brigade", "work", "brief", ..] => true,
        _ => false,
    };
    assert!(
        supported,
        "{context} must resolve to an explicitly supported read-only GraphTrail or Brigade command; found {}",
        command.join(" ")
    );
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
fn ci_covers_supported_platform_and_feature_configurations() {
    let ci = repository_file(".github/workflows/ci.yml");
    for required in [
        "name: build-and-test",
        "name: Windows (stable, default features)",
        "runs-on: windows-latest",
        "cargo test --locked",
        "name: Feature configuration (${{ matrix.name }})",
        "cargo check --locked ${{ matrix.cargo_args }}",
        "name: no-default",
        "cargo_args: --no-default-features",
        "name: default",
        "name: watch-only",
        "cargo_args: --no-default-features --features watch",
        "name: codesearch-only",
        "cargo_args: --no-default-features --features codesearch",
        "name: miseledger-only",
        "cargo_args: --no-default-features --features miseledger",
        "name: all-features",
        "cargo_args: --all-features",
    ] {
        assert!(
            ci.contains(required),
            "CI must preserve the supported platform and feature check: {required}"
        );
    }

    let readme = repository_file("README.md");
    for required in [
        "Release-supported platforms are Linux, macOS, and Windows.",
        "Required CI exercises Linux and Windows",
        "`--no-default-features`",
        "`--no-default-features --features watch`",
        "`--no-default-features --features codesearch`",
        "`--no-default-features --features miseledger`",
        "`--all-features`",
    ] {
        assert!(
            readme.contains(required),
            "README must document the release support contract: {required}"
        );
    }
}

#[test]
fn agent_startup_requires_skill_selection_before_brigade_commands() {
    let agents = repository_file("AGENTS.md");
    let first_raw_command = agents
        .find("brigade work brief --target .")
        .expect("AGENTS.md must preserve the Brigade brief command");

    for skill in ["`using-skillet`", "`brigade-work`"] {
        let skill_position = agents.find(skill).unwrap_or_else(|| {
            panic!("AGENTS.md must require agents to invoke {skill} at session start")
        });
        assert!(
            skill_position < first_raw_command,
            "AGENTS.md must require {skill} before the first raw Brigade command"
        );
    }
}

#[test]
fn station_manifest_preserves_the_read_only_startup_contract() {
    let manifest: Value = serde_json::from_str(&repository_file("station.json"))
        .expect("station.json must contain valid JSON");
    let manifest = manifest
        .as_object()
        .expect("station.json must contain a JSON object");

    assert_eq!(
        manifest.get("schema").and_then(Value::as_str),
        Some("brigade.station.v1"),
        "station.json must declare the supported station schema"
    );
    for field in ["name", "station", "summary", "lifecycle"] {
        assert!(
            manifest
                .get(field)
                .and_then(Value::as_str)
                .is_some_and(|value| !value.trim().is_empty()),
            "station.json must provide a non-empty {field}"
        );
    }

    let tools = manifest
        .get("tools")
        .and_then(Value::as_array)
        .filter(|tools| !tools.is_empty())
        .expect("station.json must declare at least one tool");
    let mut tool_names = HashSet::new();
    for tool in tools {
        let tool = tool
            .as_object()
            .expect("each station tool must be a JSON object");
        let name = tool
            .get("name")
            .and_then(Value::as_str)
            .filter(|name| !name.trim().is_empty())
            .expect("each station tool must have a non-empty name");
        assert!(
            tool_names.insert(name),
            "station tool identifiers must be unique; duplicate: {name}"
        );
        for field in ["kind", "command", "summary"] {
            assert!(
                tool.get(field)
                    .and_then(Value::as_str)
                    .is_some_and(|value| !value.trim().is_empty()),
                "station tool {name} must provide a non-empty {field}"
            );
        }
        let executable = tool
            .get("command")
            .and_then(Value::as_str)
            .expect("station tool command was validated above");
        assert!(
            matches!(executable, "graphtrail" | "graphtrail-mcp" | "brigade"),
            "station tool {name} must use a supported GraphTrail or Brigade entry point; found {executable}"
        );

        let surfaces = tool
            .get("surfaces")
            .and_then(Value::as_array)
            .filter(|surfaces| !surfaces.is_empty())
            .unwrap_or_else(|| panic!("station tool {name} must declare at least one surface"));
        let mut surface_kinds = HashSet::new();
        for surface in surfaces {
            let surface = surface
                .as_object()
                .unwrap_or_else(|| panic!("station tool {name} surfaces must be JSON objects"));
            let kind = surface
                .get("kind")
                .and_then(Value::as_str)
                .filter(|kind| !kind.trim().is_empty())
                .unwrap_or_else(|| panic!("station tool {name} surfaces need a non-empty kind"));
            assert!(
                surface_kinds.insert(kind),
                "station surface identifiers must be unique within {name}; duplicate: {kind}"
            );

            assert_supported_read_only_command(
                surface
                    .get("command")
                    .unwrap_or_else(|| panic!("station surface {name}/{kind} needs a command")),
                &format!("station surface {name}/{kind} command"),
            );
            if let Some(probe) = surface.get("probe") {
                assert_supported_read_only_command(
                    probe,
                    &format!("station surface {name}/{kind} probe"),
                );
            }
            assert_eq!(
                surface.get("read_only").and_then(Value::as_bool),
                Some(true),
                "station surface {name}/{kind} must remain explicitly read-only"
            );
            assert!(
                surface
                    .get("timeout_seconds")
                    .and_then(Value::as_u64)
                    .is_some_and(|timeout| (1..=30).contains(&timeout)),
                "station surface {name}/{kind} needs an explicit timeout between 1 and 30 seconds"
            );
            if kind == "brief-markdown" {
                assert!(
                    surface
                        .get("max_chars")
                        .and_then(Value::as_u64)
                        .is_some_and(|max_chars| (1..=4000).contains(&max_chars)),
                    "station surface {name}/{kind} needs an explicit output bound of at most 4000 characters"
                );
            }
        }
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
