"""Regression tests for cosign agent-change and commit-linkage exports."""

from __future__ import annotations

import base64
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from brigade import (
    agent_change,
    agent_change_verify,
    attestation,
    attestation_input,
    cli,
    commit_linkage,
    commit_linkage_verify,
    cosign_attestation,
)


def _bundle(statement: dict[str, Any]) -> dict[str, Any]:
    return {
        "mediaType": cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE,
        "verificationMaterial": {"publicKey": {"hint": "test-key"}, "tlogEntries": []},
        "dsseEnvelope": {
            "payloadType": attestation.DSSE_PAYLOAD_TYPE,
            "payload": base64.b64encode(attestation.canonical_statement_bytes(statement)).decode("ascii"),
            "signatures": [{"sig": base64.b64encode(b"test-signature").decode("ascii")}],
        },
    }


def _agent_workspace(tmp_path: Path, run_id: str = "run-001") -> tuple[Path, Path]:
    target = tmp_path / "workspace"
    target.mkdir()
    agent_change.init_policy(target)
    run_dir = target / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"tree_fingerprint": "a" * 40, "orchestrator": "chef", "worker": "worker"}),
        encoding="utf-8",
    )
    return target, run_dir


def _cosign_key(target: Path) -> Path:
    key = target / "cosign.key"
    key.write_text("test key", encoding="utf-8")
    return key


def test_export_statement_validates_the_bundle_from_create_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = {"_type": attestation.IN_TOTO_STATEMENT_TYPE, "subject": [], "predicateType": "test", "predicate": {}}
    expected = _bundle(statement)
    monkeypatch.setattr(cosign_attestation, "create_bundle", lambda received, _key: expected)

    assert cosign_attestation.export_statement(statement, tmp_path / "cosign.key") == expected


@pytest.mark.parametrize(
    ("statement", "cycle"),
    [
        (
            {
                "_type": attestation.IN_TOTO_STATEMENT_TYPE,
                "subject": [],
                "predicateType": "test",
                "predicate": float("nan"),
            },
            False,
        ),
        ({"_type": attestation.IN_TOTO_STATEMENT_TYPE, "subject": [], "predicateType": "test", "predicate": {}}, True),
        ({"_type": "wrong", "subject": [], "predicateType": "test", "predicate": {}}, False),
        (
            {
                "_type": attestation.IN_TOTO_STATEMENT_TYPE,
                "subject": [{"name": "git:tree", "digest": {"gitTree": 123}}],
                "predicateType": "test",
                "predicate": {},
            },
            False,
        ),
        (
            {
                "_type": attestation.IN_TOTO_STATEMENT_TYPE,
                "subject": [{"name": "git:tree", "digest": {"": "value"}}],
                "predicateType": "test",
                "predicate": {},
            },
            False,
        ),
        (
            {
                "_type": attestation.IN_TOTO_STATEMENT_TYPE,
                "subject": [{"name": "git:tree", "digest": {"gitTree": ""}}],
                "predicateType": "test",
                "predicate": {},
            },
            False,
        ),
        (
            {
                "_type": attestation.IN_TOTO_STATEMENT_TYPE,
                "subject": [{"name": "git:tree", "digest": {"gitTree": ["value"]}}],
                "predicateType": "test",
                "predicate": {},
            },
            False,
        ),
    ],
)
def test_export_statement_rejects_invalid_input_before_signing(
    statement: dict[str, Any], cycle: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if cycle:
        statement["predicate"] = statement
    monkeypatch.setattr(cosign_attestation, "create_bundle", lambda *_args: pytest.fail("signer called"))

    with pytest.raises(cosign_attestation.CosignAttestationError):
        cosign_attestation.export_statement(statement, tmp_path / "cosign.key")

    monkeypatch.undo()
    monkeypatch.setattr(cosign_attestation, "require_safe_cosign", lambda: pytest.fail("signer called"))
    with pytest.raises(cosign_attestation.CosignAttestationError):
        cosign_attestation.create_bundle(statement, tmp_path / "cosign.key")


def test_export_statement_rejects_payload_larger_than_the_bundle_limit_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [],
        "predicateType": "test",
        "predicate": {"padding": "x" * attestation_input.MAX_PAYLOAD_BYTES},
    }
    assert len(attestation.canonical_statement_bytes(statement)) > attestation_input.MAX_PAYLOAD_BYTES
    assert len(attestation.canonical_statement_bytes(statement)) <= attestation_input.MAX_JSON_BYTES
    monkeypatch.setattr(cosign_attestation, "require_safe_cosign", lambda: pytest.fail("signer called"))

    with pytest.raises(cosign_attestation.CosignAttestationError, match="payload"):
        cosign_attestation.export_statement(statement, tmp_path / "cosign.key")


