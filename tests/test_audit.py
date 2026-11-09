import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Never

import pytest
from support import FixtureConnection

from sg_mcp_server._audit import AuditLog
from tests.server._helpers import build_test_server, call


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

    def test_audit_and_warning_failures_do_not_raise(
        self,
        tmp_path: Path,
        unavailable_stderr: io.StringIO,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / 'blocked'
        target.mkdir()
        log = AuditLog(target, caller='c')

        with monkeypatch.context() as warning_output:
            warning_output.setattr(sys, 'stderr', unavailable_stderr)
            log.record_tool_call('sg_query', {}, 'ok', 1.0)

    def test_parent_creation_failure_warns_but_does_not_raise(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parent = tmp_path / 'blocked'
        parent.write_text('not a directory')
        log = AuditLog(parent / 'audit.jsonl', caller='c')

        log.record_tool_call('sg_query', {}, 'ok', 1.0)

        assert 'warning: audit write failed: ' in capsys.readouterr().err

    def test_tool_answers_when_audit_and_warning_fail(
        self,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        unavailable_stderr: io.StringIO,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / 'blocked'
        target.mkdir()
        shot = fixture_site.create('Shot', {'code': 'AUDIT_OK'})
        server = build_test_server(fixture_site, tmp_path, audit_path=target)

        with monkeypatch.context() as warning_output:
            warning_output.setattr(sys, 'stderr', unavailable_stderr)
            result = call(
                server,
                'sg_get',
                {'entity_type': 'Shot', 'entity_id': shot['id'], 'fields': ['code']},
            )

        assert result.is_error is False
        assert result.structured_content is not None
        assert result.structured_content['records'][0]['id'] == shot['id']

    def test_write_failure_warns_but_does_not_raise(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / 'blocked'
        target.mkdir()
        log = AuditLog(target, caller='c')

        log.record_tool_call('sg_query', {}, 'ok', 1.0)

        assert 'warning: audit write failed: ' in capsys.readouterr().err


@pytest.fixture(params=['closed', 'writer_error'])
def unavailable_stderr(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> io.StringIO:
    stream = io.StringIO()
    if request.param == 'closed':
        stream.close()
    else:

        def fail_write(_text: str) -> Never:
            raise OSError('warning output unavailable')

        monkeypatch.setattr(stream, 'write', fail_write)
    return stream
