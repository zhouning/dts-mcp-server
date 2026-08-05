"""Entry point.

Refuses to start without a certificate and key, so a misconfigured host never
briefly serves the control plane in plaintext.
"""
from __future__ import annotations

import sys

import uvicorn

from .app import create_application
from .config import Settings
from .host_address import AddressResolutionError, resolve_server_ip


def run_stdio() -> None:
    """Serve over stdio for a client on this same machine.

    No TLS, no bearer token, and no artifact transfer: a local client passes host
    paths directly, so the data plane is unnecessary. Remote clients must use the
    HTTPS mode instead.
    """
    from .stdio_server import mcp

    mcp.run(transport="stdio")


def main() -> None:
    if "--stdio" in sys.argv[1:]:
        run_stdio()
        return

    settings = Settings()

    if not settings.server_ip:
        try:
            settings.server_ip = resolve_server_ip()
        except AddressResolutionError as error:
            print(f"error: {error}", file=sys.stderr)
            raise SystemExit(2) from error

    missing = [
        str(path)
        for path in (settings.tls_cert, settings.tls_key)
        if not path.is_file()
    ]
    if missing:
        print(
            "error: TLS material is missing:\n  "
            + "\n  ".join(missing)
            + "\n\nRun scripts/bootstrap_tls.ps1 to generate it. The gateway "
              "will not serve the control plane without TLS.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    print(f"dts-mcp-server on {settings.base_url}/mcp", flush=True)
    uvicorn.run(
        create_application(settings),
        host=settings.bind_host,
        port=settings.bind_port,
        ssl_certfile=str(settings.tls_cert),
        ssl_keyfile=str(settings.tls_key),
        log_level="info",
    )


if __name__ == "__main__":
    main()
