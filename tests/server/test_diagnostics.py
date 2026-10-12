import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Unpack

import pytest
from mcp import Client, MCPError
from support import Clock, FixtureConnection

from sg_mcp_server import _server, _tools
from sg_mcp_server._config import Config
from sg_mcp_server._diagnostics import DiagnosticContext, DiagnosticSink
from sg_mcp_server._providers import (
    FindOptions,
    SGRecord,
    ValidatedFilters,
)
from sg_mcp_server._server import build_server
from tests.server._helpers import (
    AdversarialLabelConnection,
    BackendErrorConnection,
    build_test_server,
    call,
    read_json_lines,
)


class _CancellationConnection(FixtureConnection):
    def __init__(self, source: FixtureConnection) -> None:
        self.site = source.site
        self.completed = threading.Event()
        self.release = threading.Event()
        self.started = threading.Event()

    def find(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        fields: list[str] | None = None,
        **options: Unpack[FindOptions],
    ) -> list[SGRecord]:
        self.started.set()
        assert self.release.wait(timeout=5)
        try:
            return super().find(entity_type, filters, fields, **options)
        finally:
            self.completed.set()


class TestDiagnostics:
    def test_audit_failure_does_not_prevent_diagnostic_finalization(
        self,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        audit_path = tmp_path / 'blocked-audit'
        audit_path.mkdir()
        diagnostics_path = tmp_path / 'diagnostics.jsonl'
        server = build_server(
            Config(
                site_url='https://fixture.shotgunstudio.com',
                script_name='s',
                api_key='k',
                audit_path=audit_path,
                diagnostics_path=diagnostics_path,
            ),
            fixture_site,
            _tools.surface(),
        )

        result = call(server, 'sg_schema', {'op': 'entities'})

        assert result.is_error is False
        assert 'warning: audit write failed: ' in capsys.readouterr().err
        assert read_json_lines(diagnostics_path)[0]['outcome'] == 'ok'

    def test_cancelled_call_drains_worker_before_finalizing_diagnostics(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        connection = _CancellationConnection(fixture_site)
        server = build_test_server(connection, tmp_path, diagnostics=True)
        diagnostics_path = tmp_path / 'diagnostics.jsonl'
        audit_path = tmp_path / 'audit.jsonl'

        async def run() -> None:
            async with Client(server) as client:
                request = asyncio.create_task(
                    client.call_tool(
                        'sg_query',
                        {'entity_type': 'Shot', 'filters': [], 'fields': ['code']},
                    )
                )
                assert await asyncio.to_thread(connection.started.wait, 2)

                request.cancel()
                try:
                    cancellation_checkpoint = asyncio.Event()
                    asyncio.get_running_loop().call_soon(cancellation_checkpoint.set)
                    await cancellation_checkpoint.wait()

                    assert request.cancelling() == 1
                    assert not connection.completed.is_set()
                    assert not diagnostics_path.exists()
                    assert not audit_path.exists()
                finally:
                    connection.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await request

        asyncio.run(run())

        assert connection.completed.is_set()
        diagnostic_records = read_json_lines(diagnostics_path)
        audit_records = read_json_lines(audit_path)
        assert len(diagnostic_records) == 1
        assert len(audit_records) == 1
        assert diagnostic_records[0]['outcome'] == audit_records[0]['outcome'] == 'ok'

    def test_cold_and_warm_queries_report_exact_phase_and_provider_work(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        server = build_test_server(fixture_site, tmp_path, diagnostics=True)
        payload = {'entity_type': 'Shot', 'filters': [], 'fields': ['code']}

        first = call(server, 'sg_query', payload)
        second = call(server, 'sg_query', payload)

        records = read_json_lines(tmp_path / 'diagnostics.jsonl')
        assert first.is_error is False
        assert second.is_error is False
        assert len(records) == 2
        assert records[0]['outcome'] == 'ok'
        assert records[0]['schema_cache'] == {'hits': 0, 'misses': 1, 'refreshes': 0}
        assert records[0]['provider_call_count'] == 3
        assert set(records[0]['phases_ms']) == {
            'argument_validation',
            'connection_lock_wait',
            'schema_cache_lookup',
            'schema_reflection',
            'query_validation',
            'provider_call',
            'rendering',
            'audit_write',
        }
        assert records[1]['schema_cache'] == {'hits': 1, 'misses': 0, 'refreshes': 0}
        assert records[1]['provider_call_count'] == 1
        assert 'schema_reflection' not in records[1]['phases_ms']

    def test_destination_failure_warns_once_without_changing_result_or_audit(
        self,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / 'diagnostics.jsonl').mkdir()
        server = build_test_server(fixture_site, tmp_path, diagnostics=True)

        first = call(server, 'sg_schema', {'op': 'entities'})
        second = call(server, 'sg_schema', {'op': 'entities'})

        audit_records = read_json_lines(tmp_path / 'audit.jsonl')
        assert first.is_error is False
        assert second.structured_content == first.structured_content
        assert len(audit_records) == 2
        assert all(record['outcome'] == 'ok' for record in audit_records)
        assert capsys.readouterr().err == 'warning: diagnostic write failed\n'

    def test_disabled_diagnostics_preserve_exact_audit_schema(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        server = build_test_server(fixture_site, tmp_path)

        result = call(server, 'sg_schema', {'op': 'entities'})

        audit_record = read_json_lines(tmp_path / 'audit.jsonl')[0]
        assert result.is_error is False
        assert not (tmp_path / 'diagnostics.jsonl').exists()
        assert set(audit_record) == {
            'caller',
            'duration_ms',
            'entity_ids',
            'input_sha256',
            'outcome',
            'tool',
            'ts',
        }

    def test_full_long_lived_sequence_has_ordered_reconciling_records(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        project = fixture_site.create('Project', {'name': 'Diagnostic Project'})
        server = build_test_server(fixture_site, tmp_path, diagnostics=True)
        query = {
            'entity_type': 'Project',
            'filters': [],
            'fields': ['name'],
            'order': [{'field_name': 'id', 'direction': 'asc'}],
            'limit': 1,
        }

        async def run() -> None:
            async with Client(server) as client:
                for _ in range(5):
                    await client.call_tool('sg_schema', {'op': 'entities'})
                for _ in range(5):
                    await client.call_tool('sg_query', query)
                await client.call_tool(
                    'sg_get',
                    {'entity_type': 'Project', 'entity_id': project['id'], 'fields': ['name']},
                )

        asyncio.run(run())

        records = read_json_lines(tmp_path / 'diagnostics.jsonl')
        assert len(records) == 11
        assert [record['request_id'] for record in records] == list(
            range(records[0]['request_id'], records[0]['request_id'] + 11)
        )
        assert records[0]['tool'] == 'sg_schema'
        assert records[0]['schema_cache'] == {'hits': 0, 'misses': 1, 'refreshes': 0}
        assert 'schema_reflection' in records[0]['phases_ms']
        for record in records[1:5]:
            assert record['tool'] == 'sg_schema'
            assert record['schema_cache'] == {'hits': 1, 'misses': 0, 'refreshes': 0}
            assert 'schema_reflection' not in record['phases_ms']
        for record in records:
            assert (
                abs(
                    record['duration_ms']
                    - sum(record['phases_ms'].values())
                    - record['unattributed_ms']
                )
                <= 0.00001
            )

    def test_invalid_arguments_finalize_without_entering_worker(
        self,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server = build_test_server(fixture_site, tmp_path, diagnostics=True)

        async def fail_to_thread(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError('invalid arguments entered worker')

        monkeypatch.setattr(asyncio, 'to_thread', fail_to_thread)

        result = call(
            server,
            'sg_get',
            {'entity_type': 'Shot', 'entity_id': 1, 'fields': ['code'], 'flds': []},
        )

        record = read_json_lines(tmp_path / 'diagnostics.jsonl')[0]
        assert result.is_error is True
        assert record['outcome'] == 'invalid_arguments'
        assert set(record['phases_ms']) == {'argument_validation', 'audit_write'}

    def test_nonmonotonic_clock_warns_once_without_changing_result_or_audit(
        self,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        server = build_test_server(fixture_site, tmp_path, diagnostics=True)

        def start(_sink: DiagnosticSink, tool: str, _started_ns: int) -> DiagnosticContext:
            return DiagnosticContext(1, tool, 100, Clock(110, 120, 130, 140, 105))

        monkeypatch.setattr(DiagnosticSink, 'start', start)
        payload = {'entity_type': 'Shot', 'entity_id': 1, 'fields': ['code'], 'flds': []}

        first = call(server, 'sg_get', payload)
        second = call(server, 'sg_get', payload)

        audit_records = read_json_lines(tmp_path / 'audit.jsonl')
        diagnostic_records = read_json_lines(tmp_path / 'diagnostics.jsonl')
        assert first.is_error is True
        assert second.structured_content == first.structured_content
        assert len(audit_records) == 2
        assert all(record['outcome'] == 'invalid_arguments' for record in audit_records)
        assert len(diagnostic_records) == 2
        assert all(record['unattributed_ms'] == 0.0 for record in diagnostic_records)
        assert capsys.readouterr().err == 'warning: diagnostic accounting failed\n'

    @pytest.mark.parametrize(
        ('name', 'payload'),
        [
            ('sg_nope', {}),
            (
                'sg_get',
                {'entity_type': 'Shot', 'entity_id': 1, 'fields': ['code'], 'flds': []},
            ),
        ],
        ids=['unknown-tool', 'invalid-arguments'],
    )
    def test_protocol_rejection_audit_includes_diagnostic_startup(
        self,
        name: str,
        payload: dict[str, Any],
        fixture_site: FixtureConnection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = {'now': 0.0}
        original_start = DiagnosticSink.start

        def delayed_start(
            sink: DiagnosticSink,
            tool: str,
            started_ns: int,
        ) -> DiagnosticContext:
            clock['now'] = 0.05
            return original_start(sink, tool, started_ns)

        fake_time = SimpleNamespace(
            monotonic=lambda: clock['now'],
            monotonic_ns=lambda: round(clock['now'] * 1_000_000_000),
        )
        monkeypatch.setattr(_server, 'time', fake_time)
        monkeypatch.setattr(DiagnosticSink, 'start', delayed_start)
        server = build_test_server(fixture_site, tmp_path)

        if name == 'sg_nope':
            with pytest.raises(MCPError, match='unknown tool'):
                call(server, name, payload)
        else:
            assert call(server, name, payload).is_error is True

        audit_record = read_json_lines(tmp_path / 'audit.jsonl')[0]
        assert audit_record['duration_ms'] == 50.0

    def test_rejected_and_failed_requests_minimize_raw_diagnostic_data(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        adversarial_tool = 'private-tool-QX7V'
        adversarial_payload = 'payload-secret-QX7V'
        adversarial_backend = 'backend-secret-QX7V'
        adversarial_label = 'label-secret-QX7V'
        adversarial_entity = 'PrivateEntityQX7V'
        entity_id = 982_451_653

        label_path = tmp_path / 'label'
        label_server = build_test_server(
            AdversarialLabelConnection(fixture_site, adversarial_label),
            label_path,
            diagnostics=True,
        )
        with pytest.raises(MCPError, match='unknown tool'):
            call(
                label_server,
                adversarial_tool,
                {
                    'secret': adversarial_payload,
                    'entity_type': adversarial_entity,
                    'entity_id': entity_id,
                },
            )
        validation_result = call(
            label_server,
            'sg_query',
            {'entity_type': 'Shot', 'filters': [], 'fields': ['cod']},
        )
        unknown_entity_result = call(
            label_server,
            'sg_query',
            {'entity_type': adversarial_entity, 'filters': [], 'fields': []},
        )

        backend_path = tmp_path / 'backend'
        backend_server = build_test_server(
            BackendErrorConnection(fixture_site, adversarial_backend),
            backend_path,
            diagnostics=True,
        )
        backend_result = call(
            backend_server,
            'sg_query',
            {'entity_type': 'Shot', 'filters': [], 'fields': ['code']},
        )

        raw_diagnostics = (label_path / 'diagnostics.jsonl').read_text() + (
            backend_path / 'diagnostics.jsonl'
        ).read_text()
        outcomes = [
            record['outcome'] for record in read_json_lines(label_path / 'diagnostics.jsonl')
        ]
        outcomes.extend(
            record['outcome'] for record in read_json_lines(backend_path / 'diagnostics.jsonl')
        )
        assert validation_result.is_error is True
        assert unknown_entity_result.is_error is True
        assert backend_result.is_error is True
        assert outcomes == ['unknown_tool', 'validation_error', 'validation_error', 'query_error']
        for adversarial_value in (
            adversarial_tool,
            adversarial_payload,
            adversarial_backend,
            adversarial_label,
            adversarial_entity,
            str(entity_id),
        ):
            assert adversarial_value not in raw_diagnostics

    def test_unknown_tool_uses_normalized_name_and_finalizes_once(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        server = build_test_server(fixture_site, tmp_path, diagnostics=True)

        with pytest.raises(MCPError, match='unknown tool'):
            call(server, 'sg_nope', {})

        records = read_json_lines(tmp_path / 'diagnostics.jsonl')
        assert len(records) == 1
        assert records[0]['tool'] == 'unknown'
        assert records[0]['outcome'] == 'unknown_tool'

    def test_worker_setup_exception_finalizes_once_as_unexpected_error(
        self,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        server = build_test_server(fixture_site, tmp_path, diagnostics=True)

        def fail_arguments(**_payload: object) -> object:
            raise RuntimeError('pre-audit-secret-QX7V')

        monkeypatch.setattr(_tools, '_QueryArguments', fail_arguments)

        with pytest.raises(MCPError):
            call(server, 'sg_query', {'entity_type': 'Shot'})

        raw_diagnostics = (tmp_path / 'diagnostics.jsonl').read_text()
        records = read_json_lines(tmp_path / 'diagnostics.jsonl')
        assert len(records) == 1
        assert records[0]['outcome'] == 'unexpected_error'
        assert 'pre-audit-secret-QX7V' not in raw_diagnostics
