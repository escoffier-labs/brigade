"""External supervisor for the current authorized hosted npm candidate task.

The namespace attestation records operations; it is not a security boundary.
Docker init exiting destroys this disposable PID namespace.
"""

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def selected_tests(source):
    selected = []

    def visit(nodes, prefix):
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + [node.name])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                if any(arg.arg == "verify_signal_probe" for arg in parameters):
                    selected.append("::".join([*prefix, node.name]))

    for path in sorted((source / "tests").rglob("test_*.py")):
        visit(ast.parse(path.read_text()).body, [path.relative_to(source).as_posix()])
    return selected


def run_core(source):
    import pytest

    selected = selected_tests(source)
    result = {
        "selected_functions": selected,
        "collected": 0,
        "passed": 0,
        "skipped": 0,
        "failed": 0,
        "collection_errors": 0,
        "collection_matches": False,
        "passed_nodeids": [],
        "pytest_version": pytest.__version__,
        "status": "failed",
    }

    class Coverage:
        def pytest_collection_finish(self, session):
            ids = [item.nodeid for item in session.items]
            result["collected"] = len(ids)
            result["collection_matches"] = {node.split("[")[0] for node in ids} == set(selected)
            result["collected_nodeids"] = ids

        def pytest_collectreport(self, report):
            result["collection_errors"] += int(report.failed)
            result["skipped"] += int(report.skipped)

        def pytest_runtest_logreport(self, report):
            if report.when == "call" and report.passed:
                result["passed_nodeids"].append(report.nodeid)
                result["passed"] += 1
            result["skipped"] += int(report.skipped)
            result["failed"] += int(report.failed)

    rc = 1
    if selected:
        os.chdir(source)
        rc = int(
            pytest.main(["-q", "-ra", "-o", "addopts=", "-p", "no:cacheprovider", *selected], plugins=[Coverage()])
        )
    if (
        rc == 0
        and result["collection_matches"]
        and sorted(result["collected_nodeids"]) == sorted(result["passed_nodeids"])
        and len(set(result["collected_nodeids"])) == result["collected"]
        and result["collected"] > 0
        and result["passed"] == result["collected"]
        and result["failed"] == result["skipped"] == result["collection_errors"] == 0
    ):
        result["status"] = "passed"
    print("CORE_RESULT " + json.dumps(result), flush=True)
    return 0 if result["status"] == "passed" else 1


def main():
    namespace = os.readlink("/proc/self/ns/pid")
    if not os.environ.get("BRIGADE_HOST_PID_NAMESPACE") or namespace == os.environ["BRIGADE_HOST_PID_NAMESPACE"]:
        raise SystemExit("separate PID namespace required")
    if os.environ.get("BRIGADE_PARENT_SIGNAL_APPROVAL") != "approved":
        raise SystemExit("missing current authorized hosted npm task approval")
    mode, version = sys.argv[1:3]
    if mode not in {"core", "transport"}:
        raise SystemExit("unexpected isolated test mode")
    if f"{sys.version_info.major}.{sys.version_info.minor}" != version:
        raise SystemExit("unexpected isolated Python version")
    if "--child" in sys.argv:
        raise SystemExit(run_core(Path("/source")))
    attestation = {
        "parent_approval": "current authorized hosted npm candidate CI task",
        "externally_owned_teardown": True,
        "desktop_exposure": False,
        "pid_namespace": namespace,
        "supervisor_pid": os.getpid(),
        "namespace_init_pid": 1,
        "timeout_seconds": 240,
        "python": sys.version,
        "executable": sys.executable,
        "mode": mode,
        "node": subprocess.check_output(["node", "--version"], text=True).strip(),
    }
    path = Path("/tmp/signal-attestation.json")
    path.write_text(json.dumps(attestation))
    path.chmod(0o444)
    print("SUPERVISOR_ATTESTATION " + json.dumps(attestation), flush=True)
    source_hashes = {}
    for name in ("src", "tests", "npm/feasibility"):
        for item in sorted((Path("/source") / name).rglob("*")):
            if item.is_file() and "__pycache__" not in item.parts:
                source_hashes[item.relative_to("/source").as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    source_hashes["pyproject.toml"] = hashlib.sha256(Path("/source/pyproject.toml").read_bytes()).hexdigest()
    print("SOURCE_SHA256 " + json.dumps(source_hashes, sort_keys=True), flush=True)
    env = {
        **os.environ,
        "BRIGADE_DISPOSABLE_SIGNAL_TESTS": "approved",
        "BRIGADE_SIGNAL_SUPERVISOR_ATTESTATION": str(path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": "/source/src",
        "HOME": "/tmp/home",
    }
    Path(env["HOME"]).mkdir()
    command = (
        [sys.executable, __file__, mode, version, "--child"]
        if mode == "core"
        else ["node", "--test-reporter=tap", "--test", "/source/npm/feasibility/launcher.test.cjs"]
    )
    child = subprocess.Popen(command, cwd="/source", env=env)
    try:
        rc = child.wait(timeout=240)
    except subprocess.TimeoutExpired:
        print("supervisor deadline reached; exiting namespace", flush=True)
        rc = 124
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
