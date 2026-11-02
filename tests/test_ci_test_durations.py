import json

from scripts import ci_test_durations


def _report(path, cases):
    body = "".join(f"<testcase {attributes}/>" for attributes in cases)
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{body}</testsuite></testsuites>')
    return path


def test_file_durations_sums_xunit1_files_and_resolves_xunit2_classnames(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_alpha.py").write_text("")
    (tmp_path / "tests" / "test_beta.py").write_text("")
    first = _report(
        tmp_path / "one.xml",
        [
            'classname="tests.test_alpha" name="test_a" file="tests/test_alpha.py" time="1.25"',
            'classname="tests.test_alpha.TestGroup" name="test_b" file="tests/test_alpha.py" time="0.75"',
        ],
    )
    second = _report(
        tmp_path / "two.xml",
        [
            'classname="tests.test_beta.TestGroup" name="test_c" time="3.5"',
            'classname="tests.test_beta" name="test_d" time="not-a-number"',
            'classname="tests.test_missing" name="test_e" time="9"',
        ],
    )
    assert ci_test_durations.file_durations([first, second], root=tmp_path) == {
        "tests/test_alpha.py": 2.0,
        "tests/test_beta.py": 3.5,
    }


def test_main_writes_the_balancing_table(tmp_path):
    report = _report(
        tmp_path / "junit.xml", ['classname="tests.test_x" name="test_x" file="tests/test_x.py" time="2.345"']
    )
    output = tmp_path / "durations.json"
    assert ci_test_durations.main([str(report), "--output", str(output)]) == 0
    assert json.loads(output.read_text()) == {"version": 1, "files": {"tests/test_x.py": 2.35}}