def test_export_statement_accepts_a_canonical_payload_at_the_bundle_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [],
        "predicateType": "test",
        "predicate": {"padding": ""},
    }
    statement["predicate"]["padding"] = "x" * (
        attestation_input.MAX_PAYLOAD_BYTES - len(attestation.canonical_statement_bytes(statement))
    )
    assert len(attestation.canonical_statement_bytes(statement)) == attestation_input.MAX_PAYLOAD_BYTES
    monkeypatch.setattr(cosign_attestation, "create_bundle", lambda received, _key: _bundle(received))

    assert cosign_attestation.export_statement(statement, tmp_path / "cosign.key") == _bundle(statement)


def test_export_statement_uses_a_snapshot_across_signing_and_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = _bundle({})["dsseEnvelope"]
    statement = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [],
        "predicateType": "test",
        "predicate": {"value": statement},
    }
    expected = json.loads(json.dumps(statement))

    def mutate_original_and_return_bundle(received: dict[str, Any], _key: Path) -> dict[str, Any]:
        statement["predicate"]["value"] = "changed while signing"
        return _bundle(received)

    monkeypatch.setattr(cosign_attestation, "create_bundle", mutate_original_and_return_bundle)
    bundle = cosign_attestation.export_statement(statement, tmp_path / "cosign.key")
    decoded = json.loads(base64.b64decode(bundle["dsseEnvelope"]["payload"]))
    assert decoded == expected


