#!/usr/bin/env python3
"""Provision after-sales staff credentials in an already-migrated PostgreSQL.

This is a controlled production utility. It never reads ``.env`` and never
creates or migrates schema. The database target must be supplied explicitly,
either with ``--database-url`` or with ``--database-url-file`` (a regular file
whose mode is 0400 or 0600 and which contains exactly one
``postgresql+asyncpg://`` URL). Both forms pass the same URL validation.
``create`` and ``rotate`` write the plaintext token only to a newly created 0600
file and never print it; the database stores only the existing SHA-256 digest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.exceptions import DatabaseError
from app.core.security import generate_staff_token
from app.core.time import utcnow
from app.repositories.models import AfterSalesStaffRow
from app.repositories.staff_repository import StaffRepository
from app.schemas.after_sales import (
    AFTER_SALES_MARKETS,
    STAFF_ROLES,
    AfterSalesMarket,
    StaffRole,
    is_valid_opaque_id,
)

POSTGRESQL_ASYNC_DRIVER = "postgresql+asyncpg"
OUTPUT_SCHEMA_VERSION = "1"


class ProvisioningError(RuntimeError):
    """A safe, user-facing CLI refusal."""


def _create_engine(database_url: str) -> AsyncEngine:
    """Build the async engine from the explicit URL only.

    Tests replace this seam to exercise repository behaviour without a real
    PostgreSQL server; production never falls back to settings or ``.env``.
    """
    return create_async_engine(database_url)


def _validate_database_url(raw: str, *, source: str = "--database-url") -> str:
    if not raw or not raw.strip():
        raise ProvisioningError(f"{source} is required")
    try:
        url = make_url(raw)
    except (ArgumentError, ValueError) as exc:
        raise ProvisioningError(f"{source} is not a valid SQLAlchemy URL") from exc
    if url.drivername != POSTGRESQL_ASYNC_DRIVER:
        raise ProvisioningError(f"{source} must use postgresql+asyncpg://")
    if not url.database:
        raise ProvisioningError(f"{source} must name a database")
    return raw


def _read_database_url_file(path: Path) -> str:
    """Read the only URL line from a restricted regular file.

    Error messages deliberately never include the file contents. The mode is
    checked on the opened descriptor so a path swap cannot bypass the check.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise ProvisioningError("--database-url-file could not be opened") from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ProvisioningError("--database-url-file must be a regular file")
        if stat.S_IMODE(file_stat.st_mode) not in (0o400, 0o600):
            raise ProvisioningError("--database-url-file must have mode 0400 or 0600")
        try:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                content = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise ProvisioningError("--database-url-file could not be read") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
    values = [line.strip() for line in content.splitlines() if line.strip()]
    if len(values) != 1:
        raise ProvisioningError("--database-url-file must contain exactly one non-empty URL line")
    return values[0]


def _resolve_database_url(args: argparse.Namespace) -> str:
    if args.database_url_file is not None:
        raw = _read_database_url_file(cast(Path, args.database_url_file))
        return _validate_database_url(raw, source="--database-url-file")
    return _validate_database_url(cast(str, args.database_url))


def _validate_staff_id(raw: str) -> str:
    if not raw or len(raw) > 64 or not is_valid_opaque_id(raw):
        raise ProvisioningError("--staff-id must be a 1-64 character opaque identifier")
    return raw


