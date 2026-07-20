"""Generate static shell completion scripts from the brigade argparse tree.

Zero runtime dependencies: the command tree is walked once from the parser and
embedded in the emitted script, so completion does not shell out to brigade.
"""

from __future__ import annotations

import argparse
import sys


def _command_tree() -> dict[str, list[str]]:
    """Map each command path ("brigade", "brigade operator", ...) to its subcommands."""
    from .cli import _build_parser

    tree: dict[str, list[str]] = {}

    def walk(parser: argparse.ArgumentParser, prefix: list[str]) -> None:
        children: list[str] = []
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, subparser in action.choices.items():
                    children.append(name)
                    walk(subparser, [*prefix, name])
        if children:
            tree[" ".join(prefix)] = sorted(dict.fromkeys(children))

    walk(_build_parser(), ["brigade"])
    # These legacy plan forms share an executable command's opaque engine
    # arguments, so argparse cannot model both alternatives as subparsers.
    # Keep them in the static completion contract explicitly.
    for path in ("brigade search sync", "brigade evidence crawl"):
        tree[path] = sorted(set(tree.get(path, []) + ["plan"]))
    return tree


def bash_script(tree: dict[str, list[str]] | None = None) -> str:
    tree = tree if tree is not None else _command_tree()
    entries = "\n".join(f'  ["{path}"]="{" ".join(children)}"' for path, children in sorted(tree.items()))
    return f"""# brigade bash completion. Source this file (e.g. from ~/.bashrc):
#   source <(brigade completions bash)
declare -A _BRIGADE_TREE=(
{entries}
)
_brigade_complete() {{
  local cur path i w
  COMPREPLY=()
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  path="brigade"
  for (( i=1; i < COMP_CWORD; i++ )); do
    w="${{COMP_WORDS[i]}}"
    case "$w" in -*) continue ;; esac
    path="$path $w"
  done
  COMPREPLY=( $(compgen -W "${{_BRIGADE_TREE[$path]:-}}" -- "$cur") )
}}
complete -F _brigade_complete brigade
"""


def zsh_script(tree: dict[str, list[str]] | None = None) -> str:
    # zsh runs the bash completion via bashcompinit (compgen/complete/COMPREPLY).
    return (
        "# brigade zsh completion. Source this file (e.g. from ~/.zshrc):\n"
        "#   source <(brigade completions zsh)\n"
        "autoload -Uz bashcompinit 2>/dev/null && bashcompinit 2>/dev/null\n" + bash_script(tree)
    )


def fish_script(tree: dict[str, list[str]] | None = None) -> str:
    tree = tree if tree is not None else _command_tree()
    lines = [
        "# brigade fish completion. Save to ~/.config/fish/completions/brigade.fish:",
        "#   brigade completions fish > ~/.config/fish/completions/brigade.fish",
    ]
    top = tree.get("brigade", [])
    if top:
        lines.append(f"complete -c brigade -f -n __fish_use_subcommand -a '{' '.join(top)}'")
    for command in top:
        children = tree.get(f"brigade {command}")
        if children:
            lines.append(f"complete -c brigade -f -n '__fish_seen_subcommand_from {command}' -a '{' '.join(children)}'")
    return "\n".join(lines) + "\n"


_RENDERERS = {"bash": bash_script, "zsh": zsh_script, "fish": fish_script}


def emit(*, shell: str) -> int:
    renderer = _RENDERERS.get(shell)
    if renderer is None:
        print(f"error: unknown shell: {shell} (choose bash, zsh, or fish)", file=sys.stderr)
        return 2
    print(renderer(), end="")
    return 0
