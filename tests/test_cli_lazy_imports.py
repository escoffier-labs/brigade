# content-guard: allow home-path file
import sys


def test_unrelated_command_does_not_import_worklore_sync_modules():
    # Ensure they are not in sys.modules
    for module in list(sys.modules.keys()):
        if "worklore" in module:
            sys.modules.pop(module, None)

    from brigade.cli import main

    try:
        # Run an unrelated command. "doctor" triggers it without exiting via --help
        main(["doctor", "--target", "."])
    except SystemExit:
        pass

    # Check if they were imported
    assert "brigade.worklore_github_sync" not in sys.modules
    assert "brigade.worklore_brigade_sync" not in sys.modules
