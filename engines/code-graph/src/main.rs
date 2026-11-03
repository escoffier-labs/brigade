use anyhow::Result;

use graphtrail::cli::{parse_invoked, run};

pub(crate) fn main() -> Result<()> {
    run(parse_invoked())
}
