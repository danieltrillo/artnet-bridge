"""Configuration loading and validation.

The config is a YAML file. See config/example.yaml for the annotated version.
Every validation error names the exact key that is wrong so it can be fixed
quickly from the web UI or the terminal.
"""

from __future__ import annotations

import ipaddress
import socket
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

from . import artnet, sacn
from .frame import ARTNET, PROTOCOLS, SACN

DEFAULT_CONFIG_PATH = "/etc/artbridge/config.yaml"

ARTNET_MODES = ("broadcast", "unicast", "auto")
SACN_MODES = ("multicast", "unicast")
LOG_LEVELS = ("debug", "info", "warning", "error")


class ConfigError(Exception):
    """Raised for an invalid configuration. The message is user facing."""


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------


@dataclass
class ArtnetNetConfig:
    enabled: bool = True
    reply: str = "broadcast"  # how ArtPollReply is sent: broadcast | unicast
    poll_interval: float = 3.0  # seconds between our own ArtPolls, 0 disables
    short_name: str = "ArtBridge"
    long_name: str = ""


@dataclass
class SacnNetConfig:
    enabled: bool = False
    ttl: int = 1  # multicast TTL for sACN we send on this network


@dataclass
class NetworkConfig:
    name: str
    ip: str
    prefix: int
    broadcast: str
    interface: str | None = None
    artnet: ArtnetNetConfig = field(default_factory=ArtnetNetConfig)
    sacn: SacnNetConfig = field(default_factory=SacnNetConfig)

    @property
    def cidr(self) -> str:
        return f"{self.ip}/{self.prefix}"

    @property
    def subnet(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.ip}/{self.prefix}", strict=False)

    def contains(self, ip: str) -> bool:
        try:
            return ipaddress.IPv4Address(ip) in self.subnet
        except ValueError:
            return False


@dataclass
class EndpointConfig:
    network: str
    protocol: str
    universe: int

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.network, self.protocol, self.universe)

    @property
    def label(self) -> str:
        return format_universe(self.protocol, self.universe)


@dataclass
class OutputConfig(EndpointConfig):
    mode: str = "broadcast"
    targets: list[str] = field(default_factory=list)
    priority: int | None = None


@dataclass
class RouteConfig:
    name: str
    source: EndpointConfig
    outputs: list[OutputConfig]
    enabled: bool = True
    # Ranges are expanded into one RouteConfig per universe; these tie the
    # pieces back to the route as written so the UI can show it as one.
    group: int = 0
    group_name: str = ""


@dataclass
class WebConfig:
    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = 8080


@dataclass
class Config:
    networks: dict[str, NetworkConfig]
    routes: list[RouteConfig]
    web: WebConfig = field(default_factory=WebConfig)
    log_level: str = "info"
    forward_sync: bool = True
    forward_other: bool = False
    node_timeout: float = 10.0
    sacn_source_name: str = "ArtBridge"
    sacn_cid: str = ""

    @property
    def cid_bytes(self) -> bytes:
        return uuid.UUID(self.sacn_cid).bytes

    def network(self, name: str) -> NetworkConfig:
        return self.networks[name]


