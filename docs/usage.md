# Using the server

This guide is for people asking questions through sg-mcp-server or configuring it. It shows the
kinds of questions to ask, the tool arguments behind them, every setting, and what to do when a
request fails. If you have not installed the server yet, start with the
[README quick start](../README.md#quick-start).

The server connects your MCP client to one SG site through a configured script. Every answer
reflects that script's permissions, not those of the person asking.

| If you want to...                                                  | Start here                                  |
| ------------------------------------------------------------------ | ------------------------------------------- |
| Ask questions in plain language                                    | [Ask about your site](#ask-about-your-site) |
| Call a tool directly                                               | [Tool-call examples](#tool-call-examples)   |
| Change the time zone, limits, timeout, audit path, or schema cache | [Settings](#settings)                       |
| Fix a server that will not start or a failed request               | [Troubleshooting](#troubleshooting)         |

## Ask about your site

You can ask questions in plain language without writing tool arguments. For example:

- "What entity types are in this site's schema?" The client can call `sg_schema` with
  `op: "entities"` and explain the listed types.
- "Which fields can I use for Shot status and due dates?" The client can describe Shot with
  `sg_schema` and name fields that exist on your site.
- "Show up to 10 shots with status in progress, including their codes." The client can check the
  schema, then use `sg_query` with a status filter and `limit: 10`. Your site's status codes and
  the records the script can read determine the answer.
- "Open Shot 123 and show its code." If that ID exists and the script can read it, `sg_get` returns
  the requested fields and a link to the record in SG.

These examples describe the kind of answer to expect; they do not imply that particular records or
status values exist. When a field or status is uncertain, ask the client to inspect the schema
first.

Values of free-text fields, such as descriptions and note bodies, come back between
`[BEGIN UNTRUSTED SG TEXT]` and `[END UNTRUSTED SG TEXT]` markers. Schema listings and linked
record names carry a similar notice. Anyone who can edit your site can write that text, so the
markers ask the client to treat it as data, not as instructions.

## Tool-call examples

If your MCP client lets you call tools directly, these arguments show the supported shapes. The
fields must exist on your site and be readable by the script. Each tool's description, which the
client receives from the server, gives the complete argument rules.

### Explore the schema with `sg_schema`

List the site's entity types:

```json
{
  "op": "entities"
}
```

Describe the fields on one entity:

```json
{
  "op": "describe",
  "entity_type": "Shot"
}
```

`op: "search"` with a `query` matches field names and labels by case-insensitive substring and
returns at most 40 matches; add `entity_type` to search one entity. Entity and field listings
return 100 entries per page, and a longer listing tells the client which `offset` to request next.

The server caches the schema for 15 minutes by default (see [Settings](#settings)). Add
`"refresh": true` to re-read it from your site, for example after adding a field. The schema
reflects site-level configuration, so what a particular project shows, and which records the script
can read there, may differ.

### Find records with `sg_query`

Find up to ten Projects and request their names:

```json
{
  "entity_type": "Project",
  "fields": [
    "name"
  ],
  "limit": 10
}
```

Filter with native SG conditions, written as `[field, operator, value]`:

```json
{
  "entity_type": "Shot",
  "filters": [
    [
      "sg_status_list",
      "is",
      "ip"
    ]
  ],
  "fields": [
    "code"
  ],
  "limit": 10
}
```

`ip` is a common status code for "in progress", but your site's codes may differ. `sg_query` also
accepts ordering, nested `all` and `any` filter groups, and relative dates such as
`["updated_at", "in_last", 7, "DAY"]`. The server evaluates relative dates in the configured
`SG_MCP_TIMEZONE`.

Count records instead of listing them with a `summarize` block:

```json
{
  "entity_type": "Shot",
  "summarize": {
    "summary_fields": [
      {
        "field": "id",
        "type": "count"
      }
    ],
    "grouping": [
      {
        "field": "sg_status_list",
        "direction": "asc"
      }
    ]
  }
}
```

A summary takes one summary field per call. It cannot be combined with selected `fields`, `order`,
a positive `limit`, or `retired_only: true`, because SG's native summary call has none of those
options.

Query results are bounded by the configured limits. When more records match, the result reports
that it is truncated; narrow the filters or request a summary instead of treating the returned page
as every match.

### Open one record with `sg_get`

Get one Project after you have its numeric ID:

```json
{
  "entity_type": "Project",
  "entity_id": 123,
  "fields": [
    "name"
  ]
}
```

`fields` is required. The result includes the record's web URL.

## Settings

Set these environment variables in the MCP client's private server configuration:

| Variable                    | Default                        | Purpose                                                                |
| --------------------------- | ------------------------------ | ---------------------------------------------------------------------- |
| `SG_MCP_SITE_URL`           | Required                       | HTTPS site address, with no credentials, path, query, or fragment.     |
| `SG_MCP_SCRIPT_NAME`        | Required                       | Script name used for all requests.                                     |
| `SG_MCP_API_KEY`            | Required                       | API key for that script.                                               |
| `SG_MCP_TIMEZONE`           | `UTC`                          | IANA time zone for rendered dates and relative-date filters.           |
| `SG_MCP_AUDIT_PATH`         | `~/.sg-mcp-server/audit.jsonl` | Security audit JSONL file.                                             |
| `SG_MCP_DIAGNOSTICS_PATH`   | Disabled                       | Optional [latency diagnostics](diagnostics.md) JSONL file.             |
| `SG_MCP_DEFAULT_LIMIT`      | `50`                           | Row limit when a query omits one.                                      |
| `SG_MCP_HARD_LIMIT`         | `200`                          | Maximum row limit; larger requests are reduced to it.                  |
| `SG_MCP_SCHEMA_TTL_SECONDS` | `900`                          | How long, in seconds, the server reuses its cached copy of the schema. |
| `SG_MCP_TIMEOUT_SECONDS`    | `60`                           | Seconds to wait for the SG site to accept a connection or send data.   |

The limits must satisfy `1 <= SG_MCP_DEFAULT_LIMIT <= SG_MCP_HARD_LIMIT`. A query with `limit: 0`
gets the default limit, not unlimited results as it would through SG directly, so every result
stays bounded.

Give `SG_MCP_AUDIT_PATH` and `SG_MCP_DIAGNOSTICS_PATH` absolute paths. The server does not expand
`~`, and a relative path depends on the directory your client starts the server in. An unset or
empty diagnostics path leaves diagnostics off; it does not turn off the security audit. A blank
audit path stops startup instead, because it would point the audit at the working directory; unset
`SG_MCP_AUDIT_PATH` to use the default file. The server tries to append an audit entry for every
tool call. If an audit write fails, the server warns on standard error and still answers the
request, so that call is missing from the audit.

The server validates every request against a cached copy of the schema; reading the schema from SG
takes two remote calls. When a request names an entity type or field that the cached copy lacks,
the server re-reads the schema once before reporting an error, so new names work right away. A
removed or changed field can cause a query error until the cache expires or the client requests
`refresh: true`. A TTL of `0` or less re-reads the schema on every request.

`SG_MCP_TIMEOUT_SECONDS` must be a whole number of at least 1. It applies to each connection
attempt and each wait for data from SG, not to a whole request, so a slow but progressing response
is not cut off. When SG does not respond in time, or the connection or TLS handshake fails, the
request returns an error that starts with `the SG site request failed`, and the next request can
proceed. Raise the value if a slow site needs longer.

## Troubleshooting

**The server does not start.** Check that the client runs `sg-mcp-server` with no arguments and
passes all three required settings; `sg-mcp-server --help` lists every setting. When the server
cannot start, it writes one line to standard error, such as
`error: the SG_MCP_SITE_URL setting is required`, and exits with status 2. It stops this way when a
required setting is missing, the site URL is not a bare HTTPS origin, `SG_MCP_TIMEZONE` is not an
available IANA zone such as `UTC` or `America/Los_Angeles`, `SG_MCP_AUDIT_PATH` is set but blank, a
numeric setting is not an integer, `SG_MCP_TIMEOUT_SECONDS` is less than 1, the limits are out of
order, or the client started it without standard output. If your client keeps a log for MCP
servers, the message appears there. A server that exits with status 0 stopped normally, because the
client disconnected or closed its input.

**`error: the SG client is not installed`.** The install omitted the official SG client. Reinstall
from the checkout with `uv tool install --reinstall '.[official]'`.

**The first question fails.** The server does not contact your site until the first tool call, so a
wrong script name or key, or an unreachable site, shows up there rather than at startup.

**`the SG site request failed`.** The server could not reach SG or SG did not respond within
`SG_MCP_TIMEOUT_SECONDS`. Check the site address, the network or VPN, and SG's status, then retry.

**An entity, field, or filter error.** Ask the client to inspect the live schema and retry with a
valid name. The error lists valid alternatives; the server never substitutes a different field for
the one requested.

**`sg_get` reports `no results`.** The query was valid, but no record with that ID is visible: it
does not exist or the script's permissions hide it.

**A request is slow.** Enable [latency diagnostics](diagnostics.md) to see where the server spends
the time.
