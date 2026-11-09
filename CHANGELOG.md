# Changelog

## 0.2.0 (2026-11-08)

### Breaking changes

- The server no longer starts when `SG_MCP_AUDIT_PATH` or `SG_MCP_DIAGNOSTICS_PATH` is a relative
  path. Set each to an absolute path; a leading `~` now expands to your home directory.

### Added

- `sg_schema` search accepts `offset` and reports the `total`, so clients can page through more
  than 40 matches.

### Changed

- `sg_query` and `sg_schema` errors say how to fix a malformed filter condition or a missing
  `entity_type` or `query`.

### Security

- Text from calculated, query, and pivot fields now comes back between the untrusted-text markers,
  like free-text fields, so clients treat it as data, not instructions.

## 0.1.0 (2026-10-11)

Initial release.
