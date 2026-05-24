from brigade import cli


def test_main_deprecated_warns_and_delegates(capsys, tmp_path):
    rc = cli.main_deprecated(["doctor", "--target", str(tmp_path)])
    err = capsys.readouterr().err
    assert "deprecated" in err.lower()
    assert "brigade" in err.lower()
    # delegates to main: doctor on an empty dir returns 1
    assert rc == 1
