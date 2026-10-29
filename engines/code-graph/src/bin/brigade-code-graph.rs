// Share the legacy application, including argument parsing and exit behavior.
#[path = "../main.rs"]
mod application;

fn main() -> anyhow::Result<()> {
    application::main()
}
