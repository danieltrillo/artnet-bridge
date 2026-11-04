"""Command line entry point: `artbridge <command>`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import sys

from . import __version__, netinfo
from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config

EXAMPLE_CONFIG = pathlib.Path(__file__).parent / "example-config.yaml"


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def cmd_run(args: argparse.Namespace) -> int:
    from .app import Supervisor

    _setup_logging(args.log_level)
    override = None
    if args.web_port:
        override = (args.web_bind or "0.0.0.0", args.web_port)
    supervisor = Supervisor(args.config, web_override=override)
    try:
        return asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        return 0


def cmd_check(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 1
    print(f"{args.config}: ok")
    addresses = netinfo.list_addresses()
    for net in config.networks.values():
        found = netinfo.find_address(net.ip, addresses)
        where = f"on {found.interface}" if found else "NOT on any interface here"
        protocols = [p for p, on in (("Art-Net", net.artnet.enabled), ("sACN", net.sacn.enabled)) if on]
        print(f"  network {net.name}: {net.cidr} ({where}), {', '.join(protocols) or 'no protocols'}")
    for route in config.routes:
        state = "" if route.enabled else " (disabled)"
        dests = ", ".join(
            f"{o.network}/{o.protocol} {o.label} {o.mode}" + (f" -> {','.join(o.targets)}" if o.targets else "")
            for o in route.outputs
        )
        print(
            f"  route {route.name}{state}: {route.source.network}/{route.source.protocol} {route.source.label} => {dests}"
        )
    return 0


def cmd_interfaces(args: argparse.Namespace) -> int:
    for addr in netinfo.list_addresses():
        if addr.interface == "lo":
            continue
        print(f"{addr.interface:<12} {addr.cidr:<20} {addr.mac}")
    return 0


def cmd_example(args: argparse.Namespace) -> int:
    sys.stdout.write(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="artbridge", description="Art-Net / sACN subnet bridge")
    parser.add_argument("--version", action="version", version=f"artbridge {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the bridge (default)")
    run.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH, help=f"config file (default {DEFAULT_CONFIG_PATH})")
    run.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    run.add_argument("--web-port", type=int, default=0, help="override the web UI port")
    run.add_argument("--web-bind", default="", help="override the web UI bind address")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="validate a config file and show what it would do")
    check.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH)
    check.set_defaults(func=cmd_check)

    ifaces = sub.add_parser("interfaces", help="list this host's IPv4 addresses")
    ifaces.set_defaults(func=cmd_interfaces)

    example = sub.add_parser("example", help="print the annotated example config")
    example.set_defaults(func=cmd_example)

    from .testtools import add_test_commands

    add_test_commands(sub)
    return parser


COMMANDS = ("run", "check", "interfaces", "example", "send", "listen")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv = ["run"] + argv  # `artbridge -c file.yaml` means `artbridge run -c file.yaml`
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
