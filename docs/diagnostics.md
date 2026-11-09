# Latency diagnostics

Enable latency diagnostics to find where a slow request spends its time. The server writes optional
timings and counters as one JSON line per completed request. Diagnostics are off by default,
contain no credentials, query arguments, or site data, and are separate from the security audit.
For initial setup, see the [README quick start](../README.md#quick-start).

| If you want to...                                  | Start here                                                      |
| -------------------------------------------------- | --------------------------------------------------------------- |
| Record timings for your own requests               | [Enable diagnostics](#enable-diagnostics)                       |
| Find the slow part of one request                  | [Read a request](#read-a-request)                               |
| Compare server time with client or model time      | [What each duration measures](#what-each-duration-measures)     |
| Measure repeatable timings against your site       | [Run the read-only live probe](#run-the-read-only-live-probe)   |
| Share diagnostic output safely                     | [Keep diagnostic files private](#keep-diagnostic-files-private) |
| Look up a record field, phase, or counter          | [Diagnostic record format](#diagnostic-record-format)           |
| Look up a field in the probe's `summary.json` file | [Probe summary format](#probe-summary-format)                   |

## Enable diagnostics

Add `SG_MCP_DIAGNOSTICS_PATH` to the server's `env` in your MCP client's configuration, alongside
the three required connection settings, then restart or reconnect the client:

```json
{
  "SG_MCP_SITE_URL": "https://yoursite.shotgunstudio.com",
  "SG_MCP_SCRIPT_NAME": "your-script-name",
  "SG_MCP_API_KEY": "your-script-key",
  "SG_MCP_DIAGNOSTICS_PATH": "/path/to/private/requests.jsonl"
}
```

Use an absolute path outside any Git repository, so the file is never committed by accident. The
server expands a leading `~` and refuses to start with a relative path. Diagnostics stay off when
the variable is unset, empty, or whitespace.

The server appends one line per completed request, including rejected ones. If measurement or
writing fails, it keeps serving and prints the corresponding warning to standard error once:

- `warning: diagnostic accounting failed`
- `warning: diagnostic write failed`

A write failure reported by the operating system adds its error text, such as a missing directory
or a denied permission. The tool result and the security audit are unaffected. Diagnostics never
replace or change the security audit at `SG_MCP_AUDIT_PATH`.

## Read a request

Ask a question through your MCP client, then display the latest record:

```sh
tail -n 1 /path/to/private/requests.jsonl
```

For example, a successful query record has this shape. These example durations are not a
performance target:

```json
{
  "schema_version": 1,
  "ts": "2000-01-01T03:23:02.319500+00:00",
  "request_id": 1,
  "tool": "sg_query",
  "outcome": "ok",
  "duration_ms": 2.380917,
  "phases_ms": {
    "argument_validation": 0.105584,
    "connection_lock_wait": 0.003042,
    "query_validation": 0.272291,
    "schema_cache_lookup": 0.021667,
    "schema_reflection": 0.005333,
    "provider_call": 0.011625,
    "rendering": 0.005708,
    "audit_write": 0.353875
  },
  "schema_cache": {
    "hits": 0,
    "misses": 1,
    "refreshes": 0
  },
  "provider_call_count": 3,
  "unattributed_ms": 1.601792
}
```

Check `outcome` first, then compare `duration_ms` with the largest values in `phases_ms`. In this
example, a cache miss requires two schema calls and one `find` call, giving
`provider_call_count: 3`.

Use the phases to decide where to look:

| What you see                    | What it means                                                                                                                                                                                   |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `outcome` is not `ok`           | The request failed or was rejected. The timings cover only the work done before it stopped.                                                                                                     |
| `schema_reflection` is present  | The server read the schema from SG, which takes two remote calls: on the first request, after the cache expires, on `refresh: true`, or when a request names something the cached schema lacks. |
| `connection_lock_wait` is large | The request waited for another one. The server shares one SG connection and runs one tool call at a time.                                                                                       |
| `provider_call` is large        | The time went to SG's remote `find` or `summarize` call, including the network.                                                                                                                 |
| `unattributed_ms` is large      | The time went to server work outside the named phases, such as dispatch and thread scheduling.                                                                                                  |

A phase the request never entered is absent; for example, a cache hit has no `schema_reflection`.
An absent or small phase describes this request only. See
[What each duration measures](#what-each-duration-measures) before comparing server, audit, and
client timings. To compare repeated requests, [run the probe](#run-the-read-only-live-probe).

## Run the read-only live probe

The probe measures repeated, comparable timings against your live site. It is a script in this
repository, so it needs a development checkout set up with `make setup`, which requires the uv
version listed in the [development guide](development.md#local-environment). Setup creates `.venv`
with the server's dependencies and the official SG client. The probe runs the checkout's own code,
not your installed `sg-mcp-server`.

From the repository root, with `SG_MCP_SITE_URL`, `SG_MCP_SCRIPT_NAME`, and `SG_MCP_API_KEY` set in
your environment, run:

```sh
.venv/bin/python scripts/diagnose_live_latency.py \
    --output-dir /path/to/private/latency-run-001 \
    --repeats 5
```

The command above uses the Linux/macOS environment path. On Windows, use `.venv/Scripts/python.exe`
instead of `.venv/bin/python`.

- `--output-dir` must name a new directory whose parent already exists. The probe refuses an
  existing directory and checks that the new one ends up holding exactly its own files. Choose a
  location outside the checkout so the output is never committed.
- `--repeats` sets how many times each operation runs, including the first sample. It accepts `2`
  through `20` and defaults to `5`.

The probe starts one server process, opens one MCP session, and lists the tools once. For each
repeat, it runs an `sg_schema` entity listing, an `sg_schema` description of Project, and an
`sg_query` for one Project. If the first query returns a Project, it then runs one `sg_get` for it.
It never refreshes the schema explicitly and makes no changes to your site. So that runs are
comparable regardless of your own settings, the probe's server uses a schema TTL of `3600` seconds,
a default limit of `50`, and a hard limit of `200`.

| Exit status | Meaning                                                                          |
| ----------- | -------------------------------------------------------------------------------- |
| `0`         | The run succeeded.                                                               |
| `1`         | A handled failure; standard error starts with `live latency diagnostic failed:`. |
| `2`         | Invalid command-line arguments.                                                  |

A successful run leaves exactly three files in the output directory:

- `requests.jsonl`: the server's diagnostic records.
- `summary.json`: the client-side timing summary, described in
  [Probe summary format](#probe-summary-format).
- `security-audit.jsonl`: the security audit for the probe's requests.

To compare runs, start with the `aggregates_ms` groups in `summary.json`. The `_warm` groups leave
out each operation's first sample, which can include one-time work such as the initial schema read.

## Keep diagnostic files private

`requests.jsonl` contains fixed tool names, fixed outcomes, timings, and bounded counters. It does
not contain credentials, site URLs, tool arguments or their hashes, schema or record data, record
IDs, rendered output, error text, file paths, or model data. `summary.json` keeps only the subset
of that data needed to validate and compare probe runs.

`security-audit.jsonl` is an ordinary security audit with the fields `ts`, `caller`, `tool`,
`input_sha256`, `outcome`, `duration_ms`, and `entity_ids`. The probe records its caller as
`live-diagnostic` instead of your script name, but the entity IDs still identify records on your
site. Treat the audit as private.

Keep these files out of version control, and review access permissions before you share a
diagnostics file or probe output directory.

## What each duration measures

The diagnostic record, the security audit, and the probe's summary each report a duration, and each
measures a different span:

| Measurement                            | Starts                                                                                | Stops                                                      |
| -------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| Diagnostic `duration_ms`               | When the server receives the request                                                  | After the result is complete and the audit write attempted |
| Security audit `duration_ms`           | After the request acquires the shared connection; on receipt for a rejected tool call | Before the audit write                                     |
| Probe `round_trip_ms` (`summary.json`) | Before the probe's MCP client sends the call                                          | When the call returns to the probe                         |

The diagnostic duration covers argument validation, connection-lock waiting, tool execution,
rendering, and the audit write, but not the append of the diagnostic record itself. The audit
duration is shorter: for a tool call that runs, it excludes argument validation and lock waiting.
Audit durations are rounded to three decimal places. The probe's round trip adds the local MCP
client library and stdio transport to the server's time, and the probe reports `process_spawn`,
`mcp_initialize`, and `tool_listing` as separate setup times.

None of these measure your MCP host or the model. The server cannot observe when the host sends a
request or processes a result, or how long the model spends reasoning. Gaps between records include
that unobserved time, so do not read host or model time from them.

## Diagnostic record format

Each diagnostic line has exactly these fields:

| Field                 | Type and meaning                                                                                                                                                                                                    |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`      | Integer `1`.                                                                                                                                                                                                        |
| `ts`                  | UTC ISO 8601 completion timestamp.                                                                                                                                                                                  |
| `request_id`          | Positive, server-generated, process-local integer that increases as enabled requests start. It is not derived from client input or returned in the MCP result. Concurrent requests may finish in a different order. |
| `tool`                | `sg_get`, `sg_query`, `sg_schema`, or `unknown` for an unrecognized tool name.                                                                                                                                      |
| `outcome`             | One fixed outcome described below.                                                                                                                                                                                  |
| `duration_ms`         | Time from server receipt through the completed tool result or protocol rejection.                                                                                                                                   |
| `phases_ms`           | Object containing the applicable exclusive phase durations. An unentered phase is absent.                                                                                                                           |
| `schema_cache`        | Object containing the nonnegative integer counters `hits`, `misses`, and `refreshes`.                                                                                                                               |
| `provider_call_count` | Nonnegative integer count of measured remote SG calls.                                                                                                                                                              |
| `unattributed_ms`     | Nonnegative server time outside the named phases.                                                                                                                                                                   |

`outcome` is one of:

- `ok`: the tool completed successfully.
- `validation_error`: query or schema validation rejected the request, including a missing argument
  that the requested operation needs.
- `query_error`: SG or query execution failed, or `sg_get` found no visible record with the
  requested ID.
- `unexpected_error`: an unexpected server error interrupted the request.
- `invalid_arguments`: the MCP tool arguments failed JSON Schema validation.
- `unknown_tool`: the requested tool name was not recognized.

The server measures elapsed time with a monotonic nanosecond clock. It converts `duration_ms`, each
`phases_ms` value, and `unattributed_ms` to milliseconds and rounds each to six decimal places. A
phase the request entered stays present even when its value rounds to `0.0`.

The values reconcile as:

```text
duration_ms = sum(phases_ms values) + unattributed_ms
```

Allow a difference of at most `0.00001` ms after rounding.

## Phases and counters

Repeated spans with the same name accumulate. Phases are exclusive: a nested phase pauses its
parent until the nested phase ends. Cache lookup therefore excludes schema reflection, and query
validation excludes cache work.

| Phase                  | Measures                                                                              |
| ---------------------- | ------------------------------------------------------------------------------------- |
| `argument_validation`  | JSON Schema validation of MCP tool arguments.                                         |
| `connection_lock_wait` | Time waiting to acquire the shared SG connection.                                     |
| `schema_cache_lookup`  | Cache access and bookkeeping, excluding nested schema reflection.                     |
| `schema_reflection`    | The two remote calls that read the field and entity schemas.                          |
| `query_validation`     | Validation and construction of an executable read query, excluding nested cache work. |
| `provider_call`        | A remote `find` or `summarize` call. Repeated calls accumulate.                       |
| `rendering`            | Conversion of the completed read result into MCP text and structured content.         |
| `audit_write`          | The security-audit append attempt.                                                    |

| Counter                  | Increases when...                                                                                                 |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------- |
| `schema_cache.hits`      | A lookup returns an unexpired schema without reading SG. One request can make multiple lookups.                   |
| `schema_cache.misses`    | A lookup finds no schema or an expired schema and reads SG.                                                       |
| `schema_cache.refreshes` | A request explicitly refreshes the schema, or an unknown name triggers a refresh. A refresh is not also a miss.   |
| `provider_call_count`    | A measured `schema_read`, `schema_entity_read`, `find`, or `summarize` call runs. One schema read adds two calls. |

`unattributed_ms` includes other server work, such as dispatch and thread scheduling. The
diagnostic-file append stays outside `duration_ms`.

## Probe summary format

`summary.json` has exactly these top-level fields:

| Field                | Type and meaning                                                                           |
| -------------------- | ------------------------------------------------------------------------------------------ |
| `schema_version`     | Integer `1`.                                                                               |
| `repeats`            | Selected integer repeat count.                                                             |
| `schema_ttl_seconds` | Integer `3600`.                                                                            |
| `default_limit`      | Integer `50`.                                                                              |
| `hard_limit`         | Integer `200`.                                                                             |
| `tool_names`         | Exact list `["sg_get", "sg_query", "sg_schema"]`.                                          |
| `setup_ms`           | Setup timing object described below.                                                       |
| `operation_counts`   | Object with exact `schema`, `describe`, `query`, and `get` sample counts, described below. |
| `samples`            | Ordered list of successful operation samples described below.                              |
| `aggregates_ms`      | Client round-trip statistics described below.                                              |
| `project_found`      | Boolean indicating whether the first query returned a Project.                             |
| `get_attempted`      | Boolean equal to `project_found`.                                                          |
| `get_ok`             | `true` after a successful conditional get, or `null` when no get was attempted.            |
| `valid`              | `true`; written only after every response and diagnostic record passes the probe's checks. |

`setup_ms` contains `process_spawn`, `mcp_initialize`, and `tool_listing`. Each value is a
nonnegative millisecond measurement rounded to six decimal places.

`operation_counts` contains `schema`, `describe`, `query`, and `get`. The `schema`, `describe`, and
`query` counts each equal `repeats`. The `get` count is `1` when `project_found` is true and `0`
otherwise.

Each `samples` object contains:

- `operation`: `schema`, `describe`, `query`, or `get`.
- `ordinal`: the positive, one-based sample number for that operation.
- `round_trip_ms`: client-observed MCP call time.
- `server_ms`: the matching server `duration_ms`.
- `phases_ms`: the matching server phase object.
- `unattributed_ms`: the matching server remainder.
- `schema_cache`: the matching `hits`, `misses`, and `refreshes` counters.
- `provider_call_count`: the matching provider counter.
- `ok`: `true` for a validated successful sample.

All sample timing values are nonnegative milliseconds with at most six decimal places. The
`server_ms`, `phases_ms`, and `unattributed_ms` values reconcile within `0.00001` ms.

`aggregates_ms` contains `schema_all`, `schema_warm`, `describe_all`, `describe_warm`, `query_all`,
and `query_warm`. Each group contains `minimum`, `median`, and `maximum` of its client
`round_trip_ms` samples. Warm groups exclude the first sample. The probe does not aggregate `get`
because it runs at most once.
