from __future__ import annotations

import re
import subprocess
from pathlib import Path

_CHALLENGER_PATTERN = re.compile(
    r'_arm_|ARM_[A-Z]|_runtime_arms'
    r'|\bsg_find\b|\bsg_find_one\b|\bsg_summarize\b|\bsg_schema_read\b'
    r'|\bsg_schema_entity_read\b|\bsg_schema_field_read\b|\bsg_search\b|\bsg_describe\b'
    r'|\bsg_sql\b|\brun_python\b|from experiments|import experiments'
    r'|\bexperiments(?:/|\.)'
    r'|\b(?:from|import)\s+(?:harness|_paths)\b'
)
_PUBLIC_ROOTS = (
    'src',
    'tests',
    'docs/benchmarks.md',
    'docs/development.md',
    'docs/diagnostics.md',
    'docs/usage.md',
    'scripts',
    'README.md',
    'LICENSE',
    'pyproject.toml',
    'Makefile',
    'requirements-ci.txt',
    '.gitignore',
    '.editorconfig',
    '.gitattributes',
    'pyrightconfig.json',
)
_SELF = 'tests/test_public_boundary.py'
_ALLOWED_CONFIG_ROOT = 'experiments/tests'
_ALLOWED_LINT_OWNER_PATHS = (
    'experiments/arms/_arm_b.py',
    'experiments/arms/_arm_d.py',
    'experiments/arms/_arm_e.py',
    'experiments/bakeoff/dataset/seed.py',
    'experiments/bakeoff/driver.py',
    'experiments/bakeoff/fixtures/augment_schema.py',
    'experiments/bakeoff/harness/_questions.py',
    'experiments/bakeoff/harness/_schema_tools.py',
    'experiments/bakeoff/harness/runner.py',
    'experiments/model_benchmark/_artifacts.py',
    'experiments/model_benchmark/_budget.py',
    'experiments/model_benchmark/_claude.py',
    'experiments/model_benchmark/_cli.py',
    'experiments/model_benchmark/_codex.py',
    'experiments/model_benchmark/_interface_aggregation.py',
    'experiments/model_benchmark/_interface_gate.py',
    'experiments/model_benchmark/_interface_replay.py',
    'experiments/model_benchmark/_live.py',
    'experiments/model_benchmark/_realism.py',
    'experiments/model_benchmark/_report.py',
    'experiments/model_benchmark/_runner.py',
    'experiments/model_benchmark/_subprocess.py',
)


def _has_forbidden_public_reference(path: Path, line: str) -> bool:
    if path == Path('pyproject.toml'):
        line = line.replace(_ALLOWED_CONFIG_ROOT, '')
        key_prefix = 'lint.per-file-ignores."'
        if line.startswith(key_prefix):
            owner_path, separator, remainder = line.removeprefix(key_prefix).partition('"')
            if (
                separator
                and owner_path in _ALLOWED_LINT_OWNER_PATHS
                and remainder.lstrip().startswith('=')
            ):
                line = remainder
    return _CHALLENGER_PATTERN.search(line) is not None


def _text_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding='utf-8').splitlines()
    except UnicodeDecodeError:
        return []


def _tracked_public_files() -> list[Path]:
    output = subprocess.run(
        ['git', 'ls-files', *_PUBLIC_ROOTS],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [Path(line) for line in output.splitlines() if line != _SELF]


def test_private_lint_owner_allowlist_does_not_hide_other_references() -> None:
    references = (
        (Path('README.md'), 'lint.per-file-ignores."experiments/tests" = [ "D" ]'),
        (Path('pyproject.toml'), 'description = "experiments/bakeoff/dataset/seed.py"'),
        (
            Path('pyproject.toml'),
            'lint.per-file-ignores."experiments/bakeoff/dataset/other.py" = [ "T201" ]',
        ),
        (
            Path('pyproject.toml'),
            (
                'lint.per-file-ignores."experiments/bakeoff/dataset/seed.py" = [ "T201" ] '
                '# experiments/private.py'
            ),
        ),
    )

    assert all(_has_forbidden_public_reference(path, line) for path, line in references)


def test_product_package_adds_no_public_module() -> None:
    package = Path('src/sg_mcp_server')
    public = sorted(
        path.name
        for path in package.glob('*.py')
        if not path.name.startswith('_') and path.name != '__init__.py'
    )

    assert public == []


def test_public_reference_check_rejects_arm_names() -> None:
    references = (
        (Path('src/sg_mcp_server/_server.py'), 'from sg_mcp_server import _arm_f'),
        (Path('src/sg_mcp_server/_tools.py'), 'TOOLS = ARM_F_TOOLS'),
        (Path('scripts/example.py'), "module = 'sg_mcp_server._arm_b'"),
    )

    assert all(_has_forbidden_public_reference(path, line) for path, line in references)


def test_public_tree_has_no_arm_module_file() -> None:
    offenders = [str(path) for path in _tracked_public_files() if '_arm_' in path.name]

    assert offenders == []


def test_public_tree_has_no_challenger_or_experiments_reference() -> None:
    offenders = [
        f'{path}:{number}: {line.strip()}'
        for path in _tracked_public_files()
        for number, line in enumerate(_text_lines(path), start=1)
        if _has_forbidden_public_reference(path, line)
    ]

    assert offenders == []


def test_public_tree_has_no_pickle_file() -> None:
    offenders = [str(path) for path in _tracked_public_files() if path.suffix == '.pickle']

    assert offenders == []
