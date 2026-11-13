"""Closed runtime schema for the generic connector."""

from dataclasses import replace
from .runtime_config import (
    AuthConfig,
    Runtime,
    CLOUD_AGENT_RUNTIME_SCHEMA,
    _object,
    _invalid,
    _parse_auth,
    _parse_hosted_paths,
    _parse_hub,
    _parse_notify,
    _boolean,
    _absolute_path,
)
from .token_auth import read_token


def parse_cloud_runtime(payload: object) -> Runtime:
    root = _object(payload, {"schema", "auth", "paths", "hub"}, optional={"notify_command", "messaging_enabled"})
    if root["schema"] != CLOUD_AGENT_RUNTIME_SCHEMA:
        raise _invalid()
    raw = root["auth"]
    if not isinstance(raw, dict):
        raise _invalid()
    mode = raw.get("mode", "token")
    if mode == "token":
        data = _object(raw, {"token_file"}, optional={"mode"})
        token_file = _absolute_path(data["token_file"])
        read_token(token_file)
        auth = AuthConfig("", "", "", "agent:cloud-agent", "", mode="token", token_file=token_file)
    elif mode == "access":
        auth = replace(_parse_auth({k: v for k, v in raw.items() if k != "mode"}), mode="access")
    else:
        raise _invalid()
    paths = _parse_hosted_paths(root["paths"])
    hub = _parse_hub(root["hub"])
    if hub.principal != "agent:cloud-agent":
        raise _invalid()
    if auth.token_file is not None:
        from .lifecycle import _same_location, _same_token

        if _same_location(auth.token_file, hub.message_token_file) or _same_token(
            auth.token_file, hub.message_token_file
        ):
            raise _invalid()
    return Runtime(
        auth=auth,
        paths=paths,
        hub=hub,
        dispatch_enabled=False,
        events_enabled=False,
        digest_pr_repo="",
        callback_hosts=(),
        notify_command=_parse_notify(root),
        owner_authorization=None,
        messaging_enabled=_boolean(root.get("messaging_enabled", False)),
        schema=CLOUD_AGENT_RUNTIME_SCHEMA,
        memory_push=False,
    )
