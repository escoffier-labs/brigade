//! Command-line surface and output formatting.

use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

use crate::model::{ContextPack, Direction, EdgeRow, SearchRow};
use crate::query::{build_context_pack, graph_edges, search_symbols, stats};
use crate::store::{db_path, init_schema, open_db, open_default, sync_repo};

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
    },
    Search {
        query: String,
        #[arg(long, default_value_t = 20)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    Callers {
        symbol: String,
        #[arg(long)]
        json: bool,
    },
    Callees {
        symbol: String,
        #[arg(long)]
        json: bool,
    },
    Impact {
        symbol: String,
        #[arg(long)]
        json: bool,
    },
    Context {
        task: String,
        #[arg(long, default_value_t = 12)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    Stats {
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
        Command::Sync { root } => {
            let db_path = db_path(cli.db, &root);
            let conn = open_db(&db_path)?;
            init_schema(&conn)?;
            let summary = sync_repo(&conn, &root)?;
            println!(
                "indexed files={} symbols={} calls={} imports={} db={}",
                summary.files,
                summary.symbols,
                summary.calls,
                summary.imports,
                db_path.display()
            );
        }
        Command::Search { query, limit, json } => {
            let conn = open_default(cli.db)?;
            let rows = search_symbols(&conn, &query, limit)?;
            print_json_or_symbols(json, &rows)?;
        }
        Command::Callers { symbol, json } => {
            let conn = open_default(cli.db)?;
            let edges = graph_edges(&conn, &symbol, Direction::Incoming)?;
            print_json_or_edges(json, &edges)?;
        }
        Command::Callees { symbol, json } => {
            let conn = open_default(cli.db)?;
            let edges = graph_edges(&conn, &symbol, Direction::Outgoing)?;
            print_json_or_edges(json, &edges)?;
        }
        Command::Impact { symbol, json } => {
            let conn = open_default(cli.db)?;
            let mut edges = graph_edges(&conn, &symbol, Direction::Incoming)?;
            edges.extend(graph_edges(&conn, &symbol, Direction::Outgoing)?);
            edges.sort_by(|a, b| {
                a.source_file
                    .cmp(&b.source_file)
                    .then_with(|| a.source.cmp(&b.source))
                    .then_with(|| a.target.cmp(&b.target))
            });
            print_json_or_edges(json, &edges)?;
        }
        Command::Context { task, limit, json } => {
            let conn = open_default(cli.db)?;
            let pack = build_context_pack(&conn, task, limit)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&pack)?);
            } else {
                print_context(&pack);
            }
        }
        Command::Stats { json } => {
            let conn = open_default(cli.db)?;
            let stats = stats(&conn)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&stats)?);
            } else {
                for (key, value) in stats {
                    println!("{key}: {value}");
                }
            }
        }
    }

    Ok(())
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
            "{} --{}@{}--> {}  ({} -> {})",
            row.source,
            row.kind,
            row.line.unwrap_or_default(),
            row.target,
            row.source_file,
            row.target_file
        );
    }
    Ok(())
}

fn print_context(pack: &ContextPack) {
    println!("# Context\n");
    println!("task: {}\n", pack.task);
    println!("## Entry Points");
    for row in &pack.entry_points {
        println!(
            "- {} `{}` at {}:{}",
            row.kind, row.qualified_name, row.file_path, row.start_line
        );
    }
    println!("\n## Callers");
    for edge in &pack.callers {
        println!("- `{}` calls `{}`", edge.source, edge.target);
    }
    println!("\n## Callees");
    for edge in &pack.callees {
        println!("- `{}` calls `{}`", edge.source, edge.target);
    }
    println!("\n## Related Files");
    for file in &pack.related_files {
        println!("- {file}");
    }
}
