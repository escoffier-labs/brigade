"""Regression coverage for replay headroom and recoverable care replacement."""

import base64
import dataclasses
import subprocess
import sys

import pytest

from brigade import care_cmd, care_windows
from brigade import memory_hub_replica_limits as limits
from brigade import memory_hub_store as store
from brigade import memory_hub_sync as sync
from tests.test_memory_reflect_hub import run, setup as setup


def test_churn_event_limit_names_preseed_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "REPLAY_MAX_EVENTS", 2, raising=False)
    deferred = sync._Deferred(store.Replica(tmp_path / "replica"))
    with limits.bounded(max_cards=10, max_bytes=1024 * 1024):
        deferred.examine()
        deferred.examine()
        with pytest.raises(
            limits.ReplicaLimitError, match="examined-event limit of 2.*brigade memory hub pull.*unbounded"
        ):
            deferred.examine()


def test_realistic_150000_event_log_replays_under_defaults(tmp_path):
    class Stream:
        def call(self, action, *, after_seq, limit):
            assert action == "log"
            end = min(after_seq + limit, 150_000)
            return {
                "log_epoch": "test-epoch",
                "next_seq": end,
                "events": [
                    {
                        "seq": seq,
                        "kind": "card.retire" if seq % 2 == 0 else "card.put",
                        "scope": "fleet",
                        "card_id": f"card-{((seq - 1) // 2) % 100}",
                        "revision": seq,
                    }
                    for seq in range(after_seq + 1, end + 1)
                ],
            }

    replica = store.Replica(tmp_path / "replica")
    settings = sync.hubc.HubSettings()
    with limits.bounded(max_cards=50_000, max_bytes=65 * 1024 * 1024):
        sync._follow(
            Stream(),
            replica,
            settings,
            epoch="test-epoch",
            readable={"fleet"},
            report={"applied": 0, "warnings": []},
            save=lambda: None,
        )
    assert replica.meta["seq"] == 150_000
    assert replica.heads == {}


def _script(tmp_path):
    entry = dataclasses.replace(care_cmd.MEMORY_REFLECT_ENTRY, reflect_scope="fleet")
    command = care_windows.create_command(
        entry,
        command=care_windows.task_run(entry, workspace=tmp_path),
        name="BrigadeCare-test-recovery",
        schedule="/SC DAILY /ST 06:15",
        account='/RU "%USERNAME%" /IT',
    )
    return base64.b64decode(command.split("-EncodedCommand ")[1]).decode("utf-16le")


def test_care_replacement_restores_original_xml_on_post_create_failure(tmp_path):
    script = _script(tmp_path)
    assert script.index("$x=$o.Xml") < script.index("/Create")
    assert script.index("try { &") < script.index("/Create") < script.index("$d=$f.GetTask")
    assert "try { if ($x)" in script
    assert "$f.RegisterTask(" in script and ",$x,6,$null,$null,3,$null)" in script
    assert script.endswith("$c.Message,$b) }")


def test_care_restore_failure_preserves_initiating_error(tmp_path):
    script = _script(tmp_path)
    handler = script.split("; } catch {", 1)[1]
    assert handler.startswith(" $c=$_.Exception; $b='none';")
    assert "catch { $b=$_.Exception.Message;" in handler
    assert handler.endswith("throw ('care task {0}: {1}; rollback: {2}' -f $n,$c.Message,$b) }")


def test_care_restore_failure_removes_placeholder(tmp_path):
    handler = _script(tmp_path).split("; } catch {", 1)[1]
    assert "$m={ try { $null=$f.GetTask($n); " in handler
    assert "$f.DeleteTask($n,0)" in handler
    assert "catch { $b=$_.Exception.Message; try { & $m } catch {" in handler
    assert "$b+='; cleanup: '+$_.Exception.Message" in handler


def test_care_pre_create_failure_ignores_absent_task(tmp_path):
    handler = _script(tmp_path).split("; } catch {", 1)[1]
    lookup = handler.index("$null=$f.GetTask(")
    delete = handler.index("$f.DeleteTask(")
    assert lookup < delete < handler.index("if ($_.Exception.GetBaseException().HResult -ne -2147024894) { throw }")
    assert "catch { if ($_.Exception.GetBaseException().HResult" in handler
    assert " } else { & $m } } catch {" in handler
    assert "$c.Message,$b) }" in handler


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows Task Scheduler")
def test_native_post_create_com_failure_restores_working_task(tmp_path):
    script = _script(tmp_path)
    name = "BrigadeCare-test-recovery"

    def execute(value):
        encoded = base64.b64encode(value.encode("utf-16le")).decode()
        return subprocess.run(
            [care_windows.POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def xml():
        return subprocess.check_output(
            [care_windows.SCHTASKS_EXE, "/Query", "/TN", name, "/XML"], text=True, timeout=15
        )

    try:
        result = execute(script)
        assert result.returncode == 0, result.stderr
        before = xml()
        failed = script.replace(
            "$d=$f.GetTask",
            "$f.GetTask('BrigadeCare-nonexistent-injected'); $d=$f.GetTask",
        )
        result = execute(failed)
        assert result.returncode != 0
        assert xml() == before
    finally:
        subprocess.run([care_windows.SCHTASKS_EXE, "/Delete", "/TN", name, "/F"], capture_output=True, timeout=15)


def test_reflect_report_names_event_limit_and_recovery(setup, capsys, monkeypatch):
    target, _, _ = setup
    monkeypatch.setattr(limits, "REPLAY_MAX_EVENTS", 0, raising=False)
    code, report = run(target, capsys, "run")
    assert code == 1
    assert "examined-event limit of 0" in report["reason"]
    assert "brigade memory hub pull" in report["reason"]
    assert "unbounded" in report["reason"]
