//! Command-line surface and output formatting.

use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

use crate::model::{ContextPack, Direction, EdgeRow, GraphDiff, SearchRow};
#[cfg(feature = "codesearch")]
use crate::query::build_context_pack_from_entry_points;
use crate::query::{
    DEFAULT_AFFECTED_DEPTH, DEFAULT_IMPACT_DEPTH, ExportFormat, ExportScope, affected,
    build_context_pack,
    context::{edge_location, symbol_location},
    cycles, dead_code, diff_graphs, doctor, export_graph, file_neighbors, graph_edges_with_depth,
    impact_edges, missing_db_report, normalize_depth, personalize_context_pack, render_markdown,
    render_markdown_budgeted, search_symbols_with_path, stats,
};
use crate::store::{
    db_path, init_schema, open_db, open_default_read_only, open_read_only, sync_repo_force,
};

#[derive(Parser)]
#[command(version, about)]
pub struct Cli {
    #[arg(long, global = true, value_name = "PATH")]
    db: Option<PathBuf>,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Init {
        #[arg(default_value = ".")]
        root: PathBuf,
    },
    Sync {
        #[arg(default_value = ".")]
        root: PathBuf,
        /// Rebuild every file even if nothing changed.
        #[arg(long)]
        force: bool,
    },
    Search {
        query: String,
        #[arg(long, value_name = "PATH")]
        path: Option<String>,
        #[arg(long, default_value_t = 20)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    Neighbors {
        path: String,
        #[arg(long)]
        json: bool,
    },
    Callers {
        symbol: String,
        #[arg(long, default_value_t = DEFAULT_IMPACT_DEPTH)]
        depth: usize,
        #[arg(long)]
        json: bool,
    },
    Callees {
        symbol: String,
        #[arg(long, default_value_t = DEFAULT_IMPACT_DEPTH)]
        depth: usize,
        #[arg(long)]
        json: bool,
    },
    Impact {
        symbol: String,
        #[arg(long, default_value_t = DEFAULT_IMPACT_DEPTH)]
        depth: usize,
        #[arg(long)]
        json: bool,
    },
    Context {
        task: String,
        #[arg(long, default_value_t = 12)]
        limit: usize,
        #[arg(long)]
        json: bool,
        /// Render the pack as Brigade-friendly markdown.
        #[arg(long)]
        markdown: bool,
        /// Rank files from task-specific seeds through the resolved call graph.
        #[arg(long)]
        personalized: bool,
        /// Maximum markdown characters when --personalized is enabled.
        #[arg(long, default_value_t = 4000)]
        max_chars: usize,
        /// Use Code Search semantic hits as entry points, then rank them with graph centrality.
        #[cfg(feature = "codesearch")]
        #[arg(long)]
        blend_code_search: bool,
        #[cfg(feature = "codesearch")]
        #[arg(long, default_value_t = 0.6)]
        embed_weight: f64,
        #[cfg(feature = "codesearch")]
        #[arg(long, default_value_t = 0.4)]
        graph_weight: f64,
        /// Append read-only MiseLedger evidence links for the task and entry points.
        #[cfg(feature = "miseledger")]
        #[arg(long)]
        evidence: bool,
    },
    /// Callables with no incoming call edges (a candidate list, not proof of dead code).
    DeadCode {
        #[arg(long, default_value_t = 100)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    /// File-level dependency cycles from cross-file call edges.
    Cycles {
        #[arg(long)]
        json: bool,
    },
    /// Tests statically attributed to the given changed files via incoming call edges.
    Affected {
        /// Changed files, repo-relative (e.g. from `git diff --name-only`).
        #[arg(required = true)]
        files: Vec<String>,
        #[arg(long, default_value_t = DEFAULT_AFFECTED_DEPTH)]
        depth: usize,
        #[arg(long)]
        json: bool,
    },
    /// Dry-run extraction: print what a sync of the path WOULD store, writing nothing.
    Evaluate {
        #[arg(default_value = ".")]
        path: PathBuf,
        #[arg(long)]
        json: bool,
    },
    /// Explain how call edges between two symbols resolved (path, confidence, import).
    Explain {
        /// Calling symbol, by name or qualified name.
        source: String,
        /// Called symbol name, as written at the call site.
        target: String,
        #[arg(long)]
        json: bool,
    },
    /// Export the graph for visualization: Graphviz dot, GraphML, or JSON Lines.
    Export {
        #[arg(long, value_enum, default_value_t = ExportFormat::Dot)]
        format: ExportFormat,
        #[arg(long, value_enum, default_value_t = ExportScope::Files)]
        scope: ExportScope,
        /// Write to a file instead of stdout.
        #[arg(long, value_name = "PATH")]
        out: Option<PathBuf>,
    },
    /// Watch a repository and run an incremental sync when source files change.
    #[cfg(feature = "watch")]
    Watch {
        #[arg(default_value = ".")]
        root: PathBuf,
        /// Quiet period after the last change before a sync runs.
        #[arg(long, default_value_t = 400)]
        debounce_ms: u64,
    },
    Stats {
        #[arg(long)]
        json: bool,
    },
    Doctor {
        #[arg(default_value = ".")]
        root: PathBuf,
        #[arg(long)]
        json: bool,
    },
    /// Diff two indexed graph DBs (before -> after) into added/removed/changed
    /// nodes and edges. Produce the DBs with `graphtrail --db X sync <root>`.
    Diff {
        #[arg(long, value_name = "PATH")]
        before: PathBuf,
        #[arg(long, value_name = "PATH")]
        after: PathBuf,
        #[arg(long)]
        json: bool,
    },
    /// Blend Code Search embedding hits with graph centrality (feature: codesearch).
    #[cfg(feature = "codesearch")]
    Blend {
        query: String,
        #[arg(long, default_value_t = 10)]
        limit: usize,
        #[arg(long, default_value_t = 0.6)]
        embed_weight: f64,
        #[arg(long, default_value_t = 0.4)]
        graph_weight: f64,
        #[arg(long)]
        json: bool,
    },
    /// Surface MiseLedger evidence items mentioning a symbol/term (feature: miseledger).
    #[cfg(feature = "miseledger")]
    Links {
        term: String,
        #[arg(long, default_value_t = 10)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
}

pub fn run(cli: Cli) -> Result<()> {
    match cli.command {
        Command::Init { root } => {
            let db_path = db_path(cli.db, &root);
            let conn = open_db(&db_path)?;
            init_schema(&conn)?;
            println!("initialized {}", db_path.display());
        }
        Command::Sync { root, force } => {
            let canonical_root = root.canonicalize().unwrap_or_else(|_| root.clone());
            crate::store::guard_unsafe_root(&canonical_root)?;
            let db_path = db_path(cli.db, &root);
            let conn = open_db(&db_path)?;
            init_schema(&conn)?;
            let summary = sync_repo_force(&conn, &root, force)?;
            if summary.unchanged {
                println!(
                    "unchanged: files={} symbols={} edges={} imports={} db={}",
                    summary.files,
                    summary.symbols,
                    summary.calls,
                    summary.imports,
                    db_path.display()
                );
            } else {
                println!(
                    "indexed files={} symbols={} calls={} imports={} deleted={} db={}",
                    summary.files,
                    summary.symbols,
                    summary.calls,
                    summary.imports,
                    summary.deleted,
                    db_path.display()
                );
            }
        }
        Command::Search {
            query,
            path,
            limit,
            json,
        } => {
            let conn = open_default_read_only(cli.db)?;
            let rows = search_symbols_with_path(&conn, &query, path.as_deref(), limit)?;
            print_json_or_symbols(json, &rows)?;
        }
        Command::Neighbors { path, json } => {
            let conn = open_default_read_only(cli.db)?;
            let rows = file_neighbors(&conn, &path)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&rows)?);
            } else {
                for row in &rows {
                    println!(
                        "{} incoming={} outgoing={}",
                        row.file_path, row.incoming_edges, row.outgoing_edges
                    );
                }
            }
        }
        Command::Callers {
            symbol,
            depth,
            json,
        } => {
            let conn = open_default_read_only(cli.db)?;
            let edges = graph_edges_with_depth(
                &conn,
                &symbol,
                Direction::Incoming,
                normalize_depth(depth),
            )?;
            print_json_or_edges(json, &edges)?;
        }
        Command::Callees {
            symbol,
            depth,
            json,
        } => {
            let conn = open_default_read_only(cli.db)?;
            let edges = graph_edges_with_depth(
                &conn,
                &symbol,
                Direction::Outgoing,
                normalize_depth(depth),
            )?;
            print_json_or_edges(json, &edges)?;
        }
        Command::Impact {
            symbol,
            depth,
            json,
        } => {
            let conn = open_default_read_only(cli.db)?;
            let edges = impact_edges(&conn, &symbol, normalize_depth(depth))?;
            print_json_or_edges(json, &edges)?;
        }
        Command::Context {
            task,
            limit,
            json,
            markdown,
            personalized,
            max_chars,
            #[cfg(feature = "codesearch")]
            blend_code_search,
            #[cfg(feature = "codesearch")]
            embed_weight,
            #[cfg(feature = "codesearch")]
            graph_weight,
            #[cfg(feature = "miseledger")]
            evidence,
        } => {
            #[cfg(feature = "miseledger")]
            if evidence {
                crate::adapters::miseledger::emit_deprecation_warning();
            }
            let db_path = default_query_db_path(cli.db.clone());
            let conn = open_read_only(&db_path)?;
            #[cfg(feature = "codesearch")]
            let pack = if blend_code_search {
                let repo_root = repo_root_for_codesearch(&db_path);
                let client = crate::adapters::codesearch::CodeSearchClient::from_env_for_repo(
                    repo_root.as_deref(),
                );
                let hits = client.search(&task, limit.max(20))?;
                let rows = crate::query::blend(&conn, &hits, embed_weight, graph_weight, limit)?;
                let entry_points = rows.into_iter().map(|row| row.symbol).collect();
                build_context_pack_from_entry_points(&conn, task.clone(), entry_points)?
            } else {
                build_context_pack(&conn, task.clone(), limit)?
            };
            #[cfg(not(feature = "codesearch"))]
            let pack = build_context_pack(&conn, task.clone(), limit)?;
            let mut pack = pack;
            if personalized {
                personalize_context_pack(&conn, &mut pack)?;
            }
            if json {
                println!("{}", serde_json::to_string_pretty(&pack)?);
            } else if markdown {
                let output = if personalized {
                    render_markdown_budgeted(&pack, max_chars)
                } else {
                    render_markdown(&pack)
                };
                #[cfg(feature = "miseledger")]
                let mut output = output;
                #[cfg(feature = "miseledger")]
                if evidence {
                    let evidence = render_evidence_links(&task, &pack, limit)?;
                    if personalized {
                        append_markdown_with_budget(&mut output, &evidence, max_chars);
                    } else {
                        output.push_str(&evidence);
                    }
                }
                print!("{output}");
            } else {
                print!("{}", render_context(&pack));
                #[cfg(feature = "miseledger")]
                if evidence {
                    print!("{}", render_evidence_links(&task, &pack, limit)?);
                }
            }
        }
        Command::DeadCode { limit, json } => {
            let conn = open_default_read_only(cli.db)?;
            let report = dead_code(&conn, limit)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&report)?);
            } else {
                println!(
                    "uncalled callables: {} (showing {})",
                    report.total,
                    report.symbols.len()
                );
                println!("note: {}", report.attribution);
                for symbol in &report.symbols {
                    println!(
                        "{} {} {} {}:{} {} - {}",
                        symbol.confidence,
                        symbol.kind,
                        symbol.qualified_name,
                        symbol.file_path,
                        symbol.start_line,
                        symbol.signature,
                        symbol.reason
                    );
                }
            }
        }
        Command::Cycles { json } => {
            let conn = open_default_read_only(cli.db)?;
            let report = cycles(&conn)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&report)?);
            } else {
                println!(
                    "cycle groups: {}{}",
                    report.total_groups,
                    if report.truncated { " (truncated)" } else { "" }
                );
                for group in &report.groups {
                    println!("- {}", group.join(" <-> "));
                }
            }
        }
        Command::Affected { files, depth, json } => {
            let conn = open_default_read_only(cli.db)?;
            let report = affected(&conn, &files, depth)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&report)?);
            } else {
                println!(
                    "changed: {} known, {} missing (depth {})",
                    report.changed_files.len(),
                    report.missing_files.len(),
                    report.depth
                );
                for missing in &report.missing_files {
                    println!("missing: {missing}");
                }
                println!("note: {}", report.attribution);
                println!("affected tests: {}", report.affected_tests.len());
                for row in &report.affected_tests {
                    println!(
                        "- {} hops={} via {}",
                        row.file_path,
                        row.min_hops,
                        row.via.join(", ")
                    );
                }
                println!("impacted files: {}", report.impacted_files.len());
                for row in &report.impacted_files {
                    println!("- {} hops={}", row.file_path, row.min_hops);
                }
            }
        }
        Command::Evaluate { path, json } => {
            let report = crate::evaluate::evaluate_path(&path)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&report)?);
            } else {
                println!(
                    "evaluated files={} symbols={} imports={} calls={} (no database touched)",
                    report.totals.files,
                    report.totals.symbols,
                    report.totals.imports,
                    report.totals.calls
                );
                for file in &report.files {
                    println!(
                        "{} ({}): symbols={} imports={} calls={}",
                        file.path,
                        file.language,
                        file.symbols.len(),
                        file.imports.len(),
                        file.calls.len()
                    );
                }
            }
        }
        Command::Explain {
            source,
            target,
            json,
        } => {
            let conn = open_default_read_only(cli.db)?;
            let rows = crate::store::explain_calls(&conn, &source, &target)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&rows)?);
            } else if rows.is_empty() {
                println!("no calls from '{source}' to '{target}' in the index");
            } else {
                for row in &rows {
                    println!(
                        "{} calls {} at {}:{} [{}]",
                        row.source_qualified_name,
                        row.target_name,
                        row.source_file,
                        row.line,
                        row.kind
                    );
                    println!("  resolution: {}", row.resolution);
                    if let Some(import) = &row.matched_import {
                        println!("  via import: {} (line {})", import.module, import.line);
                    }
                    for target in &row.targets {
                        println!(
                            "  -> {} {}:{} confidence={}",
                            target.qualified_name,
                            target.file_path,
                            target.start_line,
                            target.confidence
                        );
                    }
                }
            }
        }
        Command::Export { format, scope, out } => {
            let conn = open_default_read_only(cli.db)?;
            let text = export_graph(&conn, format, scope)?;
            match out {
                Some(path) => {
                    std::fs::write(&path, text)?;
                    println!("wrote {}", path.display());
                }
                None => print!("{text}"),
            }
        }
        #[cfg(feature = "watch")]
        Command::Watch { root, debounce_ms } => {
            crate::watch::watch(cli.db, &root, std::time::Duration::from_millis(debounce_ms))?;
        }
        Command::Stats { json } => {
            let conn = open_default_read_only(cli.db)?;
            let stats = stats(&conn)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&stats)?);
            } else {
                println!("schema_version: {}", stats.schema_version);
                println!("files: {}", stats.files);
                println!("symbols: {}", stats.symbols);
                println!("edges: {}", stats.edges);
                println!("imports: {}", stats.imports);
                if let Some(synced_at) = stats.synced_at {
                    println!("synced_at: {synced_at}");
                }
                if let Some(tool_version) = stats.tool_version {
                    println!("tool_version: {tool_version}");
                }
                for (language, files) in stats.language_files {
                    println!("language_files.{language}: {files}");
                }
            }
        }
        Command::Doctor { root, json } => {
            let db_path = db_path(cli.db, &root);
            if !db_path.exists() {
                let report = missing_db_report(&root, &db_path);
                print_doctor_report(&report, json)?;
                std::process::exit(report.exit_code());
            }
            let conn = open_read_only(&db_path)?;
            let report = doctor(&conn, &root, &db_path)?;
            print_doctor_report(&report, json)?;
            std::process::exit(report.exit_code());
        }
        Command::Diff {
            before,
            after,
            json,
        } => {
            let before_conn = open_read_only(&before)?;
            let after_conn = open_read_only(&after)?;
            let diff = diff_graphs(&before_conn, &after_conn)?;
            if json {
                // Compact, not pretty: this is what Brigade embeds in a receipt.
                println!("{}", serde_json::to_string(&diff)?);
            } else {
                print!("{}", render_diff(&diff));
            }
        }
        #[cfg(feature = "codesearch")]
        Command::Blend {
            query,
            limit,
            embed_weight,
            graph_weight,
            json,
        } => {
            let db_path = default_query_db_path(cli.db);
            let conn = open_read_only(&db_path)?;
            let repo_root = repo_root_for_codesearch(&db_path);
            let client = crate::adapters::codesearch::CodeSearchClient::from_env_for_repo(
                repo_root.as_deref(),
            );
            let hits = client.search(&query, limit.max(20))?;
            let rows = crate::query::blend(&conn, &hits, embed_weight, graph_weight, limit)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&rows)?);
            } else {
                for row in &rows {
                    println!(
                        "{:.3} {} {}:{} (embed {:.3}, graph {:.3})",
                        row.blended_score,
                        row.symbol.qualified_name,
                        row.symbol.file_path,
                        row.symbol.start_line,
                        row.embedding_score,
                        row.graph_score
                    );
                }
            }
        }
        #[cfg(feature = "miseledger")]
        Command::Links { term, limit, json } => {
            crate::adapters::miseledger::emit_deprecation_warning();
            let db = crate::adapters::miseledger::default_db_path();
            let hits = crate::adapters::miseledger::search_evidence(&db, &term, limit)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&hits)?);
            } else {
                for hit in &hits {
                    println!("[{}] {} — {}", hit.source_kind, hit.item_id, hit.snippet);
                }
            }
        }
    }

    Ok(())
}