def _validate_display_name(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > 128:
        raise ProvisioningError("--display-name must be 1-128 characters")
    return value


def _split_list(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_roles(raw: str) -> tuple[StaffRole, ...]:
    values = _split_list(raw)
    if not values:
        raise ProvisioningError("--roles must contain at least one role")
    invalid = sorted(set(values) - STAFF_ROLES)
    if invalid:
        raise ProvisioningError(f"invalid role(s): {', '.join(invalid)}")
    return cast(tuple[StaffRole, ...], tuple(sorted(set(values))))


def _parse_markets(raw: str) -> tuple[AfterSalesMarket, ...]:
    values = _split_list(raw)
    if not values:
        raise ProvisioningError("--markets must contain at least one market")
    invalid = sorted(set(values) - AFTER_SALES_MARKETS)
    if invalid:
        raise ProvisioningError(f"invalid market(s): {', '.join(invalid)}")
    return cast(tuple[AfterSalesMarket, ...], tuple(sorted(set(values))))


def _parse_expires_at(raw: str) -> datetime:
    value = raw.strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        expires_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProvisioningError("--expires-at must be an ISO 8601 timestamp") from exc
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ProvisioningError("--expires-at must include a timezone")
    expires_at = expires_at.astimezone(UTC)
    if expires_at <= utcnow():
        raise ProvisioningError("--expires-at must be in the future")
    return expires_at


def _write_new_token_file(path: Path, payload: dict[str, str]) -> None:
    """Create a new 0600 file; never overwrite and never print its token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ProvisioningError("refusing to overwrite an existing token file") from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


async def _apply_change(
    database_url: str,
    *,
    action: str,
    staff_id: str,
    display_name: str | None = None,
    roles: tuple[StaffRole, ...] = (),
    allowed_markets: tuple[AfterSalesMarket, ...] = (),
    expires_at: datetime | None = None,
    token: str | None = None,
) -> AfterSalesStaffRow:
    engine = _create_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with factory.begin() as session:
            repository = StaffRepository(session)
            if action == "create":
                if display_name is None or expires_at is None or token is None:
                    raise ProvisioningError("create requires identity and token fields")
                return await repository.create(
                    staff_id=staff_id,
                    display_name=display_name,
                    token=token,
                    roles=roles,
                    allowed_markets=allowed_markets,
                    expires_at=expires_at,
                )
            if action == "rotate":
                if token is None:
                    raise ProvisioningError("rotate requires a token")
                row = await repository.rotate_token(staff_id, token)
            elif action == "disable":
                row = await repository.disable(staff_id)
            else:
                raise ProvisioningError(f"unknown action: {action}")
            if row is None:
                raise ProvisioningError(f"staff identity not found: {staff_id}")
            return row
    finally:
        await engine.dispose()


def _execute(args: argparse.Namespace) -> str:
    database_url = _resolve_database_url(args)
    action = args.command
    staff_id = _validate_staff_id(args.staff_id)

    if action == "disable":
        asyncio.run(_apply_change(database_url, action=action, staff_id=staff_id))
        return f"Disabled staff identity {staff_id}."

    output = Path(args.output)
    if output.exists():
        raise ProvisioningError("refusing to overwrite an existing token file")

    roles: tuple[StaffRole, ...] = ()
    allowed_markets: tuple[AfterSalesMarket, ...] = ()
    display_name: str | None = None
    expires_at: datetime | None = None
    if action == "create":
        display_name = _validate_display_name(args.display_name)
        roles = _parse_roles(args.roles)
        allowed_markets = _parse_markets(args.markets)
        expires_at = _parse_expires_at(args.expires_at)

    token = generate_staff_token()
    payload = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "action": action,
        "staff_id": staff_id,
        "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
        "token": token,
    }
    # Write the token before committing the database change. If the database
    # write fails, remove this run's new file so no undelivered token remains.
    _write_new_token_file(output, payload)
    try:
        asyncio.run(
            _apply_change(
                database_url,
                action=action,
                staff_id=staff_id,
                display_name=display_name,
                roles=roles,
                allowed_markets=allowed_markets,
                expires_at=expires_at,
                token=token,
            )
        )
    except BaseException:
        output.unlink(missing_ok=True)
        raise

    if action == "create":
        return f"Created staff identity {staff_id}. Token file: {output} (token not printed)."
    return f"Rotated token for staff identity {staff_id}. Token file: {output} (token not printed)."


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    target = common.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--database-url",
        help="Explicit postgresql+asyncpg:// URL for an already-migrated database",
    )
    target.add_argument(
        "--database-url-file",
        type=Path,
        metavar="PATH",
        help=(
            "Regular file with mode 0400 or 0600 containing exactly one "
            "postgresql+asyncpg:// URL"
        ),
    )

    create = subparsers.add_parser(
        "create", parents=[common], help="Create a staff credential and token file"
    )
    create.add_argument("--staff-id", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--roles", required=True, help="Comma-separated StaffRole values")
    create.add_argument("--markets", required=True, help="Comma-separated AfterSalesMarket values")
    create.add_argument("--expires-at", required=True, help="Timezone-aware ISO 8601 timestamp")
    create.add_argument("--output", required=True, type=Path)

    rotate = subparsers.add_parser(
        "rotate", parents=[common], help="Replace an existing staff token"
    )
    rotate.add_argument("--staff-id", required=True)
    rotate.add_argument("--output", required=True, type=Path)

    disable = subparsers.add_parser(
        "disable", parents=[common], help="Disable an existing staff identity"
    )
    disable.add_argument("--staff-id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        message = _execute(args)
    except IntegrityError:
        print(
            "staff management failed: staff_id or token digest already exists",
            file=sys.stderr,
        )
        return 2
    except (ProvisioningError, DatabaseError, OSError, ValueError) as exc:
        print(f"staff management failed: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError as exc:
        print(
            f"staff management failed: database error ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - never echo a URL or secret in a traceback
        print(
            f"staff management failed: unexpected {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
