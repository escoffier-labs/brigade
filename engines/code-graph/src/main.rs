use anyhow::Result;
use clap::Parser;

use graphtrail::cli::{Cli, run};

fn main() -> Result<()> {
    run(Cli::parse())
}
