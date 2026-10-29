use anyhow::Result;
use clap::Parser;

use graphtrail::cli::{Cli, run};

pub(crate) fn main() -> Result<()> {
    run(Cli::parse())
}
