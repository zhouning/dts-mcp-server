"""Gateway settings.

Loaded from the environment, or from a ``server.env`` file written by
``scripts/bootstrap.ps1``. Secrets carry a minimum length so a blank or
truncated value is a startup validation error rather than a silent weakness.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Default install root for state that must survive a reboot.
DEFAULT_STATE_DIR = Path.home() / "AppData" / "Local" / "DtsMCP"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DTS_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- network -----------------------------------------------------------
    bind_host: str = "0.0.0.0"
    bind_port: int = 8770
    #: Address clients dial. Left unset, host_address.resolve_server_ip()
    #: derives it; the certificate SAN and firewall scope use the same value.
    server_ip: str | None = None
    public_base_url: str | None = None

    # --- secrets -----------------------------------------------------------
    #: Authenticates MCP control-plane calls.
    bearer_token: SecretStr = Field(min_length=32)
    #: Signs artifact transfer URLs. Deliberately separate from bearer_token so
    #: rotating one does not invalidate the other, and so a leaked transfer URL
    #: grants one artifact rather than the whole API.
    artifact_signing_key: SecretStr = Field(min_length=32)

    # --- TLS ---------------------------------------------------------------
    tls_cert: Path = DEFAULT_STATE_DIR / "certs" / "server.crt"
    tls_key: Path = DEFAULT_STATE_DIR / "certs" / "server.key"
    tls_ca_cert: Path = DEFAULT_STATE_DIR / "certs" / "ca.crt"
    tls_ca_key: Path = DEFAULT_STATE_DIR / "certs" / "ca.key"

    # --- storage -----------------------------------------------------------
    state_dir: Path = DEFAULT_STATE_DIR
    artifact_dir: Path = DEFAULT_STATE_DIR / "artifacts"
    workspace_dir: Path = DEFAULT_STATE_DIR / "workspace"
    database_path: Path = DEFAULT_STATE_DIR / "state.sqlite3"

    # --- transfer limits ---------------------------------------------------
    #: 3DT tiles and OSGB inputs are large; these are generous but finite.
    max_upload_bytes: int = 20 * 1024**3
    max_expanded_bytes: int = 50 * 1024**3
    max_zip_entries: int = 100_000
    max_zip_depth: int = 16
    signed_url_ttl_seconds: int = 600

    # --- retention ---------------------------------------------------------
    retention_days: int = 7
    #: Abandoned .part files are the disk-exhaustion vector, so they expire far
    #: sooner than completed artifacts.
    incomplete_upload_hours: int = 24

    # --- engine ------------------------------------------------------------
    #: Passed through to the harness; None lets it discover the install.
    #: Accepts the un-prefixed DTS_ENGINE_DIR as well, since that is what the
    #: harness, the CLI and the stdio server all read — one install, one name.
    engine_dir: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("DTS_MCP_ENGINE_DIR", "DTS_ENGINE_DIR"),
    )
    job_timeout_seconds: int = 3600

    @property
    def base_url(self) -> str:
        """Public URL clients dial, used for signed URLs and the cert SAN."""
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        host = self.server_ip or "127.0.0.1"
        return f"https://{host}:{self.bind_port}"

    def ensure_directories(self) -> None:
        for path in (
            self.state_dir, self.artifact_dir, self.workspace_dir,
            self.tls_cert.parent, self.database_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