def test_create_bundle_validates_against_its_snapshot_after_signer_mutates_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [],
        "predicateType": "test",
        "predicate": {"value": "before signing"},
    }
    expected = json.loads(json.dumps(statement))
    key = tmp_path / "cosign.key"
    key.write_text("test key", encoding="utf-8")
    monkeypatch.setattr(cosign_attestation, "require_safe_cosign", lambda: ("cosign", (3, 1, 3)))

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        statement["predicate"]["value"] = "changed while signing"
        bundle_path = Path(command[command.index("--bundle") + 1])
        bundle_path.write_text(json.dumps(_bundle(expected)), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(cosign_attestation.subprocess, "run", fake_run)
    assert cosign_attestation.create_bundle(statement, key) == _bundle(expected)


def test_agent_change_cosign_profile_writes_profile_default_and_out_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, run_dir = _agent_workspace(tmp_path)
    key = _cosign_key(target)
    monkeypatch.setattr(cosign_attestation, "export_statement", lambda statement, _key: _bundle(statement))
    assert agent_change.export_agent_change(target, "run-001", key=key, profile="cosign") == 3
    assert (run_dir / "agent-change.sigstore.json").is_file()

    custom = tmp_path / "explicit-bundle.json"
    assert agent_change.export_agent_change(target, "run-001", key=key, profile="cosign", out=str(custom)) == 3
    assert custom.is_file()


def test_agent_change_cosign_profile_maps_signer_failure_and_refuses_missing_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target, run_dir = _agent_workspace(tmp_path)
    key = _cosign_key(target)
    (target / ".brigade" / "attestation" / "agent-change-policy.json").unlink()
    assert agent_change.export_agent_change(target, "run-001", key=key, profile="cosign") == 2
    capsys.readouterr()

    agent_change.init_policy(target)
    monkeypatch.setattr(
        cosign_attestation,
        "export_statement",
        lambda _statement, _key: (_ for _ in ()).throw(cosign_attestation.CosignAttestationError("private detail")),
    )
    assert agent_change.export_agent_change(target, "run-001", key=key, profile="cosign") == 1
    assert capsys.readouterr().err == "error: cosign signer failed; agent-change artifact was not written\n"
    assert not (run_dir / "agent-change.sigstore.json").exists()


@pytest.mark.parametrize("module", [agent_change, commit_linkage])
def test_cosign_missing_key_uses_fixed_artifact_diagnostic(
    module: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "workspace"
    target.mkdir()
    if module is agent_change:
        (target / ".brigade" / "runs" / "run-001").mkdir(parents=True)
        result = module.export_agent_change(target, "run-001", profile="cosign")
        artifact = "agent-change"
    else:
        result = module.export_commit_linkage(target, "run-001", "b" * 40, profile="cosign")
        artifact = "commit-linkage"

    assert result == 1
    assert capsys.readouterr().err == f"error: cosign signer failed; {artifact} artifact was not written\n"


def test_commit_linkage_cosign_profile_writes_profile_default_and_refuses_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "workspace"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    run_dir = target / ".brigade" / "runs" / "run-001"
    run_dir.mkdir(parents=True)
    key = _cosign_key(target)
    sha = "b" * 40
    statement = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [{"name": "git:commit", "digest": {"gitCommit": sha}}],
        "predicateType": commit_linkage.COMMIT_LINKAGE_PREDICATE_TYPE,
        "predicate": {"equivalence": "exact"},
    }
    monkeypatch.setattr(commit_linkage, "build_statement", lambda *_args, **_kwargs: statement)
    monkeypatch.setattr(cosign_attestation, "export_statement", lambda received, _key: _bundle(received))

    assert commit_linkage.export_commit_linkage(target, "run-001", sha, key=key, profile="cosign") == 0
    destination = run_dir / "linkage" / f"{sha}.sigstore.json"
    assert destination.is_file()
    destination.unlink()
    destination.parent.rmdir()
    destination.parent.symlink_to(tmp_path / "elsewhere")
    assert commit_linkage.export_commit_linkage(target, "run-001", sha, key=key, profile="cosign") == 2


def test_commit_linkage_cosign_profile_maps_signer_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "workspace"
    target.mkdir()
    (target / ".brigade" / "runs" / "run-001").mkdir(parents=True)
    key = _cosign_key(target)
    sha = "b" * 40
    monkeypatch.setattr(
        commit_linkage,
        "build_statement",
        lambda *_args, **_kwargs: {"predicate": {"equivalence": "exact"}},
    )
    monkeypatch.setattr(
        cosign_attestation,
        "export_statement",
        lambda _statement, _key: (_ for _ in ()).throw(cosign_attestation.CosignAttestationError("private detail")),
    )

    assert commit_linkage.export_commit_linkage(target, "run-001", sha, key=key, profile="cosign") == 1
    assert capsys.readouterr().err == "error: cosign signer failed; commit-linkage artifact was not written\n"


@pytest.mark.parametrize("profile", ["sshsig", "cosign"])
@pytest.mark.parametrize("component", [".brigade", "runs", "run-001"])
def test_agent_change_default_refuses_symlinked_run_components_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, component: str
) -> None:
    target = tmp_path / "workspace"
    target.mkdir()
    key = _cosign_key(target)
    outside = tmp_path / "outside"
    outside.mkdir()
    if component == ".brigade":
        (target / ".brigade").symlink_to(outside, target_is_directory=True)
    elif component == "runs":
        (target / ".brigade").mkdir()
        (target / ".brigade" / "runs").symlink_to(outside, target_is_directory=True)
    else:
        (target / ".brigade" / "runs").mkdir(parents=True)
        (target / ".brigade" / "runs" / "run-001").symlink_to(outside, target_is_directory=True)
    signer = cosign_attestation if profile == "cosign" else attestation
    monkeypatch.setattr(
        signer,
        "export_statement" if profile == "cosign" else "create_envelope",
        lambda *_args: pytest.fail("signer called"),
    )

    assert agent_change.export_agent_change(target, "run-001", key=key, profile=profile) == 2


