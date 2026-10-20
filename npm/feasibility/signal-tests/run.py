"""Host-owned, bounded Docker teardown for candidate core/transport signal tests."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import re
import subprocess
import threading
import time
from uuid import uuid4


BASES = {
    "3.10": "python:3.10-slim@sha256:7b6bd969dda3ecfca0655524e338b57a8b8cd692a9362e3d55b5a3ef49b07256",
    "3.12": "python:3.12-slim@sha256:44ff437bba879d4941b710a369a8f19266aea34b29002807f0c487fabc9eec9b",
}
HELPERS = Path(__file__).resolve().parent
SOURCE = HELPERS.parents[2]


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=15)


def container_absent(result):
    return (
        result.returncode != 0
        and re.search(r"\bno such (?:container|object)\b", result.stderr, re.IGNORECASE) is not None
    )


def container_namespace(name):
    # start --attach is asynchronous with respect to the Docker daemon.
    deadline = time.monotonic() + 10
    while True:
        state = json.loads(docker("inspect", name).stdout)[0]["State"]
        if state["Running"]:
            break
        if state["Status"] not in {"created", "restarting"} or time.monotonic() >= deadline:
            raise RuntimeError("owned container did not start: " + json.dumps(state))
        time.sleep(0.05)
    return docker("exec", name, "python", "-c", "import os;print(os.readlink('/proc/self/ns/pid'))").stdout.strip()


def capture_output(stream, log):
    """Keep bounded diagnostics and independently retain structured proof records."""
    metadata = {}
    tail = bytearray()
    metadata_bytes = 0
    try:
        while line := stream.readline(8 * 1024**2 + 1):
            if len(line) > 8 * 1024**2:
                raise ValueError("isolated output line exceeds bound")
            key, _, value = line.partition(b" ")
            if key in (b"SOURCE_SHA256", b"SUPERVISOR_ATTESTATION", b"CORE_RESULT"):
                name = key.decode()
                if name in metadata:
                    raise ValueError("duplicate isolated proof record: " + name)
                metadata_bytes += len(value)
                if metadata_bytes > 8 * 1024**2:
                    raise ValueError("isolated metadata exceeds bound")
                metadata[name] = json.loads(value)
            tail.extend(line)
            if len(tail) > 2 * 1024**2:
                del tail[: len(tail) - 2 * 1024**2]
    finally:
        log.write_bytes(tail)
    return metadata


def mounted_hashes(mounts):
    hashes = {}
    for relative in mounts:
        root = SOURCE / relative
        for item in sorted(root.rglob("*")) if root.is_dir() else [root]:
            if item.is_file() and "__pycache__" not in item.parts:
                hashes[item.relative_to(SOURCE).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    return hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", choices=BASES, required=True)
    parser.add_argument("--mode", choices=("core", "transport"), default="core")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    token = uuid4().hex
    name = "brigade-npm-signals-" + token
    image = None
    host_namespace = os.readlink("/proc/self/ns/pid")
    receipt = {
        "status": "failed",
        "container": name,
        "mode": args.mode,
        "python": args.python,
        "base": BASES[args.python],
        "host_pid_namespace": host_namespace,
        "removed": False,
    }
    receipt["helper_sha256"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in HELPERS.iterdir() if p.is_file()
    }
    mounts = ["src", "tests", "pyproject.toml"]
    if args.mode == "transport":
        mounts += ["npm/feasibility/" + p for p in ("launcher.cjs", "launcher.test.cjs", "prepare.py", "runtime.json")]
    tmpfs_options = (
        "rw,nosuid,nodev,exec,size=256m,mode=1777"
        if args.mode == "transport"
        else "rw,nosuid,nodev,size=256m,mode=1777"
    )
    argv = [
        "docker",
        "create",
        "--name",
        name,
        "--label",
        "brigade.npm-signal-owner=" + token,
        "--platform",
        "linux/amd64",
        "--init",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--user",
        "65534:65534",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--pids-limit",
        "128",
        "--cpus",
        "2",
        "--read-only",
        "--tmpfs",
        "/tmp:" + tmpfs_options,
        "--env",
        "BRIGADE_PARENT_SIGNAL_APPROVAL=approved",
        "--env",
        "BRIGADE_HOST_PID_NAMESPACE=" + host_namespace,
    ]
    for relative in mounts:
        argv += ["--mount", f"type=bind,src={SOURCE / relative},dst=/source/{relative},readonly"]
    argv += ["image-built-before-create", args.mode, args.python]
    expected_hashes = mounted_hashes(mounts)
    process = None
    reader = None

    def interrupted(signum, frame):
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        # Build these exact tracked helpers before use, then address immutable ID.
        iid = args.out / "image-id.txt"
        build = [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "--iidfile",
            str(iid),
            "--build-arg",
            "PYTHON_IMAGE=" + BASES[args.python],
            str(HELPERS),
        ]
        receipt["build_argv"] = build
        try:
            with (args.out / "build.log").open("wb") as log:
                subprocess.run(build, check=True, stdout=log, stderr=subprocess.STDOUT, timeout=180)
        finally:
            build_log = args.out / "build.log"
            if build_log.exists():
                with build_log.open("rb") as log:
                    log.seek(max(0, build_log.stat().st_size - 2 * 1024**2))
                    bounded = log.read(2 * 1024**2)
                build_log.write_bytes(bounded)
                receipt["build_log_sha256"] = hashlib.sha256(bounded).hexdigest()
        image = iid.read_text().strip()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", image), "missing immutable built image"
        argv[-3] = image
        receipt["create_argv"] = argv
        subprocess.run(argv, check=True, capture_output=True, text=True, timeout=20)
        config = json.loads(docker("inspect", name).stdout)[0]
        receipt["inspect"] = config
        h = config["HostConfig"]
        assert h["PidMode"] in ("", "private") and h["NetworkMode"] == "none"
        assert h["CapDrop"] == ["ALL"] and "no-new-privileges=true" in h["SecurityOpt"]
        assert h["Memory"] == h["MemorySwap"] == 512 * 1024**2
        assert h["PidsLimit"] == 128 and h["NanoCpus"] == 2_000_000_000
        assert h["ReadonlyRootfs"] and h["Init"] and not h["Privileged"]
        assert h["Tmpfs"] == {"/tmp": tmpfs_options}
        assert config["Config"]["User"] == "65534:65534"
        assert config["Config"]["Labels"].get("brigade.npm-signal-owner") == token
        assert config["Image"] == image
        assert config["Config"]["Cmd"] == [args.mode, args.python]
        assert len(config["Mounts"]) == len(mounts)
        for m in config["Mounts"]:
            # Docker may reorder mounts; compare their complete sets below.
            assert not m["RW"] and m["Type"] == "bind"
        assert {(m["Source"], m["Destination"]) for m in config["Mounts"]} == {
            (str(SOURCE / relative), "/source/" + relative) for relative in mounts
        }
        receipt["image"] = json.loads(docker("image", "inspect", config["Image"]).stdout)[0]
        assert receipt["image"]["Os"] == "linux" and receipt["image"]["Architecture"] == "amd64"
        process = subprocess.Popen(
            ["docker", "start", "--attach", name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        metadata = {}
        reader_errors = []

        def drain():
            try:
                metadata.update(capture_output(process.stdout, args.out / "runtime.log"))
            except BaseException as error:
                reader_errors.append(str(error))

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        # This independent observation is addressed through the exact container.
        observed = container_namespace(name)
        assert observed and observed != host_namespace
        receipt["container_pid_namespace"] = observed
        rc = process.wait(timeout=270)
        reader.join(timeout=5)
        assert not reader.is_alive(), "attached Docker output did not close"
        assert not reader_errors, str(reader_errors)
        attestation = metadata["SUPERVISOR_ATTESTATION"]
        assert attestation["pid_namespace"] == observed
        assert int(attestation["node"].lstrip("v").split(".")[0]) >= 22
        receipt["attestation"] = attestation
        receipt["source_sha256"] = metadata["SOURCE_SHA256"]
        assert receipt["source_sha256"] == expected_hashes == mounted_hashes(mounts), "mounted source changed"
        receipt["runtime_log_sha256"] = hashlib.sha256((args.out / "runtime.log").read_bytes()).hexdigest()
        receipt["exit_status"] = rc
        assert rc == 0, f"isolated {args.mode} exited {rc}; see runtime.log"
        if args.mode == "core":
            result = metadata["CORE_RESULT"]
            assert sorted(result["collected_nodeids"]) == sorted(result["passed_nodeids"])
            assert result["collection_matches"]
            assert result["status"] == "passed" and result["collected"] == result["passed"] > 0
            assert result["skipped"] == result["failed"] == result["collection_errors"] == 0
            receipt["core"] = result
        else:
            text = (args.out / "runtime.log").read_text()
            counts = {
                key: int(value)
                for key, value in re.findall(r"^# (tests|pass|fail|cancelled|skipped|todo) (\d+)$", text, re.MULTILINE)
            }
            assert counts["tests"] == counts["pass"] + counts["skipped"] > 0
            assert counts["fail"] == counts["cancelled"] == counts["todo"] == 0
            skips = [line for line in text.splitlines() if " # SKIP" in line]
            assert counts["skipped"] == len(skips) <= 1
            if skips:
                assert "packaged Brigade verification child receives original caller UV settings and PATH" in skips[0]
                assert "focused real-artifact reproduction needs existing state" in skips[0]
            receipt["transport"] = {**counts, "skip_details": skips}
        receipt["status"] = "passed"
    except BaseException as error:
        receipt["error"] = str(error)
        raise
    finally:
        # Name plus unguessable owner label handles errors after create, including
        # a create timeout. Never remove a different container or enumerate peers.
        existing = docker("inspect", name, check=False)
        if existing.returncode == 0:
            owned = json.loads(existing.stdout)[0]
            assert owned["Config"]["Labels"].get("brigade.npm-signal-owner") == token
            receipt["final_state"] = owned["State"]
            docker("rm", "--force", owned["Id"])
            proof = docker("inspect", owned["Id"], check=False)
            receipt["removal_proof"] = {"exit_status": proof.returncode, "stderr": proof.stderr.strip()}
            receipt["removed"] = container_absent(proof)
        else:
            receipt["removal_proof"] = {"exit_status": existing.returncode, "stderr": existing.stderr.strip()}
            receipt["removed"] = container_absent(existing)
        if process is not None:
            process.wait(timeout=10)
        if reader is not None:
            reader.join(timeout=5)
        if not receipt["removed"]:
            receipt["status"] = "failed"
        (args.out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps({key: receipt[key] for key in ("status", "container", "removed")}))
        assert receipt["removed"], "exact container removal unproved"


if __name__ == "__main__":
    main()
