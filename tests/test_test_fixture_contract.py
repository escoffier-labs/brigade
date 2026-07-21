from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_real_write_probes_opt_out_of_managed_tool_path_patch():
    text = (ROOT / "tests" / "conftest.py").read_text()

    assert '{"test_proc", "test_agent_write_probes", "test_component_bins"}' in text