@pytest.mark.parametrize(
    "module, kwargs",
    [(agent_change, {"run_id": "run-001"}), (commit_linkage, {"run_id": "run-001", "commit_sha": "b" * 40})],
)
def test_programmatic_exports_refuse_unknown_profiles(
    module: Any, kwargs: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "workspace"
    target.mkdir()
    key = _cosign_key(target)
    export = module.export_agent_change if module is agent_change else module.export_commit_linkage
    assert export(target, key=key, profile="unknown", **kwargs) == 1
    assert "unsupported" in capsys.readouterr().err


@pytest.mark.parametrize("force", [False, True])
def test_agent_change_cosign_default_refuses_a_symlinked_leaf_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    target, run_dir = _agent_workspace(tmp_path)
    key = _cosign_key(target)
    (run_dir / "agent-change.sigstore.json").symlink_to(tmp_path / "elsewhere")
    monkeypatch.setattr(cosign_attestation, "export_statement", lambda *_args: pytest.fail("signer called"))

    assert agent_change.export_agent_change(target, "run-001", key=key, profile="cosign", force=force) == 2


@pytest.mark.parametrize("force", [False, True])
def test_commit_linkage_cosign_default_refuses_a_symlinked_leaf_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    target = tmp_path / "workspace"
    target.mkdir()
    run_dir = target / ".brigade" / "runs" / "run-001"
    linkage_dir = run_dir / "linkage"
    linkage_dir.mkdir(parents=True)
    key = _cosign_key(target)
    sha = "b" * 40
    statement = {"predicate": {"equivalence": "exact"}}
    (linkage_dir / f"{sha}.sigstore.json").symlink_to(tmp_path / "elsewhere")
    monkeypatch.setattr(commit_linkage, "build_statement", lambda *_args, **_kwargs: statement)
    monkeypatch.setattr(cosign_attestation, "export_statement", lambda *_args: pytest.fail("signer called"))

    assert commit_linkage.export_commit_linkage(target, "run-001", sha, key=key, profile="cosign", force=force) == 2


def test_linkage_discovery_ignores_cosign_bundles_and_direct_bundle_is_malformed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "workspace"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    run_dir = target / ".brigade" / "runs" / "run-001"
    linkage_dir = run_dir / "linkage"
    linkage_dir.mkdir(parents=True)
    sha = "b" * 40
    ssh_envelope = linkage_dir / f"{sha}.json"
    ssh_envelope.write_text("{}", encoding="utf-8")
    bundle = linkage_dir / f"{sha}.sigstore.json"
    bundle.write_text(json.dumps(_bundle({"predicate": {}})), encoding="utf-8")

    found, _, error = commit_linkage_verify._find_linkage_envelope(run_dir, None)
    assert found == ssh_envelope
    assert error is None
    assert commit_linkage_verify.verify_commit_linkage(bundle, target) == 2
    assert "cannot read commit-linkage envelope" in capsys.readouterr().err


def test_cli_help_names_profiles_and_verifier_envelopes(capsys: pytest.CaptureFixture[str]) -> None:
    for command in (("export", "agent-change"), ("export", "commit-linkage")):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["receipts", *command, "--help"])
        assert exc_info.value.code == 0
        assert "Signer profile. Defaults to sshsig." in capsys.readouterr().out
    for command in ("verify-agent-change", "verify-commit-linkage"):
        with pytest.raises(SystemExit) as exc_info:
            cli.main(["receipts", command, "--help"])
        assert exc_info.value.code == 0
        assert "SSHSIG" in capsys.readouterr().out


def _decode_statement(artifact: dict[str, Any]) -> dict[str, Any]:
    envelope = artifact.get("dsseEnvelope", artifact)
    return json.loads(base64.b64decode(envelope["payload"]))


def _fixed_statement_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    fixed_now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)

    class FixedDatetime:
        @classmethod
        def now(cls, _tz: timezone) -> datetime:
            return fixed_now

    monkeypatch.setattr(agent_change, "datetime", FixedDatetime)
    monkeypatch.setattr(commit_linkage, "datetime", FixedDatetime)
    monkeypatch.setattr(commit_linkage, "_utc_now_iso_z", lambda: "2026-09-07T12:00:00Z")
    monkeypatch.setattr(agent_change.secrets, "token_hex", lambda _bytes: "f" * 32)


def _agent_export_context(tmp_path: Path, run_id: str = "run-001") -> tuple[Path, Path, Path, Path]:
    target, run_dir = _agent_workspace(tmp_path, run_id)
    ssh_key, _ = attestation.keygen(target, principal="test-signer")
    cosign_key = _cosign_key(target)
    return target, run_dir, ssh_key, cosign_key


