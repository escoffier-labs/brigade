// Share the legacy application, including argument parsing and exit behavior.
#[path = "graphtrail-mcp.rs"]
mod application;

fn main() -> anyhow::Result<()> {
    application::main()
}
