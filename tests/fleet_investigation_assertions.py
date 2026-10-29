"""Structural read-only boundaries shared by Fleet Hub presentation tests."""

from tests.test_ui_theme_integration import Markup


def investigation_markup(page, *, only_investigation_details=False):
    parsed = Markup(page)
    roots = {
        index
        for index, (tag, attrs) in enumerate(parsed.tags)
        if tag in {"details", "div"} and "investigation" in attrs.get("class", "").split()
    }
    ids = [attrs["id"] for _, attrs in parsed.tags if "id" in attrs]
    assert len(ids) == len(set(ids)), "Investigation controls need unique IDs"
    for index, (tag, attrs) in enumerate(parsed.tags):
        owner = next((root for root in parsed.ancestors[index] if root in roots), None)
        assert tag not in {"form", "select"}, "Read-only pages cannot offer editing controls"
        if tag == "input":
            # Existing client-side list filtering is not a saved edit.
            assert attrs.get("type") == "search" and "disabled" in attrs and "name" not in attrs
            assert any(
                "explorer-search" in parsed.tags[parent][1].get("class", "").split()
                for parent in parsed.ancestors[index]
            )
        if tag == "details" and only_investigation_details:
            # The prompt folds inside the investigation wrapper; nothing else may fold.
            assert index in roots or owner is not None, "Only requested investigation evidence may be folded here"
        if tag == "textarea":
            assert owner is not None and "readonly" in attrs and "name" not in attrs
        if tag == "button" and "data-audience-set" in attrs:
            # The Admin/Agent switch is presentation only and never edits or saves anything.
            assert attrs.get("type") == "button" and attrs["data-audience-set"] in {"admin", "agent"}
            assert any(
                "audience-toggle" in parsed.tags[parent][1].get("class", "").split()
                for parent in parsed.ancestors[index]
            )
        elif tag == "button":
            assert owner is not None and attrs.get("type") == "button"
            target = attrs.get("data-copy-target")
            assert target, "Only investigation copy buttons are allowed"
            matches = [
                i
                for i, (target_tag, target_attrs) in enumerate(parsed.tags)
                if target_tag == "textarea" and target_attrs.get("id") == target
            ]
            assert len(matches) == 1 and owner in parsed.ancestors[matches[0]]
            assert parsed.text[index] == "Copy prompt for an agent"
    return parsed, roots


def ordinary_prose(parsed, roots):
    return " ".join(
        parsed.text[index]
        for index, (tag, _) in enumerate(parsed.tags)
        if tag in {"p", "h2", "h3", "a", "span"} and not roots.intersection(parsed.ancestors[index])
    )


def prompt_text(parsed):
    return "\n".join(parsed.text[index] for index, (tag, _) in enumerate(parsed.tags) if tag == "textarea")