def format_universe(protocol: str, universe: int) -> str:
    if protocol == ARTNET:
        return artnet.format_universe(universe)
    return str(universe)


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def _mapping(value: Any, path: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected a mapping of keys and values")
    return value


def _bool(value: Any, path: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "on", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0"):
        return False
    raise ConfigError(f"{path}: expected true or false, got {value!r}")


def _int(value: Any, path: str, default: int | None, lo: int, hi: int) -> int:
    if value is None:
        if default is None:
            raise ConfigError(f"{path}: is required")
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"{path}: expected a number, got {value!r}")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{path}: expected a whole number, got {value!r}") from None
    if not lo <= number <= hi:
        raise ConfigError(f"{path}: {number} is out of range ({lo}-{hi})")
    return number


def _float(value: Any, path: str, default: float, lo: float, hi: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"{path}: expected a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{path}: expected a number, got {value!r}") from None
    if not lo <= number <= hi:
        raise ConfigError(f"{path}: {number} is out of range ({lo}-{hi})")
    return number


def _str(value: Any, path: str, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, (str, int, float)):
        raise ConfigError(f"{path}: expected text, got {value!r}")
    return str(value)


def _choice(value: Any, path: str, choices: tuple[str, ...], default: str) -> str:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text not in choices:
        raise ConfigError(f"{path}: {value!r} is not one of {', '.join(choices)}")
    return text


def _ipv4(value: Any, path: str) -> str:
    text = _str(value, path).strip()
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        raise ConfigError(f"{path}: {value!r} is not an IPv4 address") from None


def _parse_universe(value: Any, protocol: str, path: str) -> int:
    try:
        if protocol == ARTNET:
            return artnet.parse_universe(value)
        return sacn.parse_universe(value)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _parse_universe_list(value: Any, protocol: str, path: str) -> list[int]:
    """A single universe, or an inclusive range written as "a-b"."""
    if isinstance(value, str) and "-" in value:
        first, _, last = value.partition("-")
        start = _parse_universe(first.strip(), protocol, path)
        end = _parse_universe(last.strip(), protocol, path)
        if end < start:
            raise ConfigError(f"{path}: range {value!r} runs backwards")
        if end - start + 1 > 512:
            raise ConfigError(f"{path}: range {value!r} covers more than 512 universes")
        return list(range(start, end + 1))
    return [_parse_universe(value, protocol, path)]


def _universe_field(spec: dict, protocol: str, path: str) -> list[int]:
    if "universes" in spec and "universe" in spec:
        raise ConfigError(f"{path}: use either 'universe' or 'universes', not both")
    if "universes" in spec:
        return _parse_universe_list(spec["universes"], protocol, f"{path}.universes")
    if "universe" not in spec or spec["universe"] is None:
        raise ConfigError(f"{path}.universe: is required")
    return _parse_universe_list(spec["universe"], protocol, f"{path}.universe")


def _unknown_keys(mapping: dict, allowed: set[str], path: str) -> None:
    extra = [k for k in mapping if k not in allowed]
    if extra:
        raise ConfigError(f"{path}: unknown key '{extra[0]}' (allowed: {', '.join(sorted(allowed))})")


# --------------------------------------------------------------------------
# Networks
# --------------------------------------------------------------------------


def _parse_network(name: str, raw: Any) -> NetworkConfig:
    path = f"networks.{name}"
    if not isinstance(name, str) or not name or not name.replace("_", "").replace("-", "").isalnum():
        raise ConfigError(f"networks: '{name}' is not a valid network name (letters, numbers, - and _ only)")
    spec = _mapping(raw, path)
    _unknown_keys(spec, {"cidr", "ip", "netmask", "prefix", "broadcast", "interface", "artnet", "sacn"}, path)

    cidr = spec.get("cidr")
    if cidr is not None:
        try:
            iface = ipaddress.IPv4Interface(str(cidr).strip())
        except ValueError:
            raise ConfigError(f"{path}.cidr: {cidr!r} is not like 2.0.0.10/8") from None
        if "/" not in str(cidr):
            raise ConfigError(f"{path}.cidr: {cidr!r} needs a prefix length, like 2.0.0.10/8")
        ip = str(iface.ip)
        prefix = iface.network.prefixlen
    elif spec.get("ip") is not None:
        ip = _ipv4(spec.get("ip"), f"{path}.ip")
        if spec.get("netmask") is not None:
            try:
                prefix = ipaddress.IPv4Network(f"0.0.0.0/{str(spec['netmask']).strip()}").prefixlen
            except ValueError:
                raise ConfigError(f"{path}.netmask: {spec['netmask']!r} is not a netmask") from None
        else:
            prefix = _int(spec.get("prefix"), f"{path}.prefix", None, 0, 32)
    else:
        raise ConfigError(f"{path}: needs 'cidr' (like 2.0.0.10/8) or 'ip' plus 'netmask'")

    network = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    if spec.get("broadcast") is not None:
        broadcast = _ipv4(spec.get("broadcast"), f"{path}.broadcast")
    else:
        broadcast = str(network.broadcast_address) if prefix < 31 else "255.255.255.255"

    art_raw = _mapping(spec.get("artnet"), f"{path}.artnet")
    _unknown_keys(art_raw, {"enabled", "reply", "poll_interval", "short_name", "long_name"}, f"{path}.artnet")
    art = ArtnetNetConfig(
        enabled=_bool(art_raw.get("enabled"), f"{path}.artnet.enabled", True),
        reply=_choice(art_raw.get("reply"), f"{path}.artnet.reply", ("broadcast", "unicast"), "broadcast"),
        poll_interval=_float(art_raw.get("poll_interval"), f"{path}.artnet.poll_interval", 3.0, 0, 3600),
        short_name=_str(art_raw.get("short_name"), f"{path}.artnet.short_name", "ArtBridge")[:17],
        long_name=_str(art_raw.get("long_name"), f"{path}.artnet.long_name", "")[:63],
    )
    sacn_raw = _mapping(spec.get("sacn"), f"{path}.sacn")
    _unknown_keys(sacn_raw, {"enabled", "ttl"}, f"{path}.sacn")
    sacn_cfg = SacnNetConfig(
        enabled=_bool(sacn_raw.get("enabled"), f"{path}.sacn.enabled", False),
        ttl=_int(sacn_raw.get("ttl"), f"{path}.sacn.ttl", 1, 1, 255),
    )
    interface = spec.get("interface")
    return NetworkConfig(
        name=name,
        ip=ip,
        prefix=prefix,
        broadcast=broadcast,
        interface=str(interface).strip() if interface else None,
        artnet=art,
        sacn=sacn_cfg,
    )


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def _check_endpoint_protocol(networks: dict[str, NetworkConfig], network: str, protocol: str, path: str) -> None:
    if network not in networks:
        known = ", ".join(sorted(networks)) or "none defined"
        raise ConfigError(f"{path}.network: '{network}' is not a known network (known: {known})")
    net = networks[network]
    if protocol == ARTNET and not net.artnet.enabled:
        raise ConfigError(f"{path}: Art-Net is turned off on network '{network}'")
    if protocol == SACN and not net.sacn.enabled:
        raise ConfigError(f"{path}: sACN is turned off on network '{network}'")


def _parse_route(index: int, raw: Any, networks: dict[str, NetworkConfig]) -> list[RouteConfig]:
    path = f"routes[{index}]"
    spec = _mapping(raw, path)
    _unknown_keys(spec, {"name", "enabled", "from", "to"}, path)
    name = _str(spec.get("name"), f"{path}.name", f"Route {index + 1}")
    enabled = _bool(spec.get("enabled"), f"{path}.enabled", True)

    src = _mapping(spec.get("from"), f"{path}.from")
    if not src:
        raise ConfigError(f"{path}.from: is required")
    _unknown_keys(src, {"network", "protocol", "universe", "universes"}, f"{path}.from")
    src_network = _str(src.get("network"), f"{path}.from.network")
    if not src_network:
        raise ConfigError(f"{path}.from.network: is required")
    src_protocol = _choice(src.get("protocol"), f"{path}.from.protocol", PROTOCOLS, ARTNET)
    _check_endpoint_protocol(networks, src_network, src_protocol, f"{path}.from")
    src_universes = _universe_field(src, src_protocol, f"{path}.from")

    raw_outputs = spec.get("to")
    if isinstance(raw_outputs, dict):
        raw_outputs = [raw_outputs]
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise ConfigError(f"{path}.to: needs at least one destination")

    outputs_per_universe: list[list[OutputConfig]] = [[] for _ in src_universes]
    for j, raw_out in enumerate(raw_outputs):
        opath = f"{path}.to[{j}]"
        out = _mapping(raw_out, opath)
        _unknown_keys(out, {"network", "protocol", "universe", "universes", "mode", "targets", "priority"}, opath)
        network = _str(out.get("network"), f"{opath}.network")
        if not network:
            raise ConfigError(f"{opath}.network: is required")
        protocol = _choice(out.get("protocol"), f"{opath}.protocol", PROTOCOLS, src_protocol)
        _check_endpoint_protocol(networks, network, protocol, opath)
        modes = ARTNET_MODES if protocol == ARTNET else SACN_MODES
        mode = _choice(out.get("mode"), f"{opath}.mode", modes, modes[0])
        raw_targets = out.get("targets")
        if isinstance(raw_targets, str):
            raw_targets = [t for t in raw_targets.replace(";", ",").split(",") if t.strip()]
        targets = [_ipv4(t, f"{opath}.targets") for t in (raw_targets or [])]
        if mode == "unicast" and not targets:
            raise ConfigError(f"{opath}.targets: unicast needs at least one target IP")
        priority = None
        if out.get("priority") is not None:
            priority = _int(out.get("priority"), f"{opath}.priority", None, 0, 200)

        universes = _universe_field(out, protocol, opath)
        if len(universes) == 1 and len(src_universes) > 1:
            # A single destination universe is the start of a run that
            # matches the source range one to one.
            start = universes[0]
            universes = []
            for i in range(len(src_universes)):
                u = start + i
                limit = artnet.MAX_PORT_ADDRESS if protocol == ARTNET else sacn.MAX_UNIVERSE
                if u > limit:
                    raise ConfigError(
                        f"{opath}.universe: range starting at {out.get('universe')!r} runs past the last universe"
                    )
                universes.append(u)
        if len(universes) != len(src_universes):
            raise ConfigError(
                f"{opath}: {len(universes)} destination universes for {len(src_universes)} source universes; they must match"
            )
        for i, u in enumerate(universes):
            cfg = OutputConfig(
                network=network, protocol=protocol, universe=u, mode=mode, targets=list(targets), priority=priority
            )
            if cfg.key == (src_network, src_protocol, src_universes[i]):
                raise ConfigError(f"{opath}: destination is the same as the source, that would be a loop")
            outputs_per_universe[i].append(cfg)

    routes = []
    for i, universe in enumerate(src_universes):
        route_name = name if len(src_universes) == 1 else f"{name} [{format_universe(src_protocol, universe)}]"
        routes.append(
            RouteConfig(
                name=route_name,
                enabled=enabled,
                source=EndpointConfig(network=src_network, protocol=src_protocol, universe=universe),
                outputs=outputs_per_universe[i],
                group=index,
                group_name=name,
            )
        )
    return routes


# --------------------------------------------------------------------------
# Top level
# --------------------------------------------------------------------------


def parse_config(raw: Any) -> Config:
    top = _mapping(raw, "config")
    _unknown_keys(
        top,
        {
            "networks",
            "routes",
            "web",
            "log_level",
            "forward_sync",
            "forward_other",
            "node_timeout",
            "sacn_source_name",
            "sacn_cid",
        },
        "config",
    )
    raw_networks = _mapping(top.get("networks"), "networks")
    if not raw_networks:
        raise ConfigError("networks: define at least one network")
    networks: dict[str, NetworkConfig] = {}
    for name, spec in raw_networks.items():
        networks[str(name)] = _parse_network(str(name), spec)
    seen_ips: dict[str, str] = {}
    for net in networks.values():
        if net.ip in seen_ips:
            raise ConfigError(f"networks.{net.name}: IP {net.ip} is already used by network '{seen_ips[net.ip]}'")
        seen_ips[net.ip] = net.name
    seen_ifaces: dict[str, str] = {}
    for net in networks.values():
        if not net.interface:
            continue
        if net.interface in seen_ifaces:
            raise ConfigError(
                f"networks.{net.name}: interface {net.interface} is already used by network "
                f"'{seen_ifaces[net.interface]}'; each network needs its own interface"
            )
        seen_ifaces[net.interface] = net.name

    raw_routes = top.get("routes")
    if raw_routes is None:
        raw_routes = []
    if not isinstance(raw_routes, list):
        raise ConfigError("routes: expected a list")
    routes: list[RouteConfig] = []
    for i, spec in enumerate(raw_routes):
        routes.extend(_parse_route(i, spec, networks))

    web_raw = _mapping(top.get("web"), "web")
    _unknown_keys(web_raw, {"enabled", "bind", "port"}, "web")
    web = WebConfig(
        enabled=_bool(web_raw.get("enabled"), "web.enabled", True),
        bind=_str(web_raw.get("bind"), "web.bind", "0.0.0.0").strip() or "0.0.0.0",
        port=_int(web_raw.get("port"), "web.port", 8080, 1, 65535),
    )

    cid = _str(top.get("sacn_cid"), "sacn_cid").strip()
    if cid:
        try:
            cid = str(uuid.UUID(cid))
        except ValueError:
            raise ConfigError(f"sacn_cid: {cid!r} is not a UUID") from None
    else:
        cid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"artbridge.{socket.gethostname()}"))

    return Config(
        networks=networks,
        routes=routes,
        web=web,
        log_level=_choice(top.get("log_level"), "log_level", LOG_LEVELS, "info"),
        forward_sync=_bool(top.get("forward_sync"), "forward_sync", True),
        forward_other=_bool(top.get("forward_other"), "forward_other", False),
        node_timeout=_float(top.get("node_timeout"), "node_timeout", 10.0, 1, 3600),
        sacn_source_name=_str(top.get("sacn_source_name"), "sacn_source_name", "ArtBridge")[:63] or "ArtBridge",
        sacn_cid=cid,
    )


