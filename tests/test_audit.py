import hashlib
import json
from pathlib import Path

import pytest

from sg_mcp_server._audit import AuditLog


class TestRecordToolCall:
    def test_appends_jsonl_with_caller_and_complete_payload_hash(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path / 'sub' / 'audit.jsonl', caller='sg-mcp-server-script')
        log.record_tool_call(
            'sg_query',
            {'secret': 'value', 'refresh_schema': True},
            'ok',
            12.5001,
            [1, 2],
        )
        log.record_tool_call('sg_get', {}, 'validation_error', 3.0)

        lines = (tmp_path / 'sub' / 'audit.jsonl').read_text().splitlines()
        assert len(lines) == 2

        first = json.loads(lines[0])
        assert set(first) == {
            'caller',
            'duration_ms',
            'entity_ids',
            'input_sha256',
            'outcome',
            'tool',
            'ts',
        }
        assert first['ts'].endswith('+00:00')
        assert first['caller'] == 'sg-mcp-server-script'
        assert first['tool'] == 'sg_query'
        assert (
            first['input_sha256']
            == hashlib.sha256(b'{"refresh_schema": true, "secret": "value"}').hexdigest()
        )
        assert first['outcome'] == 'ok'
        assert first['duration_ms'] == 12.5
        assert first['entity_ids'] == [1, 2]
        assert 'secret' not in lines[0]
        assert 'value' not in lines[0]
        assert 'refresh_schema' not in lines[0]

        second = json.loads(lines[1])
        assert second['tool'] == 'sg_get'
        assert second['outcome'] == 'validation_error'
        assert second['entity_ids'] == []

    def test_parent_creation_failure_warns_but_does_not_raise(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parent = tmp_path / 'blocked'
        parent.write_text('not a directory')
        log = AuditLog(parent / 'audit.jsonl', caller='c')

        log.record_tool_call('sg_query', {}, 'ok', 1.0)

        assert 'warning: audit write failed: ' in capsys.readouterr().err

    def test_write_failure_warns_but_does_not_raise(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / 'blocked'
        target.mkdir()
        log = AuditLog(target, caller='c')

        log.record_tool_call('sg_query', {}, 'ok', 1.0)

        assert 'warning: audit write failed: ' in capsys.readouterr().err
