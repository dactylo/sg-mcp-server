import asyncio
import contextlib
import json
import threading
from pathlib import Path
from typing import Any, Unpack, cast

import jsonschema
import pytest
from jsonschema.protocols import Validator
from mcp import Client, MCPError, types
from mcp.server import Server
from support import FixtureConnection

from sg_mcp_server import _server, _tools
from sg_mcp_server._config import Config
from sg_mcp_server._diagnostics import DiagnosticContext
from sg_mcp_server._providers import (
    FindOptions,
    SGRecord,
    ValidatedFilters,
)
from sg_mcp_server._server import build_server
from sg_mcp_server._surface import ToolRuntime, ToolSurface
from tests.server._helpers import build_test_server, call, read_json_lines, text_of


class _ConcurrentConnection(FixtureConnection):
    def __init__(self, source: FixtureConnection) -> None:
        self.site = source.site
        self._active = 0
        self._barrier = threading.Barrier(2)
        self._state_lock = threading.Lock()
        self.max_active = 0

    def find(
        self,
        entity_type: str,
        filters: ValidatedFilters,
        fields: list[str] | None = None,
        **options: Unpack[FindOptions],
    ) -> list[SGRecord]:
        with self._state_lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            with contextlib.suppress(threading.BrokenBarrierError):
                self._barrier.wait(timeout=0.2)
            return super().find(entity_type, filters, fields, **options)
        finally:
            with self._state_lock:
                self._active -= 1


class TestDispatch:
    def test_audit_written_for_every_call(self, server: Server, tmp_path: Path) -> None:
        call(server, 'sg_schema', {'op': 'entities'})
        lines = (tmp_path / 'audit.jsonl').read_text().splitlines()
        assert len(lines) == 1

    @pytest.mark.parametrize(
        ('structured', 'expected_ids'),
        cast(
            'list[tuple[dict[str, Any], list[int]]]',
            [
                ({}, []),
                ({'records': {'id': 1}}, []),
                (
                    {
                        'records': [
                            None,
                            [],
                            {'name': 'missing'},
                            {'id': True},
                            {'id': 7.0},
                            {'id': '8'},
                            {'id': 9},
                        ]
                    },
                    [9],
                ),
            ],
        ),
        ids=['missing-records', 'non-list-records', 'malformed-records'],
    )
    def test_audited_records_only_checked_integer_ids(
        self,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        structured: dict[str, Any],
        expected_ids: list[int],
    ) -> None:
        def handler(
            runtime: ToolRuntime,
            payload: dict[str, Any],
            context: DiagnosticContext,
        ) -> types.CallToolResult:
            return runtime.audited(payload, context, lambda: ('ok', structured))

        surface = ToolSurface(
            tools=(
                types.Tool(
                    name='test_audit',
                    description='Exercise audited result extraction.',
                    input_schema={'type': 'object', 'additionalProperties': False},
                ),
            ),
            handlers={'test_audit': handler},
            argument_error=lambda _tool, _arguments, error: str(error),
        )
        server = build_server(
            Config(
                site_url='https://fixture.shotgunstudio.com',
                script_name='s',
                api_key='k',
                audit_path=tmp_path / 'audit.jsonl',
            ),
            fixture_site,
            surface,
        )

        result = call(server, 'test_audit', {})

        assert result.structured_content == structured
        audit_record = read_json_lines(tmp_path / 'audit.jsonl')[0]
        assert audit_record['entity_ids'] == expected_ids

    def test_concurrent_calls_serialize_shared_connection_access(
        self, fixture_site: FixtureConnection, tmp_path: Path
    ) -> None:
        connection = _ConcurrentConnection(fixture_site)
        server = build_test_server(connection, tmp_path, diagnostics=True)

        async def run() -> tuple[types.CallToolResult, types.CallToolResult]:
            async with Client(server) as client:
                payload = {'entity_type': 'Shot', 'filters': [], 'fields': ['code']}
                return await asyncio.gather(
                    client.call_tool('sg_query', payload),
                    client.call_tool('sg_query', payload),
                )

        results = asyncio.run(run())

        records = read_json_lines(tmp_path / 'diagnostics.jsonl')
        audit_records = read_json_lines(tmp_path / 'audit.jsonl')
        request_ids = sorted(record['request_id'] for record in records)
        wait_record = max(
            records,
            key=lambda record: record['phases_ms']['connection_lock_wait'],
        )
        lock_wait_ms = wait_record['phases_ms']['connection_lock_wait']
        assert all(result.is_error is False for result in results)
        assert request_ids[1] == request_ids[0] + 1
        assert all(record['provider_call_count'] in {1, 3} for record in records)
        assert connection.max_active == 1
        assert wait_record['duration_ms'] >= lock_wait_ms
        assert len(audit_records) == 2
        assert min(record['duration_ms'] for record in audit_records) < lock_wait_ms

    def test_sg_get_not_found_distinguishes_no_results(self, server: Server) -> None:
        result = call(
            server, 'sg_get', {'entity_type': 'Shot', 'entity_id': 999, 'fields': ['code']}
        )
        assert result.is_error is True
        assert 'no results' in text_of(result).lower()
        assert 'permissions' in text_of(result).lower()

    def test_sg_query_round_trip(self, server: Server, fixture_site: FixtureConnection) -> None:
        project = fixture_site.create('Project', {'name': 'P1'})
        shot = fixture_site.create('Shot', {'code': 'SH001', 'project': project})
        # NOTE: build_server received this same fixture_site instance.
        result = call(
            server,
            'sg_query',
            {'entity_type': 'Shot', 'filters': [], 'fields': ['code']},
        )
        assert result.is_error is False
        assert 'SH001' in text_of(result)
        assert result.structured_content is not None
        row = result.structured_content['records'][0]
        assert row['entity_url'] == (f'https://fixture.shotgunstudio.com/detail/Shot/{shot["id"]}')
        assert 'url' not in row
        assert result.structured_content['contains_untrusted_site_text'] is True

    def test_unexpected_argument_is_strictly_rejected_and_audited(
        self, server: Server, tmp_path: Path
    ) -> None:
        result = call(
            server,
            'sg_get',
            {'entity_type': 'Shot', 'entity_id': 1, 'fields': ['code'], 'flds': []},
        )
        assert result.is_error is True
        assert 'flds' in text_of(result)
        assert 'invalid_arguments' in (tmp_path / 'audit.jsonl').read_text()

    def test_unknown_tool_is_protocol_error_and_audited(
        self, server: Server, tmp_path: Path
    ) -> None:
        with pytest.raises(MCPError, match='unknown tool'):
            call(server, 'sg_nope', {})
        assert 'unknown_tool' in (tmp_path / 'audit.jsonl').read_text()

    def test_validation_error_is_a_result_not_an_exception(self, server: Server) -> None:
        result = call(server, 'sg_query', {'entity_type': 'Shoot', 'filters': [], 'fields': []})
        assert result.is_error is True
        assert 'entity_types' in text_of(result)


