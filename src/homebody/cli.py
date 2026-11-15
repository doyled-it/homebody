"""Command-line entry point for homebody.

With no subcommand it prints the version and the controller URL so an install
can confirm the app is wired up. The ``matter`` subcommands drive the Matter
controller for the first-task proof: commission the Nest with a sharing code,
read its live mode, temperature, and setpoint, and write the setpoint or mode.

``homebody run`` starts the service loop (presence, decisions, thermostat
control) and the settings API; the homebody systemd unit runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from homebody import (
    __version__,
    api,
    config,
    daemon,
    garage_service,
    matter,
    presence,
)

# A connect factory turns a controller URL into an async context manager that
# yields a MatterConnection. The default opens a real WebSocket; tests inject a
# factory that yields an in-memory fake controller.
# How long the garage task gets to finish after stop before it is cancelled.
GARAGE_STOP_SECONDS = 5.0

ConnectFactory = Callable[[str], AbstractAsyncContextManager[Any]]


def _matter_url() -> str:
    """The controller WebSocket URL: the env override, else the loopback default."""
    return os.environ.get("HOMEBODY_MATTER_URL", matter.DEFAULT_MATTER_URL)


def _default_connect(url: str) -> AbstractAsyncContextManager[Any]:
    return matter.WebSocketConnection(url)


def main(
    argv: list[str] | None = None,
    *,
    connect: ConnectFactory | None = None,
    run_daemon: Callable[[config.Config], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="homebody",
        description="Presence-based climate control over Matter.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    _add_matter(subparsers)
    run_cmd = subparsers.add_parser(
        "run", help="run the presence and thermostat service loop"
    )
    run_cmd.add_argument(
        "--config",
        default=os.environ.get("HOMEBODY_CONFIG", str(config.DEFAULT_CONFIG_PATH)),
        help="the local config file (default: /etc/homebody/local.toml)",
    )

    args = parser.parse_args(argv)

    if args.command == "matter":
        return _run_matter(args, connect or _default_connect)

    if args.command == "run":
        return _run_service(args.config, run_daemon or _serve)

    print(f"homebody {__version__}: controller {_matter_url()}")
    return 0


def _run_service(path: str, run_daemon: Callable[[config.Config], None]) -> int:
    try:
        settings = config.load(path)
    except config.ConfigError as error:
        print(f"config error: {error}", file=sys.stderr)
        return 1
    run_daemon(settings)
    return 0


def _serve(settings: config.Config) -> None:
    """Run the real daemon until SIGTERM or SIGINT."""
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )

    asyncio.run(_run_with_signals(settings))


async def _run_with_signals(settings: config.Config) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await _serve_async(settings, stop)


async def _serve_async(
    settings: config.Config,
    stop: asyncio.Event,
    *,
    build_daemon: Callable[..., daemon.Daemon] = daemon.build,
    link_factory: Callable[[Any], Any] | None = None,
    on_started: Callable[[asyncio.Server | None, daemon.Daemon], None] | None = None,
) -> None:
    """Run the daemon, the settings API and (when configured) the garage until
    ``stop``. The garage is its own task, so nothing in it can hold up a tick."""
    override = os.environ.get("HOMEBODY_MATTER_URL")
    service = build_daemon(settings, url=override)
    garage = garage_service.build(settings, service, link_factory=link_factory)
    if garage is not None:
        service.attach_garage(garage.snapshot)
    server = await _start_api(service, settings.api_port, garage)
    garage_task = (
        asyncio.create_task(garage.run(stop)) if garage is not None else None
    )
    if on_started is not None:
        on_started(server, service)
    try:
        await service.run(stop)
    finally:
        stop.set()
        if garage_task is not None:
            await _stop_garage(garage_task)
        if server is not None:
            server.close()
            await server.wait_closed()


async def _stop_garage(task: asyncio.Task[None]) -> None:
    """Let the garage task finish after ``stop``; cancel it if it does not."""
    try:
        await asyncio.wait_for(task, GARAGE_STOP_SECONDS)
    except asyncio.CancelledError:
        raise
    except BaseException:
        logging.getLogger(__name__).warning("garage task did not stop cleanly")


async def _start_api(
    service: daemon.Daemon,
    port: int,
    garage: garage_service.GarageService | None = None,
) -> asyncio.Server | None:
    """Start the settings API on loopback. Presence control matters more than
    the API, so a port that cannot be bound is logged and the loop runs anyway."""
    try:
        return await api.start(
            service, "127.0.0.1", port, presence.neighbour_mac, garage=garage
        )
    except OSError:
        logging.getLogger(__name__).exception(
            "settings API could not listen on port %d", port
        )
        return None


def _add_matter(subparsers: argparse._SubParsersAction) -> None:
    matter_cmd = subparsers.add_parser(
        "matter", help="talk to the Matter controller and the thermostat"
    )
    actions = matter_cmd.add_subparsers(dest="action", required=True)

    actions.add_parser(
        "status", help="print the thermostat's mode, temperature, and setpoint"
    )

    commission = actions.add_parser(
        "commission", help="commission a device with a QR or manual pairing code"
    )
    commission.add_argument("code", help="the Matter pairing or sharing code")

    set_setpoint = actions.add_parser(
        "set-setpoint", help="write the occupied cooling setpoint, in Fahrenheit"
    )
    set_setpoint.add_argument("fahrenheit", type=float, help="the setpoint in F")

    set_mode = actions.add_parser(
        "set-mode", help="write the thermostat's system mode"
    )
    set_mode.add_argument(
        "mode",
        choices=sorted(name for name in matter.SYSTEM_MODE_NAMES.values()),
        help="the system mode to set",
    )


def _run_matter(args: argparse.Namespace, connect: ConnectFactory) -> int:
    try:
        return asyncio.run(_dispatch_matter(args, connect))
    except matter.MatterError as error:
        print(f"matter error: {error}", file=sys.stderr)
        return 1


async def _dispatch_matter(args: argparse.Namespace, connect: ConnectFactory) -> int:
    url = _matter_url()
    async with connect(url) as connection:
        client = matter.MatterClient(connection)
        if args.action == "status":
            return await _status(client)
        if args.action == "commission":
            return await _commission(client, args.code)
        if args.action == "set-setpoint":
            return await _set_setpoint(client, args.fahrenheit)
        if args.action == "set-mode":
            return await _set_mode(client, args.mode)
    return 1


async def _resolve_thermostat(client: matter.MatterClient) -> tuple[int, int] | None:
    nodes = await client.get_nodes()
    return matter.find_thermostat(nodes)


async def _status(client: matter.MatterClient) -> int:
    found = await _resolve_thermostat(client)
    if found is None:
        print("no thermostat commissioned", file=sys.stderr)
        return 1
    node_id, endpoint = found
    state = await client.read_thermostat(node_id, endpoint=endpoint)
    print(
        f"thermostat node {node_id}: mode {state.system_mode_name}, "
        f"now {_format_f(state.local_temperature_f)}, "
        f"cooling setpoint {_format_f(state.cooling_setpoint_f)}"
    )
    return 0


async def _commission(client: matter.MatterClient, code: str) -> int:
    node = await client.commission(code)
    print(f"commissioned node {node['node_id']}")
    return 0


async def _set_setpoint(client: matter.MatterClient, fahrenheit: float) -> int:
    found = await _resolve_thermostat(client)
    if found is None:
        print("no thermostat commissioned", file=sys.stderr)
        return 1
    node_id, endpoint = found
    await client.set_cooling_setpoint_f(node_id, fahrenheit, endpoint=endpoint)
    print(f"cooling setpoint set to {_format_f(fahrenheit)}")
    return 0


async def _set_mode(client: matter.MatterClient, mode_name: str) -> int:
    found = await _resolve_thermostat(client)
    if found is None:
        print("no thermostat commissioned", file=sys.stderr)
        return 1
    node_id, endpoint = found
    mode = next(
        value for value, name in matter.SYSTEM_MODE_NAMES.items() if name == mode_name
    )
    await client.set_system_mode(node_id, mode, endpoint=endpoint)
    print(f"system mode set to {mode_name}")
    return 0


def _format_f(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f}F"


if __name__ == "__main__":
    raise SystemExit(main())
