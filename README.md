# sg-mcp-server

sg-mcp-server lets an AI assistant answer questions about your Autodesk Flow Production Tracking
site. Connect it to an MCP (Model Context Protocol) client, ask in plain language, and the
assistant looks up your site's live schema and records instead of guessing at entity types, field
names, or status values.

The server gives the client three read-only tools over MCP stdio: `sg_schema` explores entity types
and fields, `sg_query` runs bounded queries, and `sg_get` opens one record with its web link. Every
query is checked against your site's schema first: a misspelled field name returns a list of valid
alternatives and is never silently replaced.

This documentation uses SG for Flow Production Tracking, formerly named Shotgun and ShotGrid.

## Security model

sg-mcp-server is read-only: it has no tools that create, change, or delete records. Every request
runs as one SG script (`ApiUser`) and sees what that script's permissions allow. The server does
not act as the person asking the question, so anyone who can use the MCP client can read anything
the script can read. Give the script access only to the data you intend to expose, and keep its key
in your client's private configuration.

The server appends every tool call to a local security audit, `~/.sg-mcp-server/audit.jsonl` by
default. Each entry records the script name, tool, outcome, duration, and returned record IDs, with
a hash of the request instead of its contents. If an audit write fails, the server warns on
standard error and still answers, so that call is missing from the audit.

An assistant can still misread a result or answer from incomplete data. In the
[interface study](docs/benchmarks.md), no tested model answered every task correctly, so check
important answers against the records the tools return.

## Installation

You need:

- Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).
- An MCP client that can start a local stdio server.
- An SG site reachable from this machine, with a script name and API key that can read the data you
  want to ask about.

From a checkout of this repository, install the command with the official SG client:

```sh
uv tool install '.[official]'
```

The `official` extra installs `shotgun-api3`, the Autodesk Python API the server uses to reach your
site. The base package does not include an SG client; without one, the server stops at startup with
an error that names the `official` extra.

After updating the checkout, update the installed command:

```sh
uv tool install --reinstall '.[official]'
```

`make install` and `make reinstall` run the same commands. Run `sg-mcp-server --version` to check
the installed version. If your MCP client cannot find `sg-mcp-server`, give it the full path in the
directory that `uv tool dir --bin` prints.

## Quick start

### Add the server to your MCP client

Add a stdio server to your client's MCP configuration. This example uses the common `mcpServers`
JSON shape; place it where your client expects MCP servers:

```json
{
  "mcpServers": {
    "sg": {
      "command": "sg-mcp-server",
      "env": {
        "SG_MCP_SITE_URL": "https://yoursite.shotgunstudio.com",
        "SG_MCP_SCRIPT_NAME": "your-script-name",
        "SG_MCP_API_KEY": "your-script-key"
      }
    }
  }
}
```

Replace the three values with your site's address, script name, and API key. `SG_MCP_SITE_URL` must
be the bare HTTPS site address, with no path, query, or fragment. Restart or reconnect the client
so that it starts the server; the server runs until the client disconnects.

The server checks its settings when it starts and stops with a one-line `error:` message if one is
missing or invalid; [Troubleshooting](docs/usage.md#troubleshooting) lists the causes. It does not
contact your site until the first tool call, so a wrong key or an unreachable site appears as an
error on your first question, not as a startup failure.

### Ask your first question

Ask the assistant:

```text
Which projects can this script see?
```

The assistant typically calls `sg_schema` to confirm the Project entity, then `sg_query` to list
Projects, and answers with project names from your site. A list of names means the server is
working. The exact answer depends on your site's records and the script's permissions.

Next, ask "What fields are available on Shot?" to explore your site's schema before asking about
site-specific fields. [Using the server](docs/usage.md) has more example questions, direct
tool-call arguments, and every setting.

## What the tools do

| Tool        | Use it for                                                                                    |
| ----------- | --------------------------------------------------------------------------------------------- |
| `sg_schema` | List entity types, describe one entity's fields, or search field names and labels.            |
| `sg_query`  | Find records with validated filters, sorting, and a row limit, or request one native summary. |
| `sg_get`    | Fetch one record by entity type and numeric ID, including its web URL.                        |

A query returns at most 50 records unless it asks for a different limit, and never more than 200;
you can change both limits. When more records match, the result says so and suggests narrowing the
filters or requesting a summary, so the assistant does not mistake one page for every match.

## Documentation

| If you want to...                                                  | Read                                             |
| ------------------------------------------------------------------ | ------------------------------------------------ |
| Find example questions and tool-call arguments                     | [Using the server](docs/usage.md)                |
| Change the time zone, limits, timeout, audit path, or schema cache | [Settings](docs/usage.md#settings)               |
| Fix a request that fails or a server that will not start           | [Troubleshooting](docs/usage.md#troubleshooting) |
| Find out where a slow request spends its time                      | [Latency diagnostics](docs/diagnostics.md)       |
| Understand the three-tool design and model reliability             | [Interface study](docs/benchmarks.md)            |
| See changes by version                                             | [Changelog](CHANGELOG.md)                        |

## Development

See the [development guide](docs/development.md) for the required tools, CI, and dependency locks.
With those tools installed, run:

```sh
make setup
make check
```

The checks never contact a live SG site.

## License

sg-mcp-server is available under the [MIT License](LICENSE). It is an independent project and is
not affiliated with or endorsed by Autodesk. Flow Production Tracking is a product of Autodesk,
Inc.
