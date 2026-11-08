"""Ingest acceptance rules shared by `brigade ingest`, `handoff lint` and `handoff draft`.

One place defines what an ingester accepts, so the writer (`brigade handoff
draft`) and the checker (`brigade handoff lint`) cannot drift from the reader.
Every function returns plain one-line problem strings and never raises on bad
handoff content.

Two layers:

* Static rules need only the handoff text: card name shape, card frontmatter
  (`title` or `topic`, `category`, `tags`), document target allow-list, and the
  `Expected card SHA256` section format for `update-card`.
* Workspace rules read the canonical workspace (`<workspace>/memory/cards`,
  bootstrap documents): the expected SHA must match the live card, `create-card`
  must not collide with an existing card, and an append must fit the bootstrap
  budget. They are skipped when the workspace is not given or not reachable.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import budgets

SAFE_CARD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
SAFE_RULE_PATH_RE = re.compile(r"^rules/[A-Za-z0-9._-]+\.md$")
SAFE_SPECIAL_TARGETS = frozenset(
    {
        "TOOLS.md",
        "USER.md",
        ".learnings/LEARNINGS.md",
        ".learnings/ERRORS.md",
        ".learnings/FEATURE_REQUESTS.md",
    }
)

EXPECTED_SHA_SECTION = "Expected card SHA256"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_KEYS = ("created", "updated", "last_verified", "last_reviewed", "review_after", "expires", "fresh_until")
CARDS_RELATIVE = Path("memory") / "cards"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_card_name(name: str) -> bool:
    return bool(SAFE_CARD_NAME_RE.fullmatch(name))


def safe_document_target(document: str) -> bool:
    return document in SAFE_SPECIAL_TARGETS or bool(SAFE_RULE_PATH_RE.fullmatch(document))


def cards_dir(workspace: Path | None) -> Path | None:
    """The workspace card directory, or None when the workspace has no readable one."""
    if workspace is None:
        return None
    path = workspace / CARDS_RELATIVE
    return path if path.is_dir() and not path.is_symlink() else None


def read_card_bytes(workspace: Path | None, name: str) -> bytes | None:
    """Current bytes of `<workspace>/memory/cards/<name>`, or None when absent or unreadable."""
    directory = cards_dir(workspace)
    if directory is None or not safe_card_name(name):
        return None
    path = directory / name
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def card_frontmatter_structure_problems(content: str) -> list[str]:
    """Structural problems, read with the parser ingest uses for cards.

    There is one frontmatter grammar: `card_fingerprint._parse_frontmatter_lines`.
    Whatever it cannot read, ingest cannot read, so lint and draft refuse it too.
    """
    from .card_fingerprint import _parse_frontmatter_lines, strip_frontmatter

    text = content.lstrip()
    _, has_frontmatter = _parse_frontmatter_lines(text)
    if not has_frontmatter:
        return ["card content missing YAML frontmatter (it must open and close with `---`)"]
    if not strip_frontmatter(text).strip():
        return ["card body is empty after the frontmatter"]
    return []


def _present(value: object) -> bool:
    return bool(value.strip()) if isinstance(value, str) else bool(value)


def card_frontmatter_problems(content: str) -> list[str]:
    """Structure plus the card policy the live ingester enforces on the parsed frontmatter.

    Policy: a `title` or `topic`, a `category`, a `tags` key, and ISO dates (date-only or
    a timestamp, as `memory_cmd._parse_date` reads them). The shared parser keeps a
    flow list as text, so `tags` is checked for presence, not for list shape.
    """
    from .card_fingerprint import _parse_frontmatter_lines
    from .memory_cmd import _parse_date

    problems = card_frontmatter_structure_problems(content)
    if problems and problems[0].startswith("card content missing"):
        return problems
    fields, _ = _parse_frontmatter_lines(content.lstrip())
    if not any(isinstance(fields.get(key), str) and _present(fields[key]) for key in ("title", "topic")):
        problems.append("card frontmatter needs a nonempty `title` or `topic`")
    category = fields.get("category")
    if not isinstance(category, str) or not category.strip():
        problems.append("card frontmatter needs a nonempty `category` (for example `category: foundation`)")
    if "tags" not in fields:
        problems.append("card frontmatter needs `tags` (for example `tags: [memory, handoff]`)")
    for key in _DATE_KEYS:
        value = fields.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if _parse_date(value) is None:
            problems.append(f"card frontmatter `{key}` must be an ISO date like 2026-11-08")
    return problems


def expected_sha_format_problem(value: str) -> str | None:
    if not value:
        return f"update-card requires an `## {EXPECTED_SHA_SECTION}` section holding the current card's SHA-256"
    if not SHA256_RE.fullmatch(value.strip().lower()):
        return f"`{EXPECTED_SHA_SECTION}` must be 64 lowercase hex characters"
    return None


def card_state_problems(
    *,
    action: str,
    target_card: str,
    expected_sha: str,
    workspace: Path | None,
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for the expected SHA and the live card state.

    The SHA format rule always runs for update-card. The state rules run only when
    `<workspace>/memory/cards` exists. An unverifiable update-card is a warning, not
    an error, so linting outside a memory workspace stays usable.
    """
    errors: list[str] = []
    warnings: list[str] = []
    sha = expected_sha.strip().lower()
    if action == "update-card":
        sha_problem = expected_sha_format_problem(sha)
        if sha_problem:
            errors.append(sha_problem)
    if not safe_card_name(target_card):
        return errors, warnings
    reachable = cards_dir(workspace) is not None
    existing = read_card_bytes(workspace, target_card)
    if action == "update-card":
        if not reachable:
            warnings.append(
                "expected SHA not checked against the live card: no memory/cards directory under the "
                "workspace (pass --workspace <memory workspace> to check it)"
            )
        elif existing is None:
            errors.append("update-card target card does not exist in the workspace cards (use create-card)")
        elif SHA256_RE.fullmatch(sha) and not sha_matches(existing, sha):
            errors.append(
                f"update-card {EXPECTED_SHA_SECTION} does not match the current card "
                "(the card changed after the SHA was read, re-read it)"
            )
    elif reachable and existing is not None:
        errors.append("create-card target card already exists in the workspace cards (use update-card)")
    return errors, warnings


def sha_matches(existing: bytes, expected: str) -> bool:
    return sha256_hex(existing) == expected.strip().lower()


def document_destination_problem(document: str, workspace: Path | None) -> str | None:
    """Reason a document destination is unsafe to append to, or None.

    Every component below the workspace root must be a real directory or file, so a
    permitted name such as USER.md cannot be a link that leaves the workspace.
    """
    if workspace is None:
        return None
    path = workspace
    for part in Path(document).parts:
        path = path / part
        if path.is_symlink():
            return "target document path contains a symlink; the ingester will not write through it"
    if path.exists() and not path.is_file():
        return "target document is not a regular file"
    return None


def bootstrap_budget_problem(document: str, content: str, workspace: Path | None) -> str | None:
    """Reason an append to a bootstrap document would exceed its budget, or None."""
    if workspace is None:
        return None
    dest = workspace / document
    would_exceed, budget = budgets.route_would_exceed_budget(dest, "\n\n" + content.strip() + "\n")
    if not would_exceed:
        return None
    return (
        "bootstrap budget guard: the append would exceed the target document's size budget; "
        "trim the file or promote to a memory card"
    )
