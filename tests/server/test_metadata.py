import importlib.metadata
import json
from pathlib import Path

import pytest
from mcp.server import Server
from support import FixtureConnection

from sg_mcp_server import _tools
from sg_mcp_server._diagnostics import DiagnosticSink
from tests.server._helpers import (
    build_test_server,
    listed_tools,
    server_info,
    server_instructions,
)

_TOOL_NAMES = {'sg_schema', 'sg_query', 'sg_get'}


class TestServerMetadata:
    def test_instructions_cover_product_names(self, server: Server) -> None:
        instructions = server_instructions(server)
        assert instructions is not None
        for product_name in (
            'Flow Production Tracking',
            'FPT',
            'Flow',
            'ShotGrid',
            'Shotgun',
            'SG',
        ):
            assert product_name in instructions
        assert 'read-only' in instructions.lower()
        assert 'production-tracking context' in instructions

    def test_server_info_reports_the_installed_version(
        self,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        installed = {'sg-mcp-server': '9.8.7'}
        monkeypatch.setattr(importlib.metadata, 'version', installed.__getitem__)
        server = build_test_server(fixture_site, tmp_path)

        info = server_info(server)

        assert info is not None
        assert (info.name, info.version) == ('sg-mcp-server', '9.8.7')

    def test_sg_schema_description_documents_truncation(self) -> None:
        tool = next(tool for tool in _tools.surface().tools if tool.name == 'sg_schema')

        assert tool.description is not None
        assert 'Selection value and entity-type lists may be capped' in tool.description
        assert '<name>_omitted count reports how many entries are not shown' in tool.description

    def test_tool_descriptions_identify_product(self, server: Server) -> None:
        for tool in listed_tools(server):
            assert tool.description is not None
            assert 'Flow Production Tracking' in tool.description, tool.name


class TestToolList:
    def test_exactly_the_read_tools(self, server: Server) -> None:
        assert {tool.name for tool in listed_tools(server)} == _TOOL_NAMES

    def test_read_only_annotations(self, server: Server) -> None:
        for tool in listed_tools(server):
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False

    def test_schemas_are_inlined_without_nullable_unions(self, server: Server) -> None:
        for tool in listed_tools(server):
            serialized = json.dumps(tool.input_schema)
            for banned in ('$ref', '$defs', 'anyOf', '"null"'):
                assert banned not in serialized, (tool.name, banned)

    def test_schemas_close_the_argument_set(self, server: Server) -> None:
        for tool in listed_tools(server):
            assert tool.input_schema['additionalProperties'] is False, tool.name

    def test_server_binds_diagnostics_to_its_actual_tool_table(
        self,
        fixture_site: FixtureConnection,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        catalogs: list[tuple[str, ...]] = []
        original_init = DiagnosticSink.__init__

        def capture_catalog(
            sink: DiagnosticSink,
            path: Path | None,
            *,
            known_tools: tuple[str, ...],
        ) -> None:
            catalogs.append(known_tools)
            original_init(sink, path, known_tools=known_tools)

        monkeypatch.setattr(DiagnosticSink, '__init__', capture_catalog)

        build_test_server(fixture_site, tmp_path)

        assert catalogs == [tuple(tool.name for tool in _tools.surface().tools)]
