"""Provide the read-only MCP server for Flow Production Tracking.

Run `sg-mcp-server`, run `python -m sg_mcp_server`, or call `main()` to serve
the read-only MCP tools over stdio in the foreground. `main()` parses
`sys.argv` unless given an argument list, accepts no arguments other than
`--help` and `--version`, and returns the process exit status. `--help` and
invalid arguments end in `SystemExit` from argument parsing; `--version`
returns 0 after printing the version, or 2 with an error when the installed
distribution metadata is unavailable.

Set `SG_MCP_SITE_URL`, `SG_MCP_SCRIPT_NAME`, and `SG_MCP_API_KEY` for the site
connection. Optional `SG_MCP_TIMEZONE`, `SG_MCP_AUDIT_PATH`,
`SG_MCP_DEFAULT_LIMIT`, `SG_MCP_HARD_LIMIT`, `SG_MCP_SCHEMA_TTL_SECONDS`,
`SG_MCP_TIMEOUT_SECONDS`, and `SG_MCP_DIAGNOSTICS_PATH` settings control
rendering, logging, query limits, the SG request timeout, and schema caching.
"""

from sg_mcp_server._cli import main

__all__ = ['main']