fn default_query_db_path(explicit: Option<PathBuf>) -> PathBuf {
    explicit.unwrap_or_else(|| PathBuf::from(".graphtrail/graphtrail.db"))
}

#[cfg(feature = "codesearch")]
fn repo_root_for_codesearch(db: &std::path::Path) -> Option<PathBuf> {
    repo_from_graphtrail_db(db).or_else(|| std::env::current_dir().ok())
}

#[cfg(feature = "codesearch")]
fn repo_from_graphtrail_db(db: &std::path::Path) -> Option<PathBuf> {
    let graph_dir = db.parent()?;
    if graph_dir.file_name()? != ".graphtrail" {
        return None;
    }
    let parent = graph_dir.parent()?;
    // A relative default like `.graphtrail/graphtrail.db` has an empty parent here;
    // the repo root in that case is the current directory.
    if parent.as_os_str().is_empty() {
        std::env::current_dir().ok()
    } else {
        Some(parent.to_path_buf())
    }
}

fn print_json_or_symbols(json: bool, rows: &[SearchRow]) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(rows)?);
        return Ok(());
    }
    for row in rows {
        println!(
            "{} {} {}:{} {}",
            row.kind, row.qualified_name, row.file_path, row.start_line, row.signature
        );
    }
    Ok(())
}

