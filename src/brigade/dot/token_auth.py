"""Protected bearer authentication for the generic connector only."""

import hmac
import re
from pathlib import Path
from typing import Any, Mapping

from .access_auth import AuthError, bounded_headers
from .contracts import DotError
from .runtime_config import AuthConfig


def read_token(path: Path) -> bytes:
    from brigade.fleet_client_message_credentials import read_message_token_file

    try:
        value = read_message_token_file(str(path)).encode("ascii")
        if not re.fullmatch(rb"[A-Za-z0-9._~+/-]+=*", value):
            raise ValueError
        return value
    except Exception:
        raise DotError("invalid_config") from None


class TokenVerifier:
    """No network fallback, no stored token value and fixed refusal diagnostics."""

    def __init__(self, auth: AuthConfig):
        if auth.mode != "token" or auth.token_file is None:
            raise DotError("invalid_config")
        read_token(auth.token_file)
        self.config = auth
        self.validated = True
        self.metadata_url = self.resource = self.authorization_server = ""

    async def verify(self, scope: Mapping[str, Any]) -> str:
        try:
            values = [v for k, v in bounded_headers(scope) if k.lower() == b"authorization"]
            if len(values) != 1 or not re.fullmatch(rb"Bearer [A-Za-z0-9._~+/-]+=*", values[0]):
                raise ValueError
            assert self.config.token_file is not None
            if not hmac.compare_digest(values[0][7:], read_token(self.config.token_file)):
                raise ValueError
            return self.config.approved_email
        except Exception:
            raise AuthError() from None
