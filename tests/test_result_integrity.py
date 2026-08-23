from brigade.result_integrity import _progress_only, _split_clauses, validate_final_output

# Issue #1099: grok CLI 1.0.5 on Windows glued the progress sentence to the
# final answer with no space after the period.
_GLUED_FINAL = (
    "I'll read the README for the language claim and follow the Brigade worker "
    "start path before answering.media-cli is written in bash."
)
_SPACED_FINAL = _GLUED_FINAL.replace("answering.media-cli", "answering. media-cli")
_GROK_PROGRESS_WITH_PATHS = (
    "Reviewing the README.md and tools/brigade.md diffs against Brigade 0.22.0. "
    "Gathering the git diffs and current file content first."
)


def test_glued_progress_plus_final_answer_is_accepted():
    assert _progress_only(_GLUED_FINAL) is False
    assert validate_final_output(_GLUED_FINAL) is None


def test_spaced_progress_plus_final_answer_is_accepted():
    assert _progress_only(_SPACED_FINAL) is False
    assert validate_final_output(_SPACED_FINAL) is None


def test_progress_only_without_final_answer_is_still_rejected():
    text = "I'll read the README for the language claim and follow the Brigade worker start path before answering."
    failure = validate_final_output(text)
    assert _progress_only(text) is True
    assert failure is not None
    assert failure.kind == "non-final-output"


def test_progress_only_with_file_extensions_and_version_is_still_rejected():
    failure = validate_final_output(_GROK_PROGRESS_WITH_PATHS)
    assert _progress_only(_GROK_PROGRESS_WITH_PATHS) is True
    assert failure is not None
    assert failure.kind == "non-final-output"


def test_glued_final_after_spaced_progress_sentence_is_accepted():
    text = "Reviewing files first. I'll read more.media-cli is written in bash."
    assert _progress_only(text) is False
    assert validate_final_output(text) is None


def test_glued_capitalized_sentence_start_is_accepted():
    text = "I'll inspect the repository first.It is written in bash."
    assert _progress_only(text) is False
    assert validate_final_output(text) is None


def test_single_progress_sentence_with_file_extension_is_still_rejected():
    text = "Reviewing the README.md first."
    failure = validate_final_output(text)
    assert _split_clauses(text) == [text]
    assert _progress_only(text) is True
    assert failure is not None
    assert failure.kind == "non-final-output"


def test_glued_final_starting_with_short_word_is_accepted():
    text = "Inspecting the repo.it is a durable child run."
    assert _split_clauses(text) == ["Inspecting the repo.", "it is a durable child run."]
    assert _progress_only(text) is False
    assert validate_final_output(text) is None


def test_progress_only_with_long_file_extension_is_still_rejected():
    text = "Reviewing the App.csproj first."
    failure = validate_final_output(text)
    assert _split_clauses(text) == [text]
    assert _progress_only(text) is True
    assert failure is not None
    assert failure.kind == "non-final-output"


def test_progress_only_with_version_digits_is_still_rejected():
    text = "Reviewing Brigade v1.2 first."
    failure = validate_final_output(text)
    assert _split_clauses(text) == [text]
    assert _progress_only(text) is True
    assert failure is not None
    assert failure.kind == "non-final-output"
