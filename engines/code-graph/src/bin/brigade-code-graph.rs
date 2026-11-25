use anyhow::Result;

use brigade_code_graph::cli::{Cli, run};
use clap::Parser;

fn main() -> Result<()> {
    run(Cli::parse())
}
