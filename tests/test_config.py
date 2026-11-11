import pathlib
import uuid

import pytest

from artbridge.config import ConfigError, config_to_dict, dump_yaml, load_config, parse_config, parse_yaml

EXAMPLE = pathlib.Path(__file__).resolve().parent.parent / "artbridge" / "example-config.yaml"

BASE = {
    "networks": {
        "console": {"cidr": "2.0.0.10/8", "artnet": {"enabled": True}, "sacn": {"enabled": True}},
        "stage": {"cidr": "10.20.30.5/24", "artnet": {"enabled": True}, "sacn": {"enabled": True}},
    },
    "routes": [],
}


def with_routes(*routes):
    cfg = {"networks": BASE["networks"], "routes": list(routes)}
    return cfg


def test_example_config_parses():
    cfg = load_config(str(EXAMPLE))
    assert list(cfg.networks) == ["console", "stage"]
    console = cfg.networks["console"]
    assert console.ip == "2.0.0.10" and console.prefix == 8 and console.broadcast == "2.255.255.255"
    assert cfg.networks["stage"].broadcast == "10.20.30.255"
    assert cfg.networks["stage"].sacn.enabled
    assert len(cfg.routes) == 6
    assert cfg.routes[0].name == "Console to stage [0.0.0]"
    assert [r.source.universe for r in cfg.routes[:4]] == [0, 1, 2, 3]
    assert [r.outputs[0].universe for r in cfg.routes[:4]] == [0, 1, 2, 3]
    assert not cfg.routes[4].enabled
    assert cfg.routes[4].outputs[0].targets == ["10.20.30.50", "10.20.30.51"]
    assert cfg.routes[5].outputs[0].protocol == "sacn" and cfg.routes[5].outputs[0].universe == 6
    uuid.UUID(cfg.sacn_cid)
    assert cfg.web.port == 8080


def test_range_with_offset_destination():
    cfg = parse_config(
        with_routes(
            {
                "name": "r",
                "from": {"network": "console", "universes": "0.0.0-0.0.3"},
                "to": [{"network": "stage", "universe": "0.1.0"}],
            }
        )
    )
    assert [r.outputs[0].universe for r in cfg.routes] == [16, 17, 18, 19]
    assert [r.source.universe for r in cfg.routes] == [0, 1, 2, 3]


def test_single_route_shapes():
    cfg = parse_config(
        with_routes(
            {
                "from": {"network": "console", "universe": 7},
                "to": {"network": "stage", "universe": "0.0.9", "mode": "auto"},
            }
        )
    )
    assert len(cfg.routes) == 1
    route = cfg.routes[0]
    assert route.name == "Route 1"
    assert route.source.key == ("console", "artnet", 7)
    assert route.outputs[0].key == ("stage", "artnet", 9)
    assert route.outputs[0].mode == "auto"


def test_targets_as_string_and_priority():
    cfg = parse_config(
        with_routes(
            {
                "from": {"network": "console", "universe": 1},
                "to": [
                    {"network": "stage", "universe": 1, "mode": "unicast", "targets": "10.20.30.50, 10.20.30.51"},
                    {"network": "stage", "protocol": "sacn", "universe": 5, "priority": 120},
                ],
            }
        )
    )
    outs = cfg.routes[0].outputs
    assert outs[0].targets == ["10.20.30.50", "10.20.30.51"]
    assert outs[1].mode == "multicast" and outs[1].priority == 120


def test_ip_and_netmask_form_with_broadcast_override():
    cfg = parse_config(
        {
            "networks": {"a": {"ip": "2.0.0.1", "netmask": "255.0.0.0", "broadcast": "255.255.255.255"}},
            "routes": [],
        }
    )
    net = cfg.networks["a"]
    assert net.prefix == 8 and net.cidr == "2.0.0.1/8" and net.broadcast == "255.255.255.255"
    assert net.contains("2.1.1.1") and not net.contains("10.0.0.1")


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({}, "networks: define at least one"),
        ({"networks": {"a": {"cidr": "2.0.0.10"}}}, "prefix length"),
        ({"networks": {"a": {"cidr": "2.0.0.10/8", "bogus": 1}}}, "unknown key 'bogus'"),
        ({"networks": {"a": {"cidr": "2.0.0.10/8"}, "b": {"cidr": "2.0.0.10/16"}}}, "already used"),
        ({"networks": {"a!": {"cidr": "2.0.0.10/8"}}}, "not a valid network name"),
        (
            with_routes({"from": {"network": "nope", "universe": 1}, "to": [{"network": "stage", "universe": 1}]}),
            "not a known network",
        ),
        (with_routes({"from": {"network": "console", "universe": 1}, "to": []}), "at least one destination"),
        (
            with_routes(
                {
                    "from": {"network": "console", "universe": 1},
                    "to": [{"network": "stage", "universe": 1, "mode": "unicast"}],
                }
            ),
            "needs at least one target",
        ),
        (
            with_routes({"from": {"network": "console", "universe": 1}, "to": [{"network": "console", "universe": 1}]}),
            "would be a loop",
        ),
        (
            with_routes({"from": {"network": "console", "universe": 0.1}, "to": [{"network": "stage", "universe": 1}]}),
            "ambiguous",
        ),
        (
            with_routes(
                {
                    "from": {"network": "console", "universe": "0.0.0-0.0.3"},
                    "to": [{"network": "stage", "universe": "0.0.0-0.0.1"}],
                }
            ),
            "must match",
        ),
        (
            with_routes(
                {"from": {"network": "console", "universe": "0.0.3-0.0.0"}, "to": [{"network": "stage", "universe": 1}]}
            ),
            "runs backwards",
        ),
        (
            with_routes(
                {
                    "from": {"network": "console", "protocol": "sacn", "universe": 0},
                    "to": [{"network": "stage", "universe": 1}],
                }
            ),
            "out of range",
        ),
        (
            with_routes(
                {
                    "from": {"network": "console", "universe": 1},
                    "to": [{"network": "stage", "universe": 1, "mode": "multicast"}],
                }
            ),
            "not one of",
        ),
        ({"networks": BASE["networks"], "routes": [], "web": {"port": 99999}}, "out of range"),
        ({"networks": BASE["networks"], "routes": [], "sacn_cid": "not-a-uuid"}, "not a UUID"),
        ({"networks": BASE["networks"], "routes": "nope"}, "expected a list"),
    ],
)
def test_errors_name_the_problem(raw, fragment):
    with pytest.raises(ConfigError) as info:
        parse_config(raw)
    assert fragment in str(info.value)


