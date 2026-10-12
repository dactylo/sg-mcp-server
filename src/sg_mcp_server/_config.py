"""Environment-driven configuration for the solo-mode server."""

import dataclasses
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_AUDIT_PATH = Path.home() / '.sg-mcp-server' / 'audit.jsonl'
DEFAULT_LIMIT = 50
HARD_LIMIT = 200
_SCHEMA_TTL_SECONDS = 900
_TIMEOUT_SECONDS = 60


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed.

    The message is one lowercase clause that names the setting, suitable for an
    `error:` line, and never repeats credentials from the site URL.
    """


@dataclasses.dataclass(frozen=True)
class Config:
    """Resolved connection, rendering, logging, and query-limit settings.

    Instances have fixed attributes. The site URL is normalized to an HTTPS
    origin, query limits satisfy `1 <= default_limit <= hard_limit`, and the SG
    request timeout is positive.

    Attributes:
        site_url: HTTPS site origin without a trailing slash.
        script_name: Flow Production Tracking script identity.
        api_key: API key belonging to the script identity.
        timezone: IANA timezone used for date rendering and relative dates.
        audit_path: Destination for the security audit JSONL log.
        default_limit: Row limit used when a query omits one.
        hard_limit: Maximum permitted query row limit.
        schema_ttl_seconds: Schema-cache lifetime in seconds.
        diagnostics_path: Optional request-diagnostics JSONL destination;
            `None` disables request diagnostics.
        timeout_seconds: Positive timeout for each SG connection attempt and
            data wait.
    """

    site_url: str
    script_name: str
    api_key: str
    timezone: str = 'UTC'
    audit_path: Path = _DEFAULT_AUDIT_PATH
    default_limit: int = DEFAULT_LIMIT
    hard_limit: int = HARD_LIMIT
    schema_ttl_seconds: int = _SCHEMA_TTL_SECONDS
    diagnostics_path: Path | None = None
    timeout_seconds: int = _TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Validate the site origin, timezone, and bounded query limits.

        Raises:
            ConfigError: If the site URL is not a valid bare HTTPS origin, the
                timezone is unavailable or invalid, or limits do not satisfy `1
                <= default_limit <= hard_limit`, or the timeout is below 1.
        """
        site_url_message = (
            'the SG_MCP_SITE_URL setting must be an HTTPS origin with a valid host and port, '
            'without userinfo, a non-root path, a query, or a fragment'
        )
        try:
            parsed_site_url = urllib.parse.urlsplit(self.site_url)
            _ = parsed_site_url.port
        except ValueError:
            raise ConfigError(site_url_message) from None
        if (
            parsed_site_url.scheme != 'https'
            or parsed_site_url.hostname is None
            or parsed_site_url.username is not None
            or parsed_site_url.password is not None
            or parsed_site_url.path not in {'', '/'}
            or parsed_site_url.query
            or parsed_site_url.fragment
            or '?' in self.site_url
            or '#' in self.site_url
            or parsed_site_url.netloc.endswith(':')
        ):
            raise ConfigError(site_url_message)
        normalized_site_url = urllib.parse.urlunsplit(
            (parsed_site_url.scheme, parsed_site_url.netloc, '', '', '')
        )
        object.__setattr__(self, 'site_url', normalized_site_url)

        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            message = 'the SG_MCP_TIMEZONE setting must name an available IANA timezone'
            raise ConfigError(message) from None

        if self.default_limit < 1 or self.hard_limit < self.default_limit:
            message = (
                'limits must satisfy 1 <= SG_MCP_DEFAULT_LIMIT <= SG_MCP_HARD_LIMIT '
                f'(SG treats limit 0 as unlimited); got {self.default_limit} '
                f'and {self.hard_limit}'
            )
            raise ConfigError(message)

        if self.timeout_seconds < 1:
            raise ConfigError(
                'the SG_MCP_TIMEOUT_SECONDS setting must be a positive integer, '
                f'got {self.timeout_seconds}'
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> 'Config':
        """Resolve server settings from the supported environment variables.

        Args:
            env: Environment mapping, normally `os.environ`. Requires nonblank
                `SG_MCP_SITE_URL`, `SG_MCP_SCRIPT_NAME`, and `SG_MCP_API_KEY`;
                these values are stripped. Optional `SG_MCP_TIMEZONE` and
                `SG_MCP_AUDIT_PATH` override rendering and audit settings;
                the audit path is stripped and must not be blank.
                Optional `SG_MCP_DEFAULT_LIMIT`, `SG_MCP_HARD_LIMIT`,
                `SG_MCP_SCHEMA_TTL_SECONDS`, and `SG_MCP_TIMEOUT_SECONDS` are
                parsed as integers; the timeout must be at least 1. Unset
                optional settings use `Config` defaults. An absent or blank
                `SG_MCP_DIAGNOSTICS_PATH` disables request diagnostics.

        Returns:
            The resolved configuration with a normalized HTTPS site origin.

        Raises:
            ConfigError: If a required variable is absent, a numeric override
                does not parse, the audit path is blank, the site URL violates
                the HTTPS-origin rules, the timezone is unavailable or invalid,
                the resolved limit combination is invalid, or the timeout is
                below 1.
        """

        def require(name: str) -> str:
            value = env.get(name, '').strip()
            if not value:
                message = f'the {name} setting is required'
                raise ConfigError(message)
            return value

        def integer(name: str, fallback: int) -> int:
            raw = env.get(name)
            if raw is None:
                return fallback
            try:
                return int(raw)
            except ValueError as error:
                message = f'the {name} setting must be an integer, got {raw!r}'
                raise ConfigError(message) from error

        def optional_path(name: str) -> Path | None:
            raw = env.get(name, '').strip()
            return Path(raw) if raw else None

        def audit_path() -> Path:
            raw = env.get('SG_MCP_AUDIT_PATH')
            if raw is None:
                return _DEFAULT_AUDIT_PATH
            value = raw.strip()
            if not value:
                # Path('') is the working directory, where every audit write would fail.
                message = (
                    'the SG_MCP_AUDIT_PATH setting must not be blank; unset it to use the default'
                )
                raise ConfigError(message)
            return Path(value)

        return cls(
            site_url=require('SG_MCP_SITE_URL'),
            script_name=require('SG_MCP_SCRIPT_NAME'),
            api_key=require('SG_MCP_API_KEY'),
            timezone=env.get('SG_MCP_TIMEZONE', 'UTC'),
            audit_path=audit_path(),
            default_limit=integer('SG_MCP_DEFAULT_LIMIT', DEFAULT_LIMIT),
            hard_limit=integer('SG_MCP_HARD_LIMIT', HARD_LIMIT),
            schema_ttl_seconds=integer('SG_MCP_SCHEMA_TTL_SECONDS', _SCHEMA_TTL_SECONDS),
            diagnostics_path=optional_path('SG_MCP_DIAGNOSTICS_PATH'),
            timeout_seconds=integer('SG_MCP_TIMEOUT_SECONDS', _TIMEOUT_SECONDS),
        )