fn print_json_or_edges(json: bool, rows: &[EdgeRow]) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(rows)?);
        return Ok(());
    }
    for row in rows {
        println!(
            "{} --{}@{} hops={}--> {}  ({} -> {})",
            row.source,
            row.kind,
            row.line.unwrap_or_default(),
            row.hops,
            row.target,
            row.source_file,
            row.target_file
        );
    }
    Ok(())
}

fn print_doctor_report(report: &crate::query::DoctorReport, json: bool) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(report)?);
        return Ok(());
    }
    println!("repo: root={} db={}", report.repo_root, report.db_path);
    println!(
        "version: tool={} schema={}/{} needs_migration={}",
        report.tool_version,
        report
            .schema
            .stored
            .map(|version| version.to_string())
            .unwrap_or_else(|| "missing".to_string()),
        report.schema.current,
        report.schema.needs_migration
    );
    println!(
        "last_sync: synced_at={} age_seconds={}",
        report.last_sync.synced_at.as_deref().unwrap_or("missing"),
        report
            .last_sync
            .age_seconds
            .map(|age| age.to_string())
            .unwrap_or_else(|| "missing".to_string())
    );
    println!(
        "branch: synced={} current={} drifted={}",
        report.branch.synced.as_deref().unwrap_or("unknown"),
        report.branch.current.as_deref().unwrap_or("unknown"),
        report.branch.drifted
    );
    println!(
        "pending: new_files={} changed_files={} deleted_files={} fingerprint_stale={}",
        report.pending.new_files,
        report.pending.changed_files,
        report.pending.deleted_files,
        report.pending.fingerprint_stale
    );
    println!(
        "ignored: hardcoded_floor={} gitignore={}",
        report.ignored.hardcoded_floor, report.ignored.gitignore
    );
    println!("verdict: {}", report.verdict);
    Ok(())
}

