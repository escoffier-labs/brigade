# Brigade plugin

Brigade memory for Claude Code and Codex. See [docs/agent-plugin.md](../../docs/agent-plugin.md) for install steps, the tool list, and the loopback HTTP mode for MCP gateways.

The MCP server runs `npx -y @escoffier-labs/brigade@<version> memory serve-mcp --stdio`. If you already installed the CLI with pipx, you can change `command` to `brigade` and drop the first two arguments.

`skills/brigade-work/SKILL.md` is generated. Edit `src/brigade/templates/skills/brigade-work/SKILL.md` and run `scripts/sync_plugin_skills.py --write`.
