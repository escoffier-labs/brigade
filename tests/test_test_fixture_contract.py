from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_real_write_probes_opt_out_of_managed_tool_path_patch():
    text = (ROOT / "tests" / "conftest.py").read_text()

    anchor = text.index('"test_agent_write_probes"')
    opt_outs = text[text.rindex("{", 0, anchor) : text.index("}", anchor)]
    for module in ("test_proc", "test_agent_write_probes", "test_component_bins", "test_engine_rename_installer"):
        assert f'"{module}"' in opt_outs