fn render_context(pack: &ContextPack) -> String {
    use std::fmt::Write;

    let mut text = String::new();
    let _ = writeln!(text, "# Context\n");
    let _ = writeln!(text, "task: {}\n", pack.task);
    let _ = writeln!(text, "## Entry Points");
    for row in &pack.entry_points {
        let _ = writeln!(
            text,
            "- {} `{}` at {}",
            row.kind,
            row.qualified_name,
            symbol_location(row)
        );
    }
    let _ = writeln!(text, "\n## Callers");
    for edge in &pack.callers {
        let _ = writeln!(
            text,
            "- `{}` calls `{}` at {}",
            edge.source,
            edge.target,
            edge_location(edge)
        );
    }
    let _ = writeln!(text, "\n## Callees");
    for edge in &pack.callees {
        let _ = writeln!(
            text,
            "- `{}` calls `{}` at {}",
            edge.source,
            edge.target,
            edge_location(edge)
        );
    }
    let _ = writeln!(text, "\n## Related Files");
    for file in &pack.related_files {
        let _ = writeln!(text, "- {file}");
    }
    text
}

fn render_diff(diff: &GraphDiff) -> String {
    use std::fmt::Write;

    let mut text = String::new();
    let s = &diff.summary;
    let _ = writeln!(text, "# Graph diff\n");
    let _ = writeln!(
        text,
        "nodes: +{} -{} ~{}   edges: +{} -{}   edges_line_insensitive: +{} -{}\n",
        s.added_nodes,
        s.removed_nodes,
        s.changed_nodes,
        s.added_edges,
        s.removed_edges,
        s.added_edges_line_insensitive,
        s.removed_edges_line_insensitive
    );

    let section = |text: &mut String, title: &str, nodes: &[crate::model::DiffNode]| {
        if nodes.is_empty() {
            return;
        }
        let _ = writeln!(text, "## {title}");
        for n in nodes {
            let _ = writeln!(
                text,
                "- {} `{}` at {}:{}",
                n.kind, n.qualified_name, n.file_path, n.start_line
            );
        }
        text.push('\n');
    };

    section(&mut text, "Added nodes", &diff.added_nodes);
    section(&mut text, "Removed nodes", &diff.removed_nodes);
    section(&mut text, "Changed nodes", &diff.changed_nodes);

    let edge_section = |text: &mut String, title: &str, edges: &[crate::model::DiffEdge]| {
        if edges.is_empty() {
            return;
        }
        let _ = writeln!(text, "## {title}");
        for e in edges {
            let _ = writeln!(
                text,
                "- `{}` calls `{}` at {}:{} -> {}",
                e.source, e.target, e.source_file, e.line, e.target_file
            );
        }
        text.push('\n');
    };

    edge_section(&mut text, "Added edges", &diff.added_edges);
    edge_section(&mut text, "Removed edges", &diff.removed_edges);

    text
}

