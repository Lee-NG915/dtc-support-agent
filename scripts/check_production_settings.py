"""Preflight checks for the protected production environment file.

The checker reads only the path supplied with ``--env-file``. It never imports
application settings, never reads a repository ``.env`` implicitly, and never
executes the file through a shell. Output contains pass/fail labels and a
redacted database summary only; token, password, URL userinfo and query values
are never printed.

Exit codes:
    0  all checks passed
    1  the file parsed but one or more production checks failed
    2  the input file was missing, unreadable, malformed or had unsafe mode
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_INPUT = 2

SUPPORTED_MARKETS = frozenset({"au", "sg", "us"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"", "0", "false", "no", "off"})
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER_MARKERS = (
    "replace-with",
    "replace_with",
    "replace-me",
    "replace_me",
    "change-me",
    "change_me",
)
_MIN_SECRET_LENGTH = 16
_DOUBLE_QUOTE_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    '"': '"',
}


class InputError(ValueError):
    """The env file itself cannot be used for validation."""


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    label: str


@dataclass(frozen=True)
class DatabaseTarget:
    """Redaction-safe view of DATABASE_URL."""

    scheme: str
    host: str
    port: int | None
    username: str
    password: str
    database: str

    @property
    def summary(self) -> str:
        port = f":{self.port}" if self.port is not None else ""
        return f"{self.scheme or '<missing>'}://{self.host or '<missing>'}{port}"


@dataclass(frozen=True)
class HttpTarget:
    scheme: str
    host: str
    has_userinfo: bool
    has_query: bool
    has_fragment: bool

    @property
    def safe_for_epig(self) -> bool:
        return (
            self.scheme == "https"
            and bool(self.host)
            and not self.has_userinfo
            and not self.has_query
            and not self.has_fragment
        )


def _parse_double_quoted(value: str, line_number: int) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char != "\\":
            output.append(char)
            index += 1
            continue
        if index + 1 >= len(value):
            raise InputError(f"trailing escape in quoted value on line {line_number}")
        escaped = value[index + 1]
        replacement = _DOUBLE_QUOTE_ESCAPES.get(escaped)
        if replacement is None:
            raise InputError(f"unsupported escape in quoted value on line {line_number}")
        output.append(replacement)
        index += 2
    return "".join(output)


def _parse_value(value: str, line_number: int) -> str:
    if not value:
        return ""
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise InputError(f"unterminated single-quoted value on line {line_number}")
        return value[1:-1]
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            raise InputError(f"unterminated double-quoted value on line {line_number}")
        return _parse_double_quoted(value[1:-1], line_number)
    # Unquoted values stay literal. In particular, "$(...)" is never executed
    # and "${NAME}" is not expanded by this checker.
    return value


def parse_env_file(text: str) -> dict[str, str]:
    """Parse dotenv-style ``key=value`` lines without shell evaluation."""
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key):
            raise InputError(f"invalid dotenv assignment on line {line_number}")
        if key in values:
            raise InputError(f"duplicate key on line {line_number}")
        values[key] = _parse_value(value.strip(), line_number)
    return values


def load_env_file(path: Path) -> dict[str, str]:
    """Read an explicit env file after enforcing 0400/0600 permissions."""
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise InputError(f"env file does not exist: {path}") from exc
    except OSError as exc:
        raise InputError(f"cannot stat env file: {exc.strerror or type(exc).__name__}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise InputError("env file must be a regular file")
    mode = stat.S_IMODE(info.st_mode)
    if mode not in {0o400, 0o600}:
        raise InputError(f"env file permissions must be 0400 or 0600, got {mode:04o}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read env file: {exc.strerror or type(exc).__name__}") from exc
    return parse_env_file(text)


def _text(env: Mapping[str, str], key: str) -> str:
    return env.get(key, "").strip()


def _is_true(env: Mapping[str, str], key: str) -> bool:
    return _text(env, key).lower() in _TRUTHY


def _split_markets(raw: str) -> list[str]:
    return sorted({value.strip().lower() for value in raw.split(",") if value.strip()})


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _is_reserved_invalid_host(host: str) -> bool:
    return host == "invalid" or host.endswith(".invalid")


def _check_credential(name: str, value: str) -> tuple[bool, str]:
    """Check a secret without ever echoing its value."""
    if not value:
        return False, f"{name} must be set"
    if _is_placeholder(value):
        return False, f"{name} must not be a placeholder"
    if len(value) < _MIN_SECRET_LENGTH:
        return False, f"{name} must be at least {_MIN_SECRET_LENGTH} characters"
    return (
        True,
        f"{name} is set with at least {_MIN_SECRET_LENGTH} characters and is not a placeholder",
    )


def _parse_database_url(raw: str) -> DatabaseTarget | None:
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    return DatabaseTarget(
        scheme=parts.scheme.lower(),
        host=(parts.hostname or "").lower(),
        port=port,
        username=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        database=unquote(parts.path.lstrip("/")),
    )


def _parse_http_url(raw: str) -> HttpTarget | None:
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    return HttpTarget(
        scheme=parts.scheme.lower(),
        host=(parts.hostname or "").lower(),
        has_userinfo=bool(parts.username or parts.password),
        has_query=bool(parts.query),
        has_fragment=bool(parts.fragment),
    )


def validate_settings(env: Mapping[str, str]) -> list[CheckResult]:
    """Return one result per production invariant, in stable order."""
    results: list[CheckResult] = []

    def add(ok: bool, label: str) -> None:
        results.append(CheckResult(ok=ok, label=label))

    app_env = _text(env, "APP_ENV")
    add(
        app_env == "production",
        "APP_ENV is production"
        if app_env == "production"
        else f"APP_ENV must be production, got {app_env or '<missing>'}",
    )
    add(
        _is_true(env, "AFTER_SALES_ENABLED"),
        "AFTER_SALES_ENABLED is true"
        if _is_true(env, "AFTER_SALES_ENABLED")
        else "AFTER_SALES_ENABLED must be true for the production after-sales pilot",
    )

    connector = _text(env, "BUSINESS_CONNECTOR")
    add(
        connector == "epig",
        "BUSINESS_CONNECTOR is epig"
        if connector == "epig"
        else "BUSINESS_CONNECTOR must be epig; Fake is not accepted in production",
    )

    database = _parse_database_url(_text(env, "DATABASE_URL"))
    add(
        database is not None and database.scheme == "postgresql+asyncpg",
        f"DATABASE_URL dialect is {database.scheme if database else '<invalid or missing>'}"
        if database is None or database.scheme != "postgresql+asyncpg"
        else "DATABASE_URL uses postgresql+asyncpg",
    )
    add(
        database is not None and database.host == "postgres" and database.port in {None, 5432},
        (
            "DATABASE_URL targets the Compose postgres service on port 5432"
            if database is not None
            and database.host == "postgres"
            and database.port in {None, 5432}
            else "DATABASE_URL must target the Compose host postgres on port 5432"
        ),
    )

    postgres_user = _text(env, "POSTGRES_USER")
    postgres_password = _text(env, "POSTGRES_PASSWORD")
    postgres_database = _text(env, "POSTGRES_DB")
    postgres_values_present = bool(postgres_user and postgres_password and postgres_database)
    add(
        postgres_values_present,
        "POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB are set"
        if postgres_values_present
        else "POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB must all be set",
    )
    credentials_match = bool(
        database is not None
        and postgres_values_present
        and database.username == postgres_user
        and database.password == postgres_password
        and database.database == postgres_database
    )
    add(
        credentials_match,
        "DATABASE_URL user, password and database match POSTGRES_* (values not shown)"
        if credentials_match
        else "DATABASE_URL user, password and database must match POSTGRES_*",
    )
    add(*_check_credential("POSTGRES_PASSWORD", postgres_password))

    epig_url = _parse_http_url(_text(env, "EPIG_BASE_URL"))
    add(
        epig_url is not None and epig_url.safe_for_epig,
        "EPIG_BASE_URL is an absolute HTTPS URL without credentials, query or fragment"
        if epig_url is not None and epig_url.safe_for_epig
        else "EPIG_BASE_URL must be an absolute HTTPS URL without credentials, query or fragment",
    )
    add(
        epig_url is not None and not _is_reserved_invalid_host(epig_url.host),
        "EPIG_BASE_URL host is not a placeholder .invalid domain"
        if epig_url is not None and not _is_reserved_invalid_host(epig_url.host)
        else "EPIG_BASE_URL host must not use the reserved .invalid domain",
    )
    add(*_check_credential("EPIG_SERVICE_TOKEN", _text(env, "EPIG_SERVICE_TOKEN")))

    allowed_markets = _split_markets(_text(env, "EPIG_ALLOWED_MARKETS"))
    add(
        bool(allowed_markets),
        "EPIG_ALLOWED_MARKETS is non-empty"
        if allowed_markets
        else "EPIG_ALLOWED_MARKETS must list at least one market",
    )
    unsupported_markets = sorted(set(allowed_markets) - SUPPORTED_MARKETS)
    add(
        not unsupported_markets,
        "EPIG_ALLOWED_MARKETS uses supported markets"
        if not unsupported_markets
        else "EPIG_ALLOWED_MARKETS contains unsupported markets: " + ", ".join(unsupported_markets),
    )

    raw_sku_map = _text(env, "AFTER_SALES_SKU_CATEGORY_MAP")
    parsed_sku_map: object = None
    if raw_sku_map:
        try:
            parsed_sku_map = json.loads(raw_sku_map)
        except json.JSONDecodeError:
            parsed_sku_map = None
    sku_map_ok = (
        isinstance(parsed_sku_map, dict)
        and bool(parsed_sku_map)
        and all(
            isinstance(sku, str)
            and sku.strip()
            and len(sku) <= 128
            and isinstance(category, str)
            and category.strip()
            and len(category) <= 64
            for sku, category in parsed_sku_map.items()
        )
    )
    add(
        sku_map_ok,
        f"AFTER_SALES_SKU_CATEGORY_MAP is a non-empty JSON object ({len(parsed_sku_map)} entries)"
        if sku_map_ok and isinstance(parsed_sku_map, dict)
        else "AFTER_SALES_SKU_CATEGORY_MAP must be a non-empty JSON object of SKU to category",
    )
    sku_map_has_placeholders = isinstance(parsed_sku_map, dict) and any(
        _is_placeholder(sku) or _is_placeholder(category)
        for sku, category in parsed_sku_map.items()
        if isinstance(sku, str) and isinstance(category, str)
    )
    add(
        not sku_map_has_placeholders,
        "AFTER_SALES_SKU_CATEGORY_MAP contains no placeholder entries"
        if not sku_map_has_placeholders
        else "AFTER_SALES_SKU_CATEGORY_MAP must not contain placeholder SKU or category entries",
    )

    demo_login = _text(env, "AFTER_SALES_DEMO_LOGIN_ENABLED").lower()
    add(
        demo_login in _FALSY,
        "AFTER_SALES_DEMO_LOGIN_ENABLED is false"
        if demo_login in _FALSY
        else "AFTER_SALES_DEMO_LOGIN_ENABLED must be false in production",
    )

    worker_markets = _split_markets(_text(env, "AFTER_SALES_WORKER_MARKETS"))
    add(
        bool(worker_markets),
        "AFTER_SALES_WORKER_MARKETS is non-empty"
        if worker_markets
        else "AFTER_SALES_WORKER_MARKETS must list at least one market",
    )
    outside_scope = sorted(set(worker_markets) - set(allowed_markets))
    add(
        not outside_scope,
        "AFTER_SALES_WORKER_MARKETS is within EPIG_ALLOWED_MARKETS"
        if not outside_scope
        else "AFTER_SALES_WORKER_MARKETS is outside EPIG_ALLOWED_MARKETS: "
        + ", ".join(outside_scope),
    )

    add(*_check_credential("ADMIN_API_TOKEN", _text(env, "ADMIN_API_TOKEN")))
    add(*_check_credential("DEEPSEEK_API_KEY", _text(env, "DEEPSEEK_API_KEY")))

    embedding_key = _text(env, "EMBEDDING_API_KEY")
    embedding_base_url = _text(env, "EMBEDDING_BASE_URL")
    if embedding_key and embedding_base_url:
        add(True, "Embedding API key and base URL are configured")
    elif not embedding_key and not embedding_base_url:
        acknowledged = _is_true(env, "PRODUCTION_ACKNOWLEDGE_BM25_ONLY")
        add(
            acknowledged,
            "BM25-only retrieval acknowledged; dense retrieval quality is not accepted"
            if acknowledged
            else "Set EMBEDDING_API_KEY and EMBEDDING_BASE_URL, or explicitly set "
            "PRODUCTION_ACKNOWLEDGE_BM25_ONLY=true to allow BM25-only retrieval",
        )
    else:
        add(
            False,
            "EMBEDDING_API_KEY and EMBEDDING_BASE_URL must be configured together or both omitted",
        )

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a protected production env file without printing secrets."
    )
    parser.add_argument(
        "--env-file",
        required=True,
        type=Path,
        help="explicit env file to check (must be mode 0400 or 0600)",
    )
    args = parser.parse_args(argv)

    try:
        env = load_env_file(args.env_file)
    except InputError as exc:
        print(f"[FAIL] input: {exc}", file=sys.stderr)
        return EXIT_INPUT

    results = validate_settings(env)
    for result in results:
        print(f"[{'PASS' if result.ok else 'FAIL'}] {result.label}")
    failures = sum(not result.ok for result in results)
    print(f"Production settings: {len(results) - failures} passed, {failures} failed")
    return EXIT_OK if failures == 0 else EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
