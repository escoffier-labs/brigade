"""Scripted stand-in for `codex app-server` used by test_codex_appserver.py.

Speaks newline-delimited JSON-RPC on stdio. Turn behavior is selected by the
prompt text of turn/start: HANG, APPROVAL, DIE, NOISE, or normal completion.
"""

from __future__ import annotations

import json
import sys

_next_thread = 0


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _notify(method: str, params: dict) -> None:
    _send({"jsonrpc": "2.0", "method": method, "params": params})


def _thread_payload(thread_id: str) -> dict:
    return {
        "id": thread_id,
        "cliVersion": "0.0.0-fake",
        "createdAt": 0,
        "updatedAt": 0,
        "cwd": "/tmp",
        "ephemeral": False,
        "modelProvider": "fake",
        "preview": "",
        "sessionId": "fake-session",
        "source": "fake",
        "status": {"type": "idle"},
        "turns": [],
    }


def _agent_item(item_id: str, text: str) -> dict:
    return {"id": item_id, "type": "agentMessage", "text": text}


def _complete_turn(thread_id: str, turn_id: str, status: str, text: str | None) -> None:
    items = [_agent_item(f"{turn_id}-msg", text)] if text is not None else []
    for item in items:
        _notify(
            "item/completed",
            {"threadId": thread_id, "turnId": turn_id, "completedAtMs": 0, "item": item},
        )
    _notify(
        "turn/completed",
        {"threadId": thread_id, "turn": {"id": turn_id, "items": items, "status": status}},
    )


def main() -> int:
    global _next_thread
    pending_hang: dict[str, str] = {}  # turnId -> threadId
    pending_approval: dict[int, tuple[str, str]] = {}  # request id -> (threadId, turnId)
    server_req_id = 1000

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        msg_id = msg.get("id")

        if msg_id is not None and method is None:
            # Response to a server->client request (approval decision).
            entry = pending_approval.pop(msg_id, None)
            if entry is not None:
                thread_id, turn_id = entry
                decision = (msg.get("result") or {}).get("decision")
                _complete_turn(thread_id, turn_id, "completed", f"approval:{decision}")
            continue

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"userAgent": "fake/0.0.0"}})
        elif method == "initialized":
            pass
        elif method in ("thread/start", "thread/resume"):
            thread_id = msg["params"].get("threadId")
            if thread_id is None:
                _next_thread += 1
                thread_id = f"t-{_next_thread}"
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"thread": _thread_payload(thread_id)}})
        elif method == "turn/start":
            params = msg["params"]
            thread_id = params["threadId"]
            prompt = params["input"][0]["text"]
            turn_id = f"turn-{thread_id}"
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"turn": {"id": turn_id, "items": [], "status": "inProgress"}},
                }
            )
            _notify("turn/started", {"threadId": thread_id, "turn": {"id": turn_id, "items": [], "status": "inProgress"}})
            if "HANG" in prompt:
                _notify(
                    "item/agentMessage/delta",
                    {"threadId": thread_id, "turnId": turn_id, "itemId": f"{turn_id}-msg", "delta": "partial "},
                )
                _notify(
                    "item/agentMessage/delta",
                    {"threadId": thread_id, "turnId": turn_id, "itemId": f"{turn_id}-msg", "delta": "answer"},
                )
                pending_hang[turn_id] = thread_id
            elif "APPROVAL" in prompt:
                server_req_id += 1
                pending_approval[server_req_id] = (thread_id, turn_id)
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": server_req_id,
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": thread_id, "turnId": turn_id, "itemId": "cmd-1"},
                    }
                )
            elif "DIE" in prompt:
                return 1
            else:
                if "NOISE" in prompt:
                    _notify("totally/unknown", {"threadId": thread_id, "mystery": True})
                _notify(
                    "item/started",
                    {"threadId": thread_id, "turnId": turn_id, "item": {"id": "cmd-0", "type": "commandExecution"}},
                )
                _complete_turn(thread_id, turn_id, "completed", f"result for: {prompt}")
        elif method == "turn/interrupt":
            turn_id = msg["params"]["turnId"]
            thread_id = pending_hang.pop(turn_id, msg["params"]["threadId"])
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            _complete_turn(thread_id, turn_id, "interrupted", None)
        else:
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
