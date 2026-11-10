"""#2479: the mounted embedder token is readable by the hub container, and an unusable one stops only a hybrid hub.

The hub container runs as UID 10001 and reads an operator-owned file through a
bind mount, so setup writes the token 0444 inside a 0700 `secrets/`. CI cannot
read a file as another UID, so these check the modes setup writes, the owner
and mode of the entrypoint's copy, and the entrypoint's decision with the open
error injected.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

RECIPE = Path(__file__).resolve().parents[1] / "ops" / "fleet-hub"
POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX owners and modes")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


setup = _load("brigade_hub_setup_token_modes", RECIPE / "bin" / "hub-setup.py")
entry = _load("brigade_hub_entry_token_modes", RECIPE / "hub" / "entry.py")
DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def project(tmp_path):
    target = tmp_path / "ops" / "fleet-hub"
    shutil.copytree(RECIPE, target, ignore=shutil.ignore_patterns(".env", "config", "secrets", "compose.override.yaml"))
    return target


def _no_gpu(argv):
    return 0, '{"runc": {}}'


def _gpu(argv):
    return (0, '{"nvidia": {}, "runc": {}}') if argv[:2] == ["docker", "info"] else (0, "Example GPU, 20000\n")


def _post(url, request, headers):
    vector = [0.1] * setup.load_r1(RECIPE / "r1-models.json")[0]["dimensions"]
    payload = {"data": [{"embedding": vector}]} if url.endswith("/v1/embeddings") else {"embeddings": [vector]}
    return 200, {"x-model-digest": DIGEST}, payload


def _init_remote(project, tmp_path):
    token = tmp_path / "embed.token"
    token.write_text("fake-remote-embed-token\n", encoding="utf-8")
    argv = ["--project-dir", str(project), "init", "--backup-dir", str(tmp_path / "backups"), "--no-gpu"]
    argv += ["--remote-url", "https://embed.example.test", "--remote-token-file", str(token)]
    return setup.main(argv, run=_no_gpu, post=_post)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


# ------------------------------------------------------------------ setup


@POSIX
def test_setup_writes_both_embedder_tokens_0444_inside_a_0700_secrets_folder(project, tmp_path):
    assert _init_remote(project, tmp_path) == 0
    secrets_dir = project / "secrets"
    assert _mode(secrets_dir) == 0o700 and secrets_dir.stat().st_uid == os.geteuid()
    assert _mode(secrets_dir / "embed-token") == 0o444
    argv = ["--project-dir", str(project), "switch", "--to", "hybrid"]
    assert setup.main(argv, run=_gpu, post=_post) == 0
    assert _mode(secrets_dir / "embed-local-token") == 0o444
    assert _mode(secrets_dir) == 0o700


@POSIX
def test_setup_resets_a_remote_token_an_earlier_setup_wrote_0400_and_then_leaves_it_alone(project, tmp_path, capsys):
    assert _init_remote(project, tmp_path) == 0
    secret = project / "secrets" / "embed-token"
    secret.chmod(0o400)
    assert _init_remote(project, tmp_path) == 0
    assert _mode(secret) == 0o444 and secret.read_text(encoding="utf-8") == "fake-remote-embed-token\n"
    # Same token, same endpoint, mode already 0444: the file is not rewritten.
    before = secret.stat().st_ino
    capsys.readouterr()
    assert _init_remote(project, tmp_path) == 0
    assert secret.stat().st_ino == before
    assert "embed-token" not in capsys.readouterr().out.split("secrets written:")[-1]


def _init_backend(project, tmp_path, backend):
    if backend == "remote":
        return _init_remote(project, tmp_path)
    argv = ["--project-dir", str(project), "init", "--backup-dir", str(tmp_path / "backups")]
    return setup.main(argv, run=_gpu, post=_post)


@POSIX
@pytest.mark.parametrize("mode", [0o755, 0o750])
@pytest.mark.parametrize("backend", ["remote", "local"])
def test_setup_refuses_an_existing_secrets_folder_others_can_enter_and_writes_nothing(
    project, tmp_path, capsys, mode, backend
):
    """A wider folder may already have shown its 0444 tokens to other users, so setup never tightens it quietly."""
    folder = project / "secrets"
    folder.mkdir()
    folder.chmod(mode)
    before = sorted(path.name for path in project.iterdir())
    assert _init_backend(project, tmp_path, backend) != 0
    err = capsys.readouterr().err
    assert f"secrets/ has mode {mode:04o}" in err
    assert "chmod 700" in err and "rotate any token" in err
    for name in ("admin token", "findings key", "embedder tokens"):
        assert name in err
    assert _mode(folder) == mode
    assert list(folder.iterdir()) == []
    assert sorted(path.name for path in project.iterdir()) == before
    assert not (tmp_path / "backups").exists()


@POSIX
def test_a_refused_secrets_folder_keeps_the_tokens_an_earlier_setup_left_in_it(project, tmp_path, capsys):
    assert _init_remote(project, tmp_path) == 0
    folder = project / "secrets"
    kept = {path.name: path.read_bytes() for path in folder.iterdir()}
    config = (project / "config" / "hub.toml").read_bytes()
    folder.chmod(0o755)
    assert _init_remote(project, tmp_path) != 0
    assert "secrets/ has mode 0755" in capsys.readouterr().err
    assert _mode(folder) == 0o755
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == kept
    assert (project / "config" / "hub.toml").read_bytes() == config


@POSIX
@pytest.mark.parametrize("umask", [0o002, 0o000, 0o277])
def test_setup_creates_a_new_secrets_folder_0700_under_any_umask(project, tmp_path, umask):
    previous = os.umask(umask)
    try:
        created = setup.ensure_secrets(project)
        assert os.umask(umask) == umask
    finally:
        os.umask(previous)
    assert created and _mode(project / "secrets") == 0o700


@POSIX
def test_setup_proceeds_with_an_existing_0700_secrets_folder(project, tmp_path):
    folder = project / "secrets"
    folder.mkdir()
    folder.chmod(0o700)
    assert _init_remote(project, tmp_path) == 0
    assert _mode(folder) == 0o700 and _mode(folder / "embed-token") == 0o444


@POSIX
def test_setup_refuses_a_new_secrets_folder_the_filesystem_made_wider_than_0700(project, tmp_path, monkeypatch, capsys):
    """A filesystem that ignores the mode leaves the new folder 0755, and a 0444 token there is readable by anyone."""
    mkdir = os.mkdir

    def wide(path, mode=0o777, **kwargs):
        mkdir(path, mode, **kwargs)
        if Path(path).name == "secrets":
            os.chmod(path, 0o755)

    monkeypatch.setattr(setup.os, "mkdir", wide)
    assert _init_remote(project, tmp_path) != 0
    assert "secrets/ was created with mode 0755" in capsys.readouterr().err
    assert not (project / "secrets").exists()
    assert not (project / "config" / "hub.toml").exists()


@POSIX
def test_install_probed_token_refuses_a_folder_widened_after_the_other_secrets(project, tmp_path):
    """The token is installed last, so the folder is checked again right before it."""
    assert setup.main(["--project-dir", str(project), "init", "--backup-dir", str(tmp_path / "b"), "--no-gpu"]) == 0
    (project / "secrets").chmod(0o750)
    decision = setup.Decision(retrieval="hybrid", backend="remote", probed_token="fake-remote-embed-token")
    with pytest.raises(setup.SetupError, match="mode 0750"):
        setup.install_probed_token(project, decision)
    assert not (project / "secrets" / "embed-token").exists()


# ------------------------------------------------------------- entrypoint

COPY = entry.EMBED_TOKEN_COPY


def _config(tmp_path, body: str) -> str:
    path = tmp_path / "hub.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _hybrid(token_file: str = COPY, backend: str = "remote", endpoint: str = "https://embed.example.test/v1") -> str:
    return (
        '[memory]\nretrieval = "hybrid"\n\n[memory.embedder]\n'
        f'backend = "{backend}"\nendpoint = "{endpoint}"\ntoken_file = "{token_file}"\n'
    )


def test_setups_hybrid_configs_read_the_copy_and_its_lexical_config_does_not(project, tmp_path):
    """The entrypoint's rule is read from the files setup really writes."""
    config = str(project / "config" / "hub.toml")
    assert _init_remote(project, tmp_path) == 0
    used, why = entry.embed_token_use(config)
    assert used and "remote embedder" in why
    assert setup.main(["--project-dir", str(project), "switch", "--to", "hybrid"], run=_gpu, post=_post) == 0
    used, why = entry.embed_token_use(config)
    assert used and "local embedder" in why
    assert setup.main(["--project-dir", str(project), "switch", "--to", "lexical"]) == 0
    assert entry.embed_token_use(config)[0] is False


