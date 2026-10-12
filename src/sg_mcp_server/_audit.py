"""Append-only JSONL security audit log."""

import datetime
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from sg_mcp_server._diagnostics import DiagnosticOutcome


class AuditLog:
    """Records every tool invocation; payloads are hashed, never stored.

    A failed audit write warns on standard error, and the read proceeds.
    """

    def __init__(self, path: Path, caller: str) -> None:
        """Store the target path and caller identity.

        No filesystem access happens here: directory creation lives in the
        guarded write path, so an unwritable location degrades to the
        documented warn-and-proceed behavior instead of aborting server
        startup.
        """
        self._path = path
        self._caller = caller

    def record_tool_call(
        self,
        tool: str,
        input_payload: Mapping[str, object],
        outcome: DiagnosticOutcome,
        duration_ms: float,
        entity_ids: list[int] | None = None,
    ) -> None:
        """Append one audit entry.

        Args:
            tool: Tool name as listed to clients.
            input_payload: Tool arguments to fingerprint: the default-filled
                arguments of a dispatched call, or the raw arguments of a
                rejected one. Only their SHA-256 is written.
            outcome: One of `ok`, `validation_error`, `query_error`,
                `unexpected_error`, `invalid_arguments`, or `unknown_tool`.
            duration_ms: Wall time of the call.
            entity_ids: Ids of entities the response touched, if known.
        """
        entry = {
            'ts': datetime.datetime.now(datetime.UTC).isoformat(),
            'caller': self._caller,
            'tool': tool,
            'input_sha256': hashlib.sha256(
                json.dumps(input_payload, sort_keys=True, default=str).encode()
            ).hexdigest(),
            'outcome': outcome,
            'duration_ms': round(duration_ms, 3),
            'entity_ids': entity_ids or [],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(entry) + '\n')
        except OSError as error:
            print(f'warning: audit write failed: {error}', file=sys.stderr)
