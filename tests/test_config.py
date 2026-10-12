import traceback
import zoneinfo
from pathlib import Path

import pytest

from sg_mcp_server import _config
from sg_mcp_server._config import Config, ConfigError

_AUDIT_PATH = Path('/').joinpath('tmp', 'audit.jsonl')
_DIAGNOSTICS_PATH = Path('/').joinpath('tmp', 'diag.jsonl')
_BASE_ENV: dict[str, str] = {
    'SG_MCP_SITE_URL': 'https://example.shotgunstudio.com',
    'SG_MCP_SCRIPT_NAME': 'sg-mcp-server',
    'SG_MCP_API_KEY': 'secret',
}


class TestFromEnv:
    def test_audit_path_is_stripped(self) -> None:
        config = Config.from_env(_BASE_ENV | {'SG_MCP_AUDIT_PATH': f' {_AUDIT_PATH} '})
        assert config.audit_path == _AUDIT_PATH

    def test_bad_integer_raises(self) -> None:
        with pytest.raises(ConfigError, match='SG_MCP_DEFAULT_LIMIT'):
            Config.from_env(_BASE_ENV | {'SG_MCP_DEFAULT_LIMIT': 'many'})

    def test_default_above_hard_raises(self) -> None:
        env = _BASE_ENV | {'SG_MCP_DEFAULT_LIMIT': '300', 'SG_MCP_HARD_LIMIT': '200'}
        with pytest.raises(ConfigError, match='SG_MCP_DEFAULT_LIMIT'):
            Config.from_env(env)

    def test_defaults(self) -> None:
        config = Config.from_env(_BASE_ENV)
        assert config.timezone == 'UTC'
        assert config.default_limit == 50
        assert config.hard_limit == 200
        assert config.schema_ttl_seconds == 900
        assert config.audit_path == Path.home() / '.sg-mcp-server' / 'audit.jsonl'

    def test_missing_required_raises(self) -> None:
        with pytest.raises(ConfigError, match='SG_MCP_SITE_URL'):
            Config.from_env({})

    @pytest.mark.parametrize('value', ['0', '-1'])
    def test_non_positive_timeout_raises(self, value: str) -> None:
        with pytest.raises(ConfigError, match='SG_MCP_TIMEOUT_SECONDS setting must be a positive'):
            Config.from_env(_BASE_ENV | {'SG_MCP_TIMEOUT_SECONDS': value})

    @pytest.mark.parametrize('value', ['', '1.5', 'many'])
    def test_non_integer_timeout_raises(self, value: str) -> None:
        with pytest.raises(ConfigError, match='SG_MCP_TIMEOUT_SECONDS setting must be an integer'):
            Config.from_env(_BASE_ENV | {'SG_MCP_TIMEOUT_SECONDS': value})

    @pytest.mark.parametrize('timezone', ['Not/A_Zone', '/etc/localtime', ''])
    def test_bad_timezone_raises_configuration_error(self, timezone: str) -> None:
        with pytest.raises(ConfigError, match='SG_MCP_TIMEZONE'):
            Config.from_env(_BASE_ENV | {'SG_MCP_TIMEZONE': timezone})

    @pytest.mark.parametrize('value', ['', '   '])
    def test_blank_audit_path_raises_configuration_error(self, value: str) -> None:
        # A blank path would resolve to the working directory and silently disable the audit.
        with pytest.raises(ConfigError, match='SG_MCP_AUDIT_PATH'):
            Config.from_env(_BASE_ENV | {'SG_MCP_AUDIT_PATH': value})

    @pytest.mark.parametrize(
        'site_url',
        [
            'https://example.com?',
            'https://example.com#',
            'https://example.com/?',
            'https://example.com/#',
            'https://example.com:',
        ],
    )
    def test_config_rejects_empty_origin_delimiters(self, site_url: str) -> None:
        with pytest.raises(ConfigError, match='SG_MCP_SITE_URL'):
            Config(site_url=site_url, script_name='script', api_key='secret')

    def test_overrides(self) -> None:
        env = _BASE_ENV | {
            'SG_MCP_TIMEZONE': 'America/Los_Angeles',
            'SG_MCP_DEFAULT_LIMIT': '25',
            'SG_MCP_AUDIT_PATH': str(_AUDIT_PATH),
        }
        config = Config.from_env(env)
        assert config.timezone == 'America/Los_Angeles'
        assert config.default_limit == 25
        assert config.audit_path == _AUDIT_PATH

    def test_parser_error_traceback_does_not_echo_url_credentials(self) -> None:
        sensitive_netloc = 'sentinel-user:sentinel-password\uff20example.com'

        with pytest.raises(ConfigError) as exc_info:
            Config(
                site_url=f'https://{sensitive_netloc}',
                script_name='sg-mcp-server',
                api_key='api-secret',
            )

        rendered = ''.join(traceback.format_exception(exc_info.value))
        assert 'ConfigError: the SG_MCP_SITE_URL setting must be an HTTPS origin' in rendered
        assert 'sentinel-user' not in rendered
        assert 'sentinel-password' not in rendered
        assert sensitive_netloc not in rendered

    def test_reads_required_values(self) -> None:
        config = Config.from_env(_BASE_ENV)
        assert config.site_url == 'https://example.shotgunstudio.com'
        assert config.script_name == 'sg-mcp-server'
        assert config.api_key == 'secret'

    def test_timeout_defaults_to_sixty_seconds(self) -> None:
        assert Config.from_env(_BASE_ENV).timeout_seconds == 60

    def test_timeout_override(self) -> None:
        assert Config.from_env(_BASE_ENV | {'SG_MCP_TIMEOUT_SECONDS': '5'}).timeout_seconds == 5

    @pytest.mark.parametrize('timezone', ['UTC', 'America/Los_Angeles'])
    def test_timezones_available_without_system_database(
        self, timezone: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original_path = zoneinfo.TZPATH
        zoneinfo.reset_tzpath(())
        monkeypatch.setattr(_config, 'ZoneInfo', zoneinfo.ZoneInfo.no_cache)
        try:
            config = Config.from_env(_BASE_ENV | {'SG_MCP_TIMEZONE': timezone})
        finally:
            zoneinfo.reset_tzpath(original_path)

        assert config.timezone == timezone

    @pytest.mark.parametrize(
        ('site_url', 'construct_directly'),
        [
            pytest.param('http://example.shotgrid.autodesk.com', True, id='http'),
            pytest.param(
                'https://url-user:url-password@example.shotgrid.autodesk.com',
                False,
                id='userinfo',
            ),
            pytest.param('https:///missing-host', False, id='missing-host'),
            pytest.param(
                'https://example.shotgrid.autodesk.com/path',
                False,
                id='non-root-path',
            ),
            pytest.param(
                'https://example.shotgrid.autodesk.com?query=value',
                False,
                id='query',
            ),
            pytest.param(
                'https://example.shotgrid.autodesk.com#fragment',
                False,
                id='fragment',
            ),
            pytest.param(
                'https://example.shotgrid.autodesk.com:not-a-port',
                False,
                id='malformed-port',
            ),
        ],
    )
    def test_unsafe_site_url_raises_without_echoing_secrets(
        self,
        site_url: str,
        construct_directly: bool,
    ) -> None:
        if construct_directly:

            def construct() -> Config:
                return Config(
                    site_url=site_url,
                    script_name='sg-mcp-server',
                    api_key='api-secret',
                )
        else:

            def construct() -> Config:
                return Config.from_env(
                    _BASE_ENV | {'SG_MCP_SITE_URL': site_url, 'SG_MCP_API_KEY': 'api-secret'}
                )

        with pytest.raises(ConfigError) as exc_info:
            construct()

        message = str(exc_info.value)
        assert 'SG_MCP_SITE_URL' in message
        assert 'api-secret' not in message
        assert 'url-user' not in message
        assert 'url-password' not in message

    @pytest.mark.parametrize(
        'site_url',
        [
            'https://example.shotgrid.autodesk.com',
            'https://example.shotgrid.autodesk.com/',
        ],
    )
    def test_valid_https_origin_is_normalized(self, site_url: str) -> None:
        env = _BASE_ENV | {'SG_MCP_SITE_URL': site_url}
        assert Config.from_env(env).site_url == 'https://example.shotgrid.autodesk.com'

    def test_zero_hard_limit_raises(self) -> None:
        # SG treats limit=0 as unlimited; a zero cap would remove the row bound entirely.
        with pytest.raises(ConfigError, match='SG_MCP_HARD_LIMIT'):
            Config.from_env(_BASE_ENV | {'SG_MCP_HARD_LIMIT': '0'})


def test_diagnostics_path_defaults_to_none() -> None:
    assert Config.from_env(_BASE_ENV).diagnostics_path is None


@pytest.mark.parametrize('value', ['', '   '])
def test_empty_diagnostics_path_disables_diagnostics(value: str) -> None:
    config = Config.from_env(_BASE_ENV | {'SG_MCP_DIAGNOSTICS_PATH': value})
    assert config.diagnostics_path is None


def test_nonempty_diagnostics_path_is_stripped() -> None:
    config = Config.from_env(_BASE_ENV | {'SG_MCP_DIAGNOSTICS_PATH': ' /tmp/diag.jsonl '})
    assert config.diagnostics_path == _DIAGNOSTICS_PATH
