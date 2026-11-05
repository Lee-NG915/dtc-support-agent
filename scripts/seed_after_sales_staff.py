#!/usr/bin/env python3
"""Seed synthetic after-sales staff into a brand-new local SQLite database.

This is an explicit development/testing utility. It never reads .env, never
touches an existing database, refuses production, writes bearer tokens only
to a newly created 0600 file, and never prints a token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.security import generate_staff_token
from app.core.time import utcnow
from app.repositories.models import Base
from app.repositories.staff_repository import StaffRepository
from app.schemas.after_sales import AfterSalesMarket, StaffRole

DEFAULT_OUTPUT = Path("data/local/after_sales_staff.json")


class SeedError(RuntimeError):
    """A safe, user-facing seed refusal."""


@dataclass(frozen=True)
class DemoStaff:
    staff_id: str
    display_name: str
    roles: tuple[StaffRole, ...]
    allowed_markets: tuple[AfterSalesMarket, ...]


DEMO_STAFF: tuple[DemoStaff, ...] = (
    DemoStaff(
        staff_id="staff_support_au",
        display_name="Synthetic Support AU",
        roles=("support",),
        allowed_markets=("au",),
    ),
    DemoStaff(
        staff_id="staff_approver_au",
        display_name="Synthetic Approver AU/SG",
        roles=("approver",),
        allowed_markets=("au", "sg"),
    ),
    DemoStaff(
        staff_id="staff_auditor_all",
        display_name="Synthetic Auditor All Markets",
        roles=("auditor",),
        allowed_markets=("au", "sg", "us"),
    ),
    DemoStaff(
        staff_id="staff_operator_au",
        display_name="Synthetic Operator AU",
        roles=("operator",),
        allowed_markets=("au",),
    ),
    DemoStaff(
        staff_id="staff_support_us",
        display_name="Synthetic Support US",
        roles=("support",),
        allowed_markets=("us",),
    ),
)


async def _seed_database(
    database_url: str,
    *,
    expires_at: datetime,
) -> list[dict[str, str]]:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        credentials: list[dict[str, str]] = []
        async with factory.begin() as session:
            repository = StaffRepository(session)
            for demo in DEMO_STAFF:
                token = generate_staff_token()
                await repository.create(
                    staff_id=demo.staff_id,
                    display_name=demo.display_name,
                    token=token,
                    roles=demo.roles,
                    allowed_markets=demo.allowed_markets,
                    expires_at=expires_at,
                )
                credentials.append(
                    {
                        "staff_id": demo.staff_id,
                        "display_name": demo.display_name,
                        "roles": ",".join(demo.roles),
                        "allowed_markets": ",".join(demo.allowed_markets),
                        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                        "token": token,
                    }
                )
        return credentials
    finally:
        await engine.dispose()


def _resolve_sqlite_target(raw: str) -> tuple[str, Path]:
    url = make_url(raw)
    if not url.drivername.startswith("sqlite"):
        raise SeedError("seed only supports a new local SQLite database")
    if url.query:
        raise SeedError("seed database URL must not contain query parameters")
    database = url.database
    if not database or database == ":memory:":
        raise SeedError("seed requires a new file-backed SQLite database")
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    if path.exists():
        raise SeedError("seed refuses to operate on an existing database")
    path.parent.mkdir(parents=True, exist_ok=True)
    async_url = url.set(drivername="sqlite+aiosqlite", database=str(path))
    return async_url.render_as_string(hide_password=False), path


def _write_new_credentials(path: Path, payload: dict[str, object]) -> None:
    if path.exists():
        raise SeedError("refusing to overwrite an existing credentials file")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise


def _select_app_env(explicit: str | None) -> str:
    values = {value.lower() for value in (explicit, os.getenv("APP_ENV")) if value}
    if "production" in values:
        raise SeedError("seed is forbidden in production")
    if values - {"development", "testing"}:
        raise SeedError("seed only runs in development or testing")
    return explicit or os.getenv("APP_ENV") or "development"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--app-env", choices=["development", "testing"], default=None)
    parser.add_argument("--expires-in-days", type=int, default=7)
    args = parser.parse_args(argv)
    if not 1 <= args.expires_in_days <= 30:
        print("seed failed: --expires-in-days must be between 1 and 30", file=sys.stderr)
        return 2

    database_path: Path | None = None
    try:
        app_env = _select_app_env(args.app_env)
        if args.output.exists():
            raise SeedError("refusing to overwrite an existing credentials file")
        database_url, database_path = _resolve_sqlite_target(args.database_url)
        expires_at = utcnow() + timedelta(days=args.expires_in_days)
        credentials = asyncio.run(_seed_database(database_url, expires_at=expires_at))
        _write_new_credentials(
            args.output,
            {
                "schema_version": "1",
                "app_env": app_env,
                "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
                "staff": credentials,
            },
        )
    except (SeedError, OSError, ValueError) as exc:
        if database_path is not None and database_path.exists():
            database_path.unlink(missing_ok=True)
        print(f"seed failed: {exc}", file=sys.stderr)
        return 2

    print(
        f"Seeded {len(credentials)} synthetic staff. "
        f"Credentials file: {args.output} (tokens were not printed)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