def check_host_interfaces(cfg: Config, addresses: list) -> None:
    """Refuse two networks that end up on the same interface of this host.

    A network's interface is the one named in the config or, when none is
    named, the one that holds its IP. Networks whose interface cannot be
    worked out (the IP is not on this host yet) are left alone.
    `addresses` is a list of netinfo.Address.
    """
    owner: dict[str, str] = {}
    for net in cfg.networks.values():
        iface = net.interface
        if not iface:
            iface = next((a.interface for a in addresses if a.ip == net.ip), None)
        if not iface:
            continue
        if iface in owner:
            raise ConfigError(
                f"networks.{net.name}: interface {iface} is already used by network '{owner[iface]}'; "
                "each network needs its own interface"
            )
        owner[iface] = net.name


def parse_yaml(text: str) -> Config:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise ConfigError(f"YAML syntax error{where}: {getattr(exc, 'problem', exc)}") from None
    if raw is None:
        raise ConfigError("the config file is empty")
    return parse_config(raw)


def load_config(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    return parse_yaml(text)


# --------------------------------------------------------------------------
# Serialisation (used by the web UI's structured editor)
# --------------------------------------------------------------------------


def config_to_dict(cfg: Config) -> dict:
    """A plain dict in the same shape as the YAML file."""
    networks = {}
    for net in cfg.networks.values():
        entry: dict[str, Any] = {"cidr": net.cidr}
        if net.interface:
            entry["interface"] = net.interface
        if net.broadcast != str(net.subnet.broadcast_address):
            entry["broadcast"] = net.broadcast
        entry["artnet"] = asdict(net.artnet)
        entry["sacn"] = asdict(net.sacn)
        networks[net.name] = entry
    routes = []
    for route in cfg.routes:
        entry = {
            "name": route.name,
            "enabled": route.enabled,
            "from": {
                "network": route.source.network,
                "protocol": route.source.protocol,
                "universe": route.source.label,
            },
            "to": [],
        }
        for out in route.outputs:
            o: dict[str, Any] = {
                "network": out.network,
                "protocol": out.protocol,
                "universe": out.label,
                "mode": out.mode,
            }
            if out.targets:
                o["targets"] = list(out.targets)
            if out.priority is not None:
                o["priority"] = out.priority
            entry["to"].append(o)
        routes.append(entry)
    return {
        "networks": networks,
        "routes": routes,
        "web": asdict(cfg.web),
        "log_level": cfg.log_level,
        "forward_sync": cfg.forward_sync,
        "forward_other": cfg.forward_other,
        "node_timeout": cfg.node_timeout,
        "sacn_source_name": cfg.sacn_source_name,
        "sacn_cid": cfg.sacn_cid,
    }


YAML_HEADER = """# ArtBridge configuration
# Docs: https://github.com/danieltrillo/artnet-bridge#configuration
# universe for Art-Net is net.subnet.universe (0.0.0 is the first one);
# for sACN it is a plain number starting at 1.
"""


def dump_yaml(data: dict) -> str:
    return YAML_HEADER + "\n" + yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)
