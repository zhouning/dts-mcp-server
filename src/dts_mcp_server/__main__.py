"""Entry point: ``python -m dts_mcp_server`` or the ``dts-mcp-server`` script.

Runs over stdio, so it is launched as a subprocess by the MCP client rather than
listening on a port.
"""
from __future__ import annotations

from .server import mcp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