#[cfg(any(feature = "miseledger", test))]
fn append_markdown_with_budget(output: &mut String, addition: &str, max_chars: usize) {
    let mut used_chars = output.chars().count();
    for line in addition.lines() {
        let candidate = format!("{line}\n");
        let candidate_chars = candidate.chars().count();
        if used_chars + candidate_chars > max_chars {
            break;
        }
        output.push_str(&candidate);
        used_chars += candidate_chars;
    }
}

#[cfg(feature = "miseledger")]
fn render_evidence_links(task: &str, pack: &ContextPack, limit: usize) -> Result<String> {
    use std::collections::BTreeSet;
    use std::fmt::Write;

    let db = crate::adapters::miseledger::default_db_path();
    let mut terms = BTreeSet::new();
    terms.insert(task.to_string());
    for row in &pack.entry_points {
        terms.insert(row.qualified_name.clone());
        terms.insert(row.name.clone());
    }

    let mut md = String::new();
    let _ = writeln!(md, "\n## Evidence links\n");
    let mut written = 0usize;
    for term in terms {
        if written >= limit {
            break;
        }
        let hits = crate::adapters::miseledger::search_evidence(&db, &term, 3)?;
        if hits.is_empty() {
            continue;
        }
        let _ = writeln!(md, "### `{term}`\n");
        for hit in hits {
            if written >= limit {
                break;
            }
            let _ = writeln!(
                md,
                "- [{}] `{}` - {}",
                hit.source_kind, hit.item_id, hit.snippet
            );
            written += 1;
        }
        md.push('\n');
    }
    if written == 0 {
        let _ = writeln!(md, "_none_\n");
    }
    Ok(md)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::SCHEMA_VERSION;

    #[cfg(feature = "codesearch")]
    #[test]
    fn repo_from_graphtrail_db_resolves_relative_default_to_current_dir() {
        let root = repo_from_graphtrail_db(std::path::Path::new(".graphtrail/graphtrail.db"));
        assert_eq!(root, std::env::current_dir().ok());
    }

    #[cfg(feature = "codesearch")]
    #[test]
    fn repo_from_graphtrail_db_resolves_absolute_paths_and_rejects_other_dirs() {
        let root = repo_from_graphtrail_db(std::path::Path::new(
            "/tmp/example/.graphtrail/graphtrail.db",
        ));
        assert_eq!(root, Some(std::path::PathBuf::from("/tmp/example")));
        assert_eq!(
            repo_from_graphtrail_db(std::path::Path::new("/tmp/example/other/graphtrail.db")),
            None
        );
    }

    fn sample_pack() -> ContextPack {
        ContextPack {
            schema_version: SCHEMA_VERSION,
            task: "wire context".to_string(),
            entry_points: vec![SearchRow {
                id: "sym-run".to_string(),
                kind: "function".to_string(),
                name: "run".to_string(),
                qualified_name: "run".to_string(),
                file_path: "app.py".to_string(),
                start_line: 5,
                end_line: 7,
                signature: "def run():".to_string(),
                score: 1.0,
            }],
            callers: vec![EdgeRow {
                source_id: "sym-main".to_string(),
                source: "main".to_string(),
                target_id: "sym-run".to_string(),
                target: "run".to_string(),
                kind: "call".to_string(),
                line: Some(12),
                source_file: "cli.py".to_string(),
                target_file: "app.py".to_string(),
                hops: 1,
                confidence: None,
            }],
            callees: vec![EdgeRow {
                source_id: "sym-run".to_string(),
                source: "run".to_string(),
                target_id: "sym-helper".to_string(),
                target: "helper".to_string(),
                kind: "call".to_string(),
                line: Some(6),
                source_file: "app.py".to_string(),
                target_file: "lib.py".to_string(),
                hops: 1,
                confidence: None,
            }],
            related_files: vec![
                "app.py".to_string(),
                "cli.py".to_string(),
                "lib.py".to_string(),
            ],
        }
    }

    #[test]
    fn cli_context_renders_ranges_and_edge_locations() {
        let text = render_context(&sample_pack());

        assert!(text.contains("- function `run` at app.py:5-7"));
        assert!(text.contains("- `main` calls `run` at cli.py:12 -> app.py"));
        assert!(text.contains("- `run` calls `helper` at app.py:6 -> lib.py"));
    }

    #[test]
    fn appended_markdown_respects_the_total_character_budget() {
        let mut output = "# Context\n\n- src/app.rs\n".to_string();

        append_markdown_with_budget(
            &mut output,
            "\n## Evidence links\n\n- first\n- second\n",
            45,
        );

        assert!(output.chars().count() <= 45);
        assert!(output.ends_with('\n'));
        assert!(!output.contains("- second"));
    }
}
