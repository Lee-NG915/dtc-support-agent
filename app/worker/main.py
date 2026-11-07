"""Worker loop and ``python -m app.worker`` entry point.

The loop has no in-memory lease: every claim is a database conditional update
inside :mod:`app.services.after_sales.execution`. This module only owns
process lifetime: bounded batches, a sleep between cycles, signal-aware
shutdown and closing the connector/database when the loop exits or is
cancelled.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import replace
from typing import Protocol

from app.config.settings import Settings, get_settings
from app.connectors.errors import ConnectorConfigurationError, ConnectorError
from app.core.exceptions import AppError
from app.observability.logging import configure_logging
from app.schemas.after_sales import AFTER_SALES_MARKETS
from app.services.after_sales.execution import AfterSalesExecution, RunReport
from app.services.container import AppContainer, build_container

logger = logging.getLogger(__name__)


class WorkerRuntime(Protocol):
    async def run_cycle(self, markets: list[str]) -> RunReport: ...

    async def aclose(self) -> None: ...


class ContainerWorkerRuntime:
    """Own the application container for one worker process."""

    def __init__(self, container: AppContainer, execution: AfterSalesExecution) -> None:
        self._container = container
        self._execution = execution
        workflow = container.after_sales_workflow
        if workflow is None:
            raise ConnectorConfigurationError(
                "the after-sales workflow is not available for the worker"
            )
        self._workflow = workflow

    async def run_cycle(self, markets: list[str]) -> RunReport:
        selected = sorted(set(markets))
        invalid = [market for market in selected if market not in AFTER_SALES_MARKETS]
        if invalid:
            raise ValueError(f"unsupported worker market: {invalid[0]}")
        polled, poll_failed, error_codes = await self._poll_markets(selected)
        report = await self._execution.run_once(selected)
        return replace(
            report,
            polled=polled,
            poll_failed=poll_failed,
            poll_error_codes=tuple(error_codes),
        )

    async def _poll_markets(self, markets: list[str]) -> tuple[int, int, list[str]]:
        """Pull at most one bounded feed page per market.

        Cursor advancement and case storage share the 2b transaction, so a
        failure leaves the cursor untouched. The rest of the cycle still runs:
        another market may succeed and already-approved operations may remain
        executable.
        """
        polled = 0
        failed = 0
        error_codes: list[str] = []
        for market in markets:
            try:
                await self._workflow.sync_service(market)
            except Exception as exc:  # noqa: BLE001 - one market must not stop the cycle
                # CancelledError derives from BaseException and therefore is
                # not swallowed here; shutdown still propagates to aclose().
                code = _safe_sync_code(exc)
                logger.warning(
                    "after_sales worker service poll failed market=%s code=%s",
                    market,
                    code,
                )
                failed += 1
                error_codes.append(f"{market}:{code}")
            else:
                polled += 1
        return polled, failed, error_codes

    async def aclose(self) -> None:
        await self._container.aclose()


def _safe_sync_code(exc: BaseException) -> str:
    """Classify a poll failure without persisting or logging its message."""
    if isinstance(exc, ConnectorError):
        return exc.code
    if isinstance(exc, AppError):
        return exc.error_code.value
    return "SYNC_FAILED"


def _connector_scope(settings: Settings | None) -> frozenset[str]:
    """Markets the selected business connector is allowed to serve."""
    if settings is None or settings.business_connector != "epig":
        return AFTER_SALES_MARKETS
    values = {
        value.strip().lower() for value in settings.epig_allowed_markets.split(",") if value.strip()
    }
    if not values:
        raise ValueError("EPIG_ALLOWED_MARKETS is required for the worker")
    invalid = sorted(values - AFTER_SALES_MARKETS)
    if invalid:
        raise ValueError(f"unsupported connector market: {invalid[0]}")
    return frozenset(values)


def parse_markets(raw: str | None, *, settings: Settings | None = None) -> list[str]:
    scope = _connector_scope(settings)
    if raw is None:
        if settings is not None and settings.after_sales_worker_markets.strip():
            raw = settings.after_sales_worker_markets
        elif settings is not None and settings.business_connector == "epig":
            raw = settings.epig_allowed_markets
        else:
            raw = ""
    values = sorted({value.strip().lower() for value in (raw or "").split(",") if value.strip()})
    if not values:
        return sorted(scope)
    invalid = [value for value in values if value not in AFTER_SALES_MARKETS]
    if invalid:
        raise ValueError(f"unsupported worker market: {invalid[0]}")
    outside = [value for value in values if value not in scope]
    if outside:
        raise ValueError(f"worker market outside connector scope: {outside[0]}")
    return [str(value) for value in values]


async def build_runtime(settings: Settings) -> WorkerRuntime:
    if not settings.after_sales_enabled:
        raise ConnectorConfigurationError(
            "AFTER_SALES_ENABLED must be true before starting the after-sales worker"
        )
    container = await build_container(settings)
    execution = container.after_sales_execution
    if execution is None:
        await container.aclose()
        raise ConnectorConfigurationError(
            "the after-sales workflow/connector is not available for the worker"
        )
    return ContainerWorkerRuntime(container, execution)


async def run_worker_loop(
    runtime: WorkerRuntime,
    markets: list[str],
    *,
    once: bool,
    max_cycles: int,
    poll_interval: float,
    stop_event: asyncio.Event | None = None,
) -> int:
    if max_cycles < 0:
        raise ValueError("max_cycles cannot be negative")
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    stop = stop_event or asyncio.Event()
    cycles = 0
    try:
        while not stop.is_set():
            await runtime.run_cycle(markets)
            cycles += 1
            if once or (max_cycles > 0 and cycles >= max_cycles):
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except TimeoutError:
                continue
    finally:
        # Closing is required on normal completion, a bounded cycle limit,
        # Ctrl-C or task cancellation. It does not rewrite an in-flight write.
        await runtime.aclose()
    return cycles


async def run_worker(
    settings: Settings,
    *,
    markets: list[str],
    once: bool,
    max_cycles: int,
    poll_interval: float,
    stop_event: asyncio.Event | None = None,
) -> int:
    runtime = await build_runtime(settings)
    return await run_worker_loop(
        runtime,
        markets,
        once=once,
        max_cycles=max_cycles,
        poll_interval=poll_interval,
        stop_event=stop_event,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.worker",
        description="Bounded after-sales execution/reconciliation worker",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one bounded batch, then exit (useful for local acceptance)",
    )
    parser.add_argument(
        "--markets",
        default=None,
        help="comma-separated markets; defaults to worker/epig config or all",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="stop after N cycles; 0 with no --once means run until signalled",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="seconds between cycles; defaults to AFTER_SALES_WORKER_POLL_INTERVAL_SECONDS",
    )
    return parser


async def _amain(argv: list[str] | None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)
    markets = parse_markets(args.markets, settings=settings)
    poll_interval = (
        args.poll_interval
        if args.poll_interval is not None
        else settings.after_sales_worker_poll_interval_seconds
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            # Signal handlers are unavailable on some event-loop/platform
            # combinations; cancellation still reaches the loop's finally.
            pass
    try:
        await run_worker(
            settings,
            markets=markets,
            once=args.once,
            max_cycles=args.max_cycles,
            poll_interval=poll_interval,
            stop_event=stop,
        )
    except (ConnectorConfigurationError, ValueError) as exc:
        logger.error("after_sales worker configuration error: %s", exc)
        return 2
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(signum)
            except (NotImplementedError, RuntimeError):
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ContainerWorkerRuntime",
    "WorkerRuntime",
    "build_parser",
    "build_runtime",
    "main",
    "parse_markets",
    "run_worker",
    "run_worker_loop",
]
