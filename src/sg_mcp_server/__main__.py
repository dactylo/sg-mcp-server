"""Serve the read-only solo-mode MCP tools over stdio.

Run `python -m sg_mcp_server` with `SG_MCP_SITE_URL`, `SG_MCP_SCRIPT_NAME`, and
`SG_MCP_API_KEY` set. The command uses the same optional environment settings
as `sg-mcp-server` and runs the server in the foreground.
"""

import sys

from sg_mcp_server._cli import main

if __name__ == '__main__':
    sys.exit(main())