def test_audit_uses_dispatched_tool_name(fixture_site: FixtureConnection, tmp_path: Path) -> None:
    original = _tools.surface()
    schema_tool = next(tool for tool in original.tools if tool.name == 'sg_schema')
    surface = ToolSurface(
        tools=(schema_tool.model_copy(update={'name': 'schema_alias'}),),
        handlers={'schema_alias': original.handlers['sg_schema']},
        argument_error=original.argument_error,
    )
    server = build_server(
        Config(
            site_url='https://fixture.shotgunstudio.com',
            script_name='s',
            api_key='k',
            audit_path=tmp_path / 'audit.jsonl',
        ),
        fixture_site,
        surface,
    )
    result = call(server, 'schema_alias', {'op': 'entities'})
    assert result.is_error is False
    record = json.loads((tmp_path / 'audit.jsonl').read_text())
    assert record['tool'] == 'schema_alias'


def test_tool_schema_validation_preserves_best_match_message(server: Server) -> None:
    result = call(
        server,
        'sg_get',
        {'entity_type': 'Shot', 'entity_id': '1'},
    )

    assert result.is_error is True
    assert result.structured_content == {
        'error': "invalid arguments: 'fields' is a required property"
    }


def test_tool_schema_validators_compile_once(
    fixture_site: FixtureConnection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiled_schemas: list[object] = []
    real_validator_for = _server.validator_for
    surface = _tools.surface()
    tools = surface.tools
    tool_schemas = tuple(tool.input_schema for tool in tools)
    monkeypatch.setattr(_tools, 'surface', lambda: surface)

    def _counting_validator_for(schema: dict[str, Any]) -> type[Validator]:
        if any(schema is tool_schema for tool_schema in tool_schemas):
            compiled_schemas.append(schema)
        return real_validator_for(schema)

    monkeypatch.setattr(_server, 'validator_for', _counting_validator_for)
    server = build_test_server(fixture_site, tmp_path)

    def _forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError('jsonschema.validate must not run per request')

    monkeypatch.setattr(jsonschema, 'validate', _forbidden)
    payload = {'entity_type': 'Shot', 'filters': [], 'fields': ['code']}
    first = call(server, 'sg_query', payload)
    second = call(server, 'sg_query', payload)
    invalid = call(
        server,
        'sg_query',
        {'entity_type': 'Shot', 'filters': ['code', 'is', 'NF_0101']},
    )

    assert first.is_error is False
    assert second.is_error is False
    assert invalid.is_error is True
    assert invalid.structured_content is not None
    assert set(invalid.structured_content) == {'error'}
    assert 'invalid arguments' in invalid.structured_content['error']
    assert compiled_schemas == list(tool_schemas)