def _git_run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)


def _linkage_export_context(tmp_path: Path, run_id: str = "run-001") -> tuple[Path, Path, str, Path, Path]:
    target = tmp_path / "workspace"
    target.mkdir()
    _git_run(["git", "init", "-q", "-b", "main", str(target)])
    _git_run(["git", "-C", str(target), "config", "user.email", "fixture@example.invalid"])
    _git_run(["git", "-C", str(target), "config", "user.name", "Fixture"])
    (target / "tracked.txt").write_text("fixture", encoding="utf-8")
    _git_run(["git", "-C", str(target), "add", "tracked.txt"])
    _git_run(["git", "-C", str(target), "commit", "-qm", "fixture"])
    commit_sha = _git_run(["git", "-C", str(target), "rev-parse", "HEAD"]).stdout.strip()
    tree = _git_run(["git", "-C", str(target), "rev-parse", "HEAD^{tree}"]).stdout.strip()
    agent_change.init_policy(target)
    run_dir = target / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"tree_fingerprint": tree, "baseline_commit": commit_sha}), encoding="utf-8"
    )
    ssh_key, _ = attestation.keygen(target, principal="test-signer")
    cosign_key = _cosign_key(target)
    return target, run_dir, commit_sha, ssh_key, cosign_key


def test_agent_change_profiles_encode_equal_actual_builder_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_statement_sources(monkeypatch)
    target, run_dir, ssh_key, cosign_key = _agent_export_context(tmp_path)
    monkeypatch.setattr(cosign_attestation, "export_statement", lambda statement, _key: _bundle(statement))

    assert agent_change.export_agent_change(target, run_dir.name, key=ssh_key) == 3
    assert agent_change.export_agent_change(target, run_dir.name, key=cosign_key, profile="cosign") == 3

    ssh_statement = _decode_statement(json.loads((run_dir / "agent-change.json").read_text(encoding="utf-8")))
    cosign_statement = _decode_statement(
        json.loads((run_dir / "agent-change.sigstore.json").read_text(encoding="utf-8"))
    )
    assert ssh_statement == cosign_statement
    assert ssh_statement["predicate"]["complete"] is False


def test_commit_linkage_profiles_encode_equal_actual_builder_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fixed_statement_sources(monkeypatch)
    target, run_dir, commit_sha, ssh_key, cosign_key = _linkage_export_context(tmp_path)
    monkeypatch.setattr(cosign_attestation, "export_statement", lambda statement, _key: _bundle(statement))

    assert commit_linkage.export_commit_linkage(target, run_dir.name, commit_sha, key=ssh_key) == 0
    assert commit_linkage.export_commit_linkage(target, run_dir.name, commit_sha, key=cosign_key, profile="cosign") == 0

    ssh_statement = _decode_statement(
        json.loads((run_dir / "linkage" / f"{commit_sha}.json").read_text(encoding="utf-8"))
    )
    cosign_statement = _decode_statement(
        json.loads((run_dir / "linkage" / f"{commit_sha}.sigstore.json").read_text(encoding="utf-8"))
    )
    assert ssh_statement == cosign_statement
    assert ssh_statement["predicate"]["equivalence"] == "exact"


def test_export_statement_forwards_the_snapshot_and_export_attestation_delegates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [],
        "predicateType": "test",
        "predicate": {"result": "PASSED"},
    }
    key = tmp_path / "cosign.key"
    captured: list[tuple[dict[str, Any], Path]] = []

    def fake_create_bundle(received: dict[str, Any], received_key: Path) -> dict[str, Any]:
        captured.append((received, received_key))
        return _bundle(received)

    monkeypatch.setattr(cosign_attestation, "create_bundle", fake_create_bundle)
    assert cosign_attestation.export_statement(statement, key) == _bundle(statement)
    assert captured == [(statement, key)]

    built = dict(statement)
    delegated: list[tuple[dict[str, Any], Path]] = []
    monkeypatch.setattr(attestation, "build_statement", lambda _receipt: built)
    monkeypatch.setattr(
        cosign_attestation,
        "export_statement",
        lambda received, received_key: delegated.append((received, received_key)) or {"bundle": "exact"},
    )
    assert cosign_attestation.export_attestation({"receipt": "unused"}, key) == {"bundle": "exact"}
    assert delegated == [(built, key)]


