"""Two independent secrets: one for control, one for data transfer.

The bearer token authenticates MCP calls. The signing key authorizes artifact
uploads and downloads. Keeping them separate means a leaked transfer URL grants
access to exactly one artifact for one TTL window, not to the whole API — so
signed URLs must never carry the bearer token.
"""
from __future__ import annotations

import hashlib
import hmac
import time

from mcp.server.auth.provider import AccessToken


class StaticBearerVerifier:
    """Constant-time bearer check.

    Implements the mcp.server.auth.provider.TokenVerifier protocol, which in
    MCP SDK 2.0 is a Protocol rather than a base class.
    """

    def __init__(self, expected_token: str) -> None:
        self._expected = expected_token.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token.encode("utf-8"), self._expected):
            return None
        return AccessToken(
            token=token,
            client_id="dts-mcp-client",
            scopes=[],
            claims={"auth_method": "static_bearer"},
        )


class ArtifactSigner:
    """HMAC-SHA256 over ``operation:artifact_id:expires``.

    Binding the operation into the MAC is what stops an upload URL being
    replayed as a download URL.
    """

    def __init__(self, key: bytes) -> None:
        self._key = key

    def sign(self, operation: str, artifact_id: str, expires_at: int) -> str:
        payload = f"{operation}:{artifact_id}:{expires_at}".encode()
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def verify(
        self,
        operation: str,
        artifact_id: str,
        expires_at: int,
        signature: str,
    ) -> bool:
        # Expiry is checked before the MAC compare so an expired URL cannot be
        # distinguished from a forged one by timing.
        if expires_at < int(time.time()):
            return False
        expected = self.sign(operation, artifact_id, expires_at)
        return hmac.compare_digest(expected, signature)
