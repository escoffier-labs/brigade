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
    agent = Agent(name="author", cli="claude", role="code", fallback=("coder",), invalid_final_fallback="coder")
    coder = Agent(name="coder", cli="codex", role="code", fallback=("author",))
    roster = Roster(orchestrator="chef", agents={"author": agent, "coder": coder})
    strict = task_roles.strict_roster(roster, "author")
    assert strict.agents["author"].fallback == ()
    assert strict.agents["author"].invalid_final_fallback is None
    assert strict.agents["coder"] is coder
    assert roster.agents["author"].fallback == ("coder",)
    assert roster.agents["author"].invalid_final_fallback == "coder"


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
        "Update email validation to reject malformed addresses",
        "Update the POST /api/login endpoint to validate the request body",
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
        ("Draft an email about validation", "writing"),
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


@pytest.mark.parametrize(
    "text",
    [
        "Update the email sender to retry",
        "Update the Post model to include tags",
        "Update the article model and its serializer",
        "Update the newsletter subscription service",
        "Update the Thread type to carry a parent id",
        "Write a guide generator script",
        "Update the release notes generator",
        "Write the email template renderer",
        "Implement the layout engine for the PDF renderer",
        "Build the CSS minification step",
        "Build the Tailwind plugin that purges unused CSS",
        "Write the article indexer",
        "Update the blog post exporter",
        "Build the page compositor",
        "Implement the theme transformer",
        "Write the email-to-post converter",
        "Write the guide generator's blog post",
        "Build the CSS compressor page",
        "Write a renderer using email",
        "Design the layout-engine page",
    ],
)
def test_audience_and_visual_modifiers_do_not_change_the_object_head(text):
    assert task_roles.classify(text).status == "none"


@pytest.mark.parametrize(
    "text",
    [
        "Write the PR description and bump the version",
        "Update the README intro and refactor the loader",
        "Draft release notes plus add an endpoint",
        "Draft a newsletter but optimize startup",
        "Write an announcement and configure telemetry",
        "Draft release notes plus remove the obsolete entrypoint",
        "Redesign the pricing page but build a serializer",
    ],
)
def test_implementation_actions_do_not_need_a_known_code_noun(text):
    decision = task_roles.classify(text)
    assert decision.status == "ambiguous"
    assert decision.markers


@pytest.mark.parametrize(
    "text,workload",
    [
        ("Write a blog post on the new API", "writing"),
        ("Write release notes covering the database migration", "writing"),
        ("Draft a blog post announcing the new endpoint", "writing"),
        ("Draft an announcement of the new API", "writing"),
        ("Write a blog post with SQL examples", "writing"),
        ("Draft release notes but do not touch the API", "writing"),
        ("Redesign the login page with the auth API unchanged", "frontend"),
        ("Write the team's blog post intro", "writing"),
        ("Draft an email to customers about validation changes", "writing"),
        ("Implement the navbar in React", "frontend"),
        ("Improve the contrast on the pricing page", "frontend"),
        ("Build the landing page", "frontend"),
        ("Draft a tutorial on how to configure telemetry and bump the version", "writing"),
        ("Write a guide covering how to refactor the loader plus add logging", "writing"),
        ("Draft a blog post but redesign the pricing page", "frontend"),
    ],
)
def test_closed_object_heads_allow_prose_topics_and_visual_complements(text, workload):
    decision = task_roles.classify(text)
    assert (decision.status, decision.workload) == ("matched", workload)


@pytest.mark.parametrize("separator", [", then ", ", and ", " and also ", " plus ", " but "])
def test_action_separators_keep_both_requests(separator):
    decision = task_roles.classify(f"Draft release notes{separator}add an endpoint")
    assert decision.status == "ambiguous"
    assert decision.markers == ("backend-request:endpoint",)


@pytest.mark.parametrize(
    "verb", ["add", "fix", "create", "migrate", "alter", "remove", "refactor", "optimize", "wire", "bump", "configure"]
)
def test_implementation_verb_with_unknown_object_has_fixed_evidence(verb):
    decision = task_roles.classify(f"Draft release notes and {verb} SyntheticOnlyObjectQ83")
    assert decision.status == "ambiguous"
    assert decision.markers == ("backend-request:code",)
    assert "syntheticonly" not in str(decision.payload()).lower()


@pytest.mark.parametrize("backend", ["update the email sender to retry", "write the email template renderer"])
@pytest.mark.parametrize("separator", [" and ", " plus "])
def test_rejected_backend_modifier_composes_with_audience_work(backend, separator):
    assert task_roles.classify(backend).status == "none"
    decision = task_roles.classify("Draft release notes" + separator + backend)
    assert (decision.status, decision.workload) == ("ambiguous", "general")
    assert decision.markers == ("backend-request:code",)


def test_conjoined_closed_audience_objects_remain_writing():
    decision = task_roles.classify("Draft release notes and write a short email about the parser")
    assert (decision.status, decision.workload) == ("matched", "writing")
    assert decision.markers == ()