def test_protocol_disabled_on_network():
    raw = {
        "networks": {"a": {"cidr": "2.0.0.10/8", "sacn": {"enabled": False}}, "b": {"cidr": "10.0.0.1/24"}},
        "routes": [
            {"from": {"network": "a", "protocol": "sacn", "universe": 1}, "to": [{"network": "b", "universe": 1}]}
        ],
    }
    with pytest.raises(ConfigError) as info:
        parse_config(raw)
    assert "sACN is turned off" in str(info.value)


def test_yaml_errors_are_friendly():
    with pytest.raises(ConfigError) as info:
        parse_yaml("networks: [\n")
    assert "YAML syntax error" in str(info.value)
    with pytest.raises(ConfigError):
        parse_yaml("")


def test_dict_and_yaml_roundtrip():
    cfg = load_config(str(EXAMPLE))
    data = config_to_dict(cfg)
    again = parse_config(data)
    assert [r.source.key for r in again.routes] == [r.source.key for r in cfg.routes]
    assert [[o.key for o in r.outputs] for r in again.routes] == [[o.key for o in r.outputs] for r in cfg.routes]
    text = dump_yaml(data)
    third = parse_yaml(text)
    assert third.networks["stage"].cidr == "10.20.30.5/24"
    assert third.sacn_cid == cfg.sacn_cid


# -- interfaces ------------------------------------------------------------


def _two_nets(a_iface=None, b_iface=None, b_cidr="10.0.0.6/22"):
    nets = {"console": {"cidr": "10.0.0.5/22"}, "stage": {"cidr": b_cidr}}
    if a_iface:
        nets["console"]["interface"] = a_iface
    if b_iface:
        nets["stage"]["interface"] = b_iface
    return {"networks": nets, "routes": []}


def test_same_interface_on_two_networks_is_rejected():
    with pytest.raises(ConfigError, match="interface eth85 is already used by network 'console'"):
        parse_config(_two_nets("eth85", "eth85"))


def test_different_interfaces_are_fine():
    cfg = parse_config(_two_nets("eth85", "eth82", "10.201.82.64/24"))
    assert cfg.networks["stage"].interface == "eth82"


def test_host_check_catches_two_networks_on_one_interface_by_ip():
    from artbridge import netinfo
    from artbridge.config import check_host_interfaces

    addresses = [
        netinfo.Address("eth85", 3, "10.0.0.5", 22),
        netinfo.Address("eth85", 3, "10.0.0.6", 22),
        netinfo.Address("eth82", 2, "10.201.82.64", 24),
    ]
    with pytest.raises(ConfigError, match="each network needs its own interface"):
        check_host_interfaces(parse_config(_two_nets()), addresses)
    # explicit name for one, IP lookup for the other: still the same NIC
    with pytest.raises(ConfigError, match="eth85"):
        check_host_interfaces(parse_config(_two_nets("eth85")), addresses)
    check_host_interfaces(parse_config(_two_nets(None, None, "10.201.82.64/24")), addresses)
    # an IP that is not on this host yet is not judged
    check_host_interfaces(parse_config(_two_nets("eth85", None, "192.168.9.9/24")), addresses)


def test_range_route_keeps_its_group():
    cfg = parse_config(
        {
            "networks": BASE["networks"],
            "routes": [
                {
                    "name": "single",
                    "from": {"network": "console", "universe": "0.0.0"},
                    "to": [{"network": "stage", "universe": "0.0.0"}],
                },
                {
                    "name": "big",
                    "from": {"network": "console", "universes": "0.1.0-1.2.15"},
                    "to": [{"network": "stage", "universe": "0.1.0"}],
                },
            ],
        }
    )
    big = [r for r in cfg.routes if r.group_name == "big"]
    assert len(big) == 288
    assert {r.group for r in big} == {1}
    assert cfg.routes[0].group == 0 and cfg.routes[0].group_name == "single"
