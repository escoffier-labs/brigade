from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "claude.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_claude_action_is_pinned_to_a_full_commit_sha():
    uses = re.findall(r"uses: anthropics/claude-code-action@(\S+)", _text())

    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in uses)


def test_claude_workflow_uses_subscription_token_not_api_key():
    text = _text()

    assert "claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in text
    assert "anthropic_api_key" not in text


def test_every_trigger_requires_mention_and_trusted_author():
    text = _text()
    condition = text[text.index("    if: >-") : text.index("    runs-on:")]

    for body in ("github.event.comment.body", "github.event.review.body"):
        assert f"contains({body}, '@claude')" in condition
    for association in ("github.event.comment.author_association", "github.event.review.author_association"):
        assert f"""contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), {association})""" in condition
    assert "pull_request_target" not in text


def test_claude_session_is_review_only():
    text = _text()

    assert "contents: read" in text
    assert "contents: write" not in text
    disallowed = re.search(r'--disallowedTools "([^"]+)"', text)
    assert disallowed is not None
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash(git push:*)", "Bash(git commit:*)"):
        assert tool in disallowed.group(1).split(",")


def test_inline_and_review_events_skip_fork_prs():
    text = _text()
    condition = text[text.index("    if: >-") : text.index("    runs-on:")]
    # Fork PR review events get no secrets, so they must be limited to same-repo branches.
    assert condition.count("github.event.pull_request.head.repo.full_name == github.repository") == 2
