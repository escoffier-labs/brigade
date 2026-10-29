"""Task intent routing must distinguish audience work from mechanical writes."""

import pytest

from brigade import task_roles
from brigade.roster import Agent, Roster


@pytest.mark.parametrize(
    ("text", "status", "workload"),
    [
        ("Draft a blog post about the API schema", "matched", "writing"),
        ("Polish the prose in docs/quickstart.md for newcomers", "matched", "writing"),
        ("Rewrite my LinkedIn summary", "matched", "writing"),
        ("Proofread the cover letter", "matched", "writing"),
        ("Update the changelog entry", "matched", "writing"),
        ("Write the commit message", "matched", "writing"),
        ("Draft release notes", "matched", "writing"),
        ("Edit the PR description", "matched", "writing"),
        ("Redesign the login page visually", "matched", "frontend"),
        ("Critique the visual hierarchy of the pricing page screenshot", "matched", "frontend"),
        ("Implement the restyled navbar in Tailwind to match the mockup", "matched", "frontend"),
        ("Write a function that writes rows to the database", "none", "general"),
        ("Fix the file write race in store.py", "none", "general"),
        ("Add pagination to the /api/runs endpoint", "none", "general"),
        ("Configure vite build to emit source maps", "none", "general"),
        ("Write unit tests for the component registry", "none", "general"),
        ("Refactor the page cache eviction", "none", "general"),
        ("Mention write, page and component in a test", "none", "general"),
        ("Draft the release blog post and add the /api/releases endpoint", "ambiguous", "general"),
        ("Redesign the settings page and migrate the settings table schema", "ambiguous", "general"),
        ("Draft a blog post and redesign the landing page", "matched", "frontend"),
    ],
)
def test_classification(text, status, workload):
    decision = task_roles.classify(text)
    assert (decision.status, decision.workload) == (status, workload)
    assert decision.payload()["schema"] == "brigade.task_role.v1"


def test_bounded_deterministic_evidence():
    assert task_roles.classify("x" * 8000 + " Draft a blog post").status == "none"
    result = task_roles.classify("Draft a blog post and redesign the landing page")
    assert result == task_roles.classify("Draft a blog post and redesign the landing page")
    assert set(result.matched) == {"writing", "frontend"}
    assert task_roles.role_for_workload("windows-native") == "impl"
    assert task_roles.role_for_workload("writing") == "writing"


def test_strict_roster_copies_only_selected_fallback():
    agent = Agent(name="author", cli="claude", role="code", fallback=("coder",))
    coder = Agent(name="coder", cli="codex", role="code", fallback=("author",))
    roster = Roster(orchestrator="chef", agents={"author": agent, "coder": coder})
    strict = task_roles.strict_roster(roster, "author")
    assert strict.agents["author"].fallback == ()
    assert strict.agents["coder"] is coder
    assert roster.agents["author"].fallback == ("coder",)


@pytest.mark.parametrize(
    "text",
    [
        "Draft a blog post about how to add an endpoint",
        "Write a tutorial explaining how to migrate the database",
        "Redesign the login page for the authentication API",
        "Draft a blog post about the POST endpoint",
    ],
)
def test_technical_subject_is_not_an_extra_backend_request(text):
    assert task_roles.classify(text).status == "matched"


@pytest.mark.parametrize(
    "text",
    [
        "Draft a blog post. Add an endpoint.",
        "Add a CLI flag and write release notes",
        "Redesign the settings page and implement backend authentication",
    ],
)
def test_additional_backend_request_is_ambiguous(text):
    assert task_roles.classify(text).status == "ambiguous"


@pytest.mark.parametrize("text", ["Write a POST endpoint", "Edit the POST handler", "Rewrite the POST request"])
def test_http_post_is_not_audience_prose(text):
    assert task_roles.classify(text).status == "none"
