#!/usr/bin/env python3
"""Run upstream python-substack MCP with durable local sessions."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from farm.substack_connection import SubstackConnection
from substack_mcp import mcp_server


def main():
    connection = SubstackConnection()
    mcp_server.get_api = connection.get_api

    @mcp_server.mcp.tool()
    def connection_status() -> dict:
        """Read local connection setup and the latest publication receipt."""
        return connection.status()

    mcp_server.main()


if __name__ == "__main__":
    main()