@pytest.mark.parametrize(
    "body, used",
    [
        (_hybrid(), True),
        (_hybrid(backend="local", endpoint="http://embedder:11434/api/embed"), True),
        (_hybrid(token_file="/run/brigade/../brigade/embed_token"), True),
        (_hybrid(token_file="/etc/brigade/other-token"), False),
        (_hybrid(endpoint=""), False),
        ('[memory]\nretrieval = "lexical"\n', False),
        ('[memory]\nretrieval = "lexical"\n\n[memory.embedder]\ntoken_file = "' + COPY + '"\n', False),
        ("[other]\nkey = 1\n", False),
        ("", False),
        ("not toml [", False),
    ],
)
def test_only_a_hybrid_config_whose_embedder_reads_the_copy_uses_the_token(tmp_path, body, used):
    assert entry.embed_token_use(_config(tmp_path, body))[0] is used


def test_a_missing_or_unreadable_config_does_not_use_the_token(tmp_path, monkeypatch):
    used, why = entry.embed_token_use(str(tmp_path / "absent.toml"))
    assert used is False and "BM25" in why
    path = _config(tmp_path, _hybrid())

    def refuse(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("builtins.open", refuse)
    assert entry.embed_token_use(path)[0] is False


def test_the_config_looked_at_is_the_one_the_hub_will_read(tmp_path):
    assert entry.memory_config_path(["--db", "/data/hub.db", "--memory-config", "/etc/brigade/hub.toml"]) == (
        "/etc/brigade/hub.toml"
    )
    assert entry.memory_config_path(["--memory-config=/etc/x.toml"]) == "/etc/x.toml"
    # Without the flag the hub reads hub.toml beside its database.
    db = tmp_path / "state" / "hub.db"
    assert entry.memory_config_path(["--db", str(db)]) == str(tmp_path / "state" / "hub.toml")


def _started(monkeypatch):
    """Stand in for both ways main starts a hub. Returns the list each start is recorded in."""
    starts: list[list[str]] = []

    class Child:
        def __init__(self, argv):
            starts.append(list(argv))

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def send_signal(self, _signum):
            pass

    monkeypatch.setattr(entry, "engine_command", lambda argv: None)
    monkeypatch.setattr(entry, "engine_supports_memory_config", lambda: True)
    monkeypatch.setattr(entry.subprocess, "Popen", Child)
    return starts


def _eacces_mount(monkeypatch, tmp_path, mount_errno=errno.EACCES):
    """A mount the container's UID cannot open, and a tmpfs with a copy left by an earlier start.

    The copy is made in a temp folder. The configs still name the container
    path, which is what main compares `token_file` with.
    """
    folder = tmp_path / "run-brigade"
    folder.mkdir(mode=0o700)
    folder.chmod(0o700)
    copy = folder / "embed_token"
    copy.write_text("stale-token\n", encoding="utf-8")
    mount = str(tmp_path / "mounted" / "embed_token")
    real_open = os.open

    def fake_open(path, flags, *args, **kwargs):
        if path == mount:
            raise OSError(mount_errno, os.strerror(mount_errno))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(entry, "EMBED_TOKEN_MOUNT", mount)
    monkeypatch.setattr(entry, "EMBED_TOKEN_COPY", str(copy))
    monkeypatch.setattr(entry.os, "open", fake_open)
    # Defaults are bound at definition, so main's bare calls are pointed at the test paths.
    deliver, remove = entry.deliver_embed_token, entry.remove_embed_token_copy
    monkeypatch.setattr(entry, "deliver_embed_token", lambda: deliver(mount, str(copy)))
    monkeypatch.setattr(entry, "remove_embed_token_copy", lambda: remove(str(copy)))
    return copy


def _argv(config: str) -> list[str]:
    return ["--host", "127.0.0.1", "--port", "1", "--memory-config", config]


@POSIX
def test_hybrid_with_an_unreadable_token_mount_stops_before_any_hub_starts(tmp_path, monkeypatch, capsys):
    starts = _started(monkeypatch)
    _eacces_mount(monkeypatch, tmp_path)
    config = _config(tmp_path, _hybrid())
    assert entry.main(_argv(config)) == 2
    assert starts == []
    err = capsys.readouterr().err
    assert err.startswith("error: cannot read embedder token") and "Permission denied" in err
    assert "rerun bin/hub-setup.py, which writes it mode 0444" in err and "selects hybrid retrieval" in err
    assert "stale-token" not in err


@POSIX
@pytest.mark.parametrize(
    "config_body", ['[memory]\nretrieval = "lexical"\n', _hybrid("/etc/brigade/other-token"), None]
)
def test_a_config_that_does_not_use_the_token_starts_with_a_warning_and_no_copy(
    tmp_path, monkeypatch, capsys, config_body
):
    starts = _started(monkeypatch)
    copy = _eacces_mount(monkeypatch, tmp_path)
    config = str(tmp_path / "absent.toml") if config_body is None else _config(tmp_path, config_body)
    assert entry.main(_argv(config)) == 0
    assert len(starts) == 1 and starts[0][1:] == _argv(config)
    err = capsys.readouterr().err
    assert err.startswith("warning: cannot read embedder token") and "error:" not in err
    assert "Rerun bin/hub-setup.py" in err
    # The copy from an earlier start is gone, so a later hybrid config cannot pick up an old token.
    assert not copy.exists()


@POSIX
def test_a_symlinked_token_is_fatal_for_hybrid_and_never_copied_for_lexical(tmp_path, monkeypatch, capsys):
    starts = _started(monkeypatch)
    copy = _eacces_mount(monkeypatch, tmp_path, errno.ELOOP)
    assert entry.main(_argv(_config(tmp_path, _hybrid()))) == 2
    assert starts == [] and "is a symlink" in capsys.readouterr().err
    assert entry.main(_argv(_config(tmp_path, '[memory]\nretrieval = "lexical"\n'))) == 0
    assert len(starts) == 1 and not copy.exists()
    assert "warning: embedder token" in capsys.readouterr().err


@POSIX
def test_a_stale_copy_that_cannot_be_removed_is_fatal_even_for_lexical(tmp_path, monkeypatch, capsys):
    starts = _started(monkeypatch)
    _eacces_mount(monkeypatch, tmp_path)
    monkeypatch.setattr(entry, "remove_embed_token_copy", lambda: "cannot remove the earlier embedder token copy")
    assert entry.main(_argv(_config(tmp_path, '[memory]\nretrieval = "lexical"\n'))) == 2
    assert starts == [] and "error: cannot remove the earlier" in capsys.readouterr().err


@POSIX
def test_hybrid_with_a_readable_0444_mount_copies_it_0400_owned_by_the_hub_user_and_starts(
    tmp_path, monkeypatch, capsys
):
    starts = _started(monkeypatch)
    folder = tmp_path / "run-brigade"
    folder.mkdir(mode=0o700)
    folder.chmod(0o700)
    copy = folder / "embed_token"
    mount = tmp_path / "embed_token"
    mount.write_text("mounted-embed-token\n", encoding="utf-8")
    mount.chmod(0o444)
    deliver = entry.deliver_embed_token
    monkeypatch.setattr(entry, "deliver_embed_token", lambda: deliver(str(mount), str(copy)))
    assert entry.main(_argv(_config(tmp_path, _hybrid()))) == 0
    info = copy.lstat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o400 and info.st_uid == os.geteuid()
    assert copy.read_text(encoding="utf-8") == "mounted-embed-token\n"
    assert len(starts) == 1 and capsys.readouterr().err == ""


@POSIX
def test_an_image_without_the_flag_reads_the_config_beside_its_database_so_the_mount_is_not_needed(
    tmp_path, monkeypatch, capsys
):
    """The default BRIGADE_REF: the flag is dropped, the hub is in BM25 mode, and an unreadable mount only warns."""
    starts = _started(monkeypatch)
    monkeypatch.setattr(entry, "engine_supports_memory_config", lambda: False)
    monkeypatch.setattr(entry, "deliver_embed_token", lambda: "cannot read embedder token /run/secrets/embed_token")
    monkeypatch.setattr(entry, "remove_embed_token_copy", lambda: None)
    config = _config(tmp_path, _hybrid())
    argv = ["--host", "127.0.0.1", "--port", "1", "--db", str(tmp_path / "state" / "hub.db")]
    assert entry.main([*argv, "--memory-config", config]) == 0
    assert starts[0][1:] == argv
    err = capsys.readouterr().err
    assert "predates --memory-config" in err and "warning: cannot read embedder token" in err
