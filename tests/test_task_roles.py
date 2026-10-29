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


@pytest.mark.parametrize(
    "text",
    [
        "Write a thread-safe cache for the scheduler",
        "Write an abstract base class for the storage backend",
        "Write resume logic for detached runs",
        "Write the email sender function",
        "Write a post-processing step for the importer",
        "Write the POST /api/users handler",
        "Write a handler for POST /users",
        "Edit the post-receive hook",
        "Update the thread pool size in the worker",
        "Update the guide parser to accept YAML",
        "Design the database layout for the queue",
        "Design the memory layout of the page allocator",
        "Improve the scheduler memory layout",
        "Make the settings page parser accept mobile flags",
    ],
)
def test_backend_action_objects_do_not_select_audience_roles(text):
    decision = task_roles.classify(text)
    assert (decision.status, decision.workload) == ("none", "general")


@pytest.mark.parametrize(
    "text",
    [
        "Draft release notes, add a migration for the users table",
        "Write the changelog entry and fix the retry bug in the scheduler",
        "Redesign the dashboard page and wire it to the new /stats API",
        "Draft a blog post. Implement the email sender function",
    ],
)
def test_separate_implementation_action_exposes_ambiguity(text):
    decision = task_roles.classify(text)
    assert (decision.status, decision.workload) == ("ambiguous", "general")
    assert decision.markers


@pytest.mark.parametrize(
    "text,workload",
    [
        ("Draft a blog post about API/authentication", "writing"),
        ("Draft release notes about the users table migration", "writing"),
        ("Write the changelog entry explaining the retry bug in the scheduler", "writing"),
        ("Redesign the login page for authentication", "frontend"),
        ("Implement the responsive dashboard page in Tailwind", "frontend"),
        ("Improve the hero section's spacing and typography", "frontend"),
        ("Make the settings page look better on mobile", "frontend"),
        ("Write an email about the sender function", "writing"),
        ("Write a guide about parsing YAML", "writing"),
        ("Write a thread about thread-safe caches", "writing"),
        ("Write an abstract about base classes", "writing"),
        ("Edit my resume for a backend role", "writing"),
        ('Draft a blog post titled "Do not edit the database"', "writing"),
        ("Don't change the API; draft a blog post about it", "writing"),
        ("Rewrite the article's introduction", "writing"),
    ],
)
def test_real_audience_actions_keep_technical_or_quoted_subjects(text, workload):
    decision = task_roles.classify(text)
    assert (decision.status, decision.workload) == ("matched", workload)


@pytest.mark.parametrize(
    "text",
    [
        "Do not edit the blog post; only bump the version constant",
        "Fix the test that asserts the string 'draft a blog post' appears in help output",
        "Do not redesign the login page; fix the parser",
        'The help sample says "draft a blog post". Fix its escaping',
    ],
)
def test_negated_actions_and_quoted_samples_are_not_requests(text):
    assert task_roles.classify(text).status == "none"


@pytest.mark.parametrize("fragment", ["SyntheticOnlyKeyA91", "/customers/SyntheticOnlyPathB42", "sYnThEtIcOnlyC73"])
def test_task_evidence_contains_categories_not_task_fragments(fragment):
    import json

    decision = task_roles.classify(f"Draft release notes and add {fragment} endpoint")
    assert decision.status == "ambiguous"
    assert fragment.lower() not in json.dumps(decision.payload()).lower()
    assert decision.markers == ("backend-request:endpoint",)
    assert set(decision.matched) <= {"writing", "frontend"}
    assert set(decision.vetoed) <= {"code-context", "negated-action", "quoted-content"}


@pytest.mark.parametrize(
    "text",
    [
        "Draft a tutorial explaining how to add an endpoint and migrate a table",
        "Write a guide about how to fix the retry bug and update the worker",
    ],
)
def test_conjoined_steps_inside_a_prose_topic_are_not_extra_actions(text):
    assert task_roles.classify(text).status == "matched"


@pytest.mark.parametrize(
    "text",
    [
        "Do not edit the blog post and redesign the login page",
        "Don't write a newsletter and draft a blog post",
    ],
)
def test_negation_continues_across_conjoined_actions(text):
    assert task_roles.classify(text).status == "none"
