from pathlib import Path

from brigade.research.sources import cli


def test_antigravity_adapter_builds_cli_provider(tmp_path: Path):
    providers = cli.build_providers(
        [
            {
                "type": "antigravity",
                "command": ["agy", "{query}"],
            }
        ],
        target=tmp_path,
    )

    assert len(providers) == 1
    assert providers[0].source_id == "antigravity"
    assert providers[0].source_type == "antigravity"
    assert providers[0].trust == "cli"