@pytest.mark.parametrize("kind", ["agent-change", "commit-linkage"])
def test_exporter_profiles_resolve_cosign_keys_and_isolate_sshsig_environment(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if kind == "agent-change":
        target, run_dir, ssh_key, explicit_key = _agent_export_context(tmp_path)

        def invoke(**kwargs: Any) -> int:
            return agent_change.export_agent_change(target, run_dir.name, out="-", **kwargs)

    else:
        target, run_dir, commit_sha, ssh_key, explicit_key = _linkage_export_context(tmp_path)

        def invoke(**kwargs: Any) -> int:
            return commit_linkage.export_commit_linkage(target, run_dir.name, commit_sha, out="-", **kwargs)

    env_key = target / "env-cosign.key"
    env_key.write_text("env key", encoding="utf-8")
    monkeypatch.setenv(cosign_attestation.COSIGN_KEY_ENV, str(env_key))
    cosign_keys: list[Path] = []
    monkeypatch.setattr(
        cosign_attestation,
        "export_statement",
        lambda statement, key: cosign_keys.append(key) or _bundle(statement),
    )

    assert invoke(key=explicit_key, profile="cosign") in {0, 3}
    assert invoke(profile="cosign") in {0, 3}
    assert cosign_keys == [explicit_key.resolve(), env_key.resolve()]

    ssh_keys: list[Path] = []
    monkeypatch.setattr(
        attestation,
        "create_envelope",
        lambda statement, key: (
            ssh_keys.append(key)
            or {"payload": base64.b64encode(attestation.canonical_statement_bytes(statement)).decode()}
        ),
    )
    assert invoke(profile="sshsig") in {0, 3}
    assert ssh_keys == [ssh_key.resolve()]


@pytest.mark.parametrize("kind", ["agent-change", "commit-linkage"])
def test_exporter_missing_cosign_binary_uses_fixed_diagnostic_without_signer_details(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if kind == "agent-change":
        target, run_dir, _ssh_key, cosign_key = _agent_export_context(tmp_path)
        expected = "agent-change"
    else:
        target, run_dir, commit_sha, _ssh_key, cosign_key = _linkage_export_context(tmp_path)
        expected = "commit-linkage"
    monkeypatch.setattr(cosign_attestation.shutil, "which", lambda _binary: None)
    if kind == "agent-change":
        result = agent_change.export_agent_change(target, run_dir.name, key=cosign_key, profile="cosign", out="-")
    else:
        result = commit_linkage.export_commit_linkage(
            target, run_dir.name, commit_sha, key=cosign_key, profile="cosign", out="-"
        )
    assert result == 1
    error = capsys.readouterr().err
    assert error == f"error: cosign signer failed; {expected} artifact was not written\n"
    assert str(cosign_key) not in error


@pytest.mark.parametrize("kind", ["agent-change", "commit-linkage"])
def test_exporter_signer_failure_suppresses_process_error_details(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if kind == "agent-change":
        target, run_dir, _ssh_key, cosign_key = _agent_export_context(tmp_path)

        def invoke() -> int:
            return agent_change.export_agent_change(target, run_dir.name, key=cosign_key, profile="cosign", out="-")

        expected = "agent-change"
    else:
        target, run_dir, commit_sha, _ssh_key, cosign_key = _linkage_export_context(tmp_path)

        def invoke() -> int:
            return commit_linkage.export_commit_linkage(
                target, run_dir.name, commit_sha, key=cosign_key, profile="cosign", out="-"
            )

        expected = "commit-linkage"
    marker = "signer-error-marker"
    original_run = subprocess.run
    monkeypatch.setattr(cosign_attestation, "require_safe_cosign", lambda: ("cosign", (3, 1, 3)))

    def fake_run(command: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "attest-blob" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr=f"{marker} {cosign_key}")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(cosign_attestation.subprocess, "run", fake_run)
    assert invoke() == 1
    error = capsys.readouterr().err
    assert error == f"error: cosign signer failed; {expected} artifact was not written\n"
    assert marker not in error
    assert str(cosign_key) not in error


@pytest.mark.parametrize("kind", ["agent-change", "commit-linkage"])
@pytest.mark.parametrize("profile", ["sshsig", "cosign"])
def test_exporter_profiles_honor_stdout_explicit_paths_force_and_default_suffixes(
    kind: str,
    profile: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if kind == "agent-change":
        target, run_dir, ssh_key, cosign_key = _agent_export_context(tmp_path)

        def invoke(**kwargs: Any) -> int:
            return agent_change.export_agent_change(target, run_dir.name, **kwargs)

        default_path = run_dir / ("agent-change.json" if profile == "sshsig" else "agent-change.sigstore.json")
        expected_rc = 3
    else:
        target, run_dir, commit_sha, ssh_key, cosign_key = _linkage_export_context(tmp_path)

        def invoke(**kwargs: Any) -> int:
            return commit_linkage.export_commit_linkage(target, run_dir.name, commit_sha, **kwargs)

        suffix = ".json" if profile == "sshsig" else ".sigstore.json"
        default_path = run_dir / "linkage" / f"{commit_sha}{suffix}"
        expected_rc = 0
    if profile == "cosign":
        monkeypatch.setattr(cosign_attestation, "export_statement", lambda statement, _key: _bundle(statement))
        key = cosign_key
    else:
        key = ssh_key

    assert invoke(key=key, profile=profile, out="-") == expected_rc
    assert isinstance(json.loads(capsys.readouterr().out), dict)
    explicit_path = tmp_path / f"explicit-{kind}-{profile}.json"
    assert invoke(key=key, profile=profile, out=str(explicit_path)) == expected_rc
    assert explicit_path.is_file()
    default_path.parent.mkdir(parents=True, exist_ok=True)
    default_path.write_text("old", encoding="utf-8")
    assert invoke(key=key, profile=profile) == 1
    assert default_path.read_text(encoding="utf-8") == "old"
    assert invoke(key=key, profile=profile, force=True) == expected_rc
    assert isinstance(json.loads(default_path.read_text(encoding="utf-8")), dict)


def test_actual_agent_change_cosign_export_writes_incomplete_statement_and_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, run_dir, _ssh_key, cosign_key = _agent_export_context(tmp_path)
    monkeypatch.setattr(cosign_attestation, "export_statement", lambda statement, _key: _bundle(statement))

    assert agent_change.export_agent_change(target, run_dir.name, key=cosign_key, profile="cosign") == 3
    statement = _decode_statement(json.loads((run_dir / "agent-change.sigstore.json").read_text(encoding="utf-8")))
    assert statement["predicate"]["complete"] is False


def test_actual_commit_linkage_cosign_export_writes_none_equivalence_and_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, run_dir, _baseline_sha, _ssh_key, cosign_key = _linkage_export_context(tmp_path)
    (target / "tracked.txt").write_text("different", encoding="utf-8")
    _git_run(["git", "-C", str(target), "add", "tracked.txt"])
    _git_run(["git", "-C", str(target), "commit", "-qm", "different"])
    commit_sha = _git_run(["git", "-C", str(target), "rev-parse", "HEAD"]).stdout.strip()
    monkeypatch.setattr(cosign_attestation, "export_statement", lambda statement, _key: _bundle(statement))

    assert commit_linkage.export_commit_linkage(target, run_dir.name, commit_sha, key=cosign_key, profile="cosign") == 3
    statement = _decode_statement(
        json.loads((run_dir / "linkage" / f"{commit_sha}.sigstore.json").read_text(encoding="utf-8"))
    )
    assert statement["predicate"]["equivalence"] == "none"


def test_agent_change_verifier_rejects_a_direct_sigstore_bundle_as_malformed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _run_dir, _ssh_key, _cosign_key = _agent_export_context(tmp_path)
    bundle_path = tmp_path / "agent-change.sigstore.json"
    bundle_path.write_text(json.dumps(_bundle({"predicate": {}})), encoding="utf-8")

    assert agent_change_verify.verify_agent_change(bundle_path, target) == 2
    assert "cannot read agent-change envelope" in capsys.readouterr().err
