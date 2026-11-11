# ArtBridge

An Art-Net / sACN bridge for lighting networks that span more than one subnet.

Consoles like GrandMA3 only send Art-Net to addresses inside their own subnet.
They will not hand packets to a router. ArtBridge is a small service with one
leg in each subnet: the console talks to it as if it were a normal node, and
it re-sends every universe where you tell it to.

```
   console network (2.x.x.x)                   stage network (10.20.30.x)
   ┌─────────────┐                              ┌────────────┐
   │  GrandMA3   │  ArtDmx 0.0.0 - 0.0.3        │  node A    │
   │  2.0.0.1    │ ───────────────┐             │ 10.20.30.50│
   └─────────────┘                │             └────────────┘
                           ┌──────▼──────┐      ┌────────────┐
       ArtPoll  ◄────────► │  ArtBridge  │ ───► │  node B    │  broadcast,
       ArtPollReply        │  2.0.0.10   │      │ 10.20.30.51│  unicast or
       (looks like a node) │  10.20.30.5 │      └────────────┘  sACN out
                           └─────────────┘
```

Runs in a Debian LXC on Proxmox, on a Pi, or on any Linux box. Python 3.11+,
no special privileges.

## What it does

- Forwards Art-Net DMX (ArtDmx and ArtNzs) between any number of networks,
  broadcast or unicast, with per universe remapping.
- Answers ArtPoll on every network with a proper ArtPollReply, so the console
  discovers it and shows the universes it accepts.
- Polls the other side itself and keeps a list of the nodes it finds.
  `mode: auto` then unicasts to exactly the nodes that announced a universe.
- sACN (E1.31) in and out, multicast or unicast, with priority, plus universe
  discovery packets. Art-Net to sACN and back.
- Passes ArtSync through (and optionally ArtTimeCode / ArtTrigger / ArtCommand).
- Never forwards its own packets back, so there is no loop.
- Web UI on port 8080: live rates per route, discovered nodes, "seen but not
  routed" universes, a form based config editor, and the log.
- One YAML config file. Config changes apply live without a restart.

## Quick start on Proxmox

On the Proxmox host, as root. Adjust the VLAN tags and IPs to your networks:

```bash
curl -fsSLO https://raw.githubusercontent.com/danieltrillo/artnet-bridge/main/deploy/proxmox-create-lxc.sh
CTID=210 \
NET0="bridge=vmbr0,tag=10,ip=2.0.0.10/8" \
NET1="bridge=vmbr0,tag=20,ip=10.20.30.5/24" \
MGMT="bridge=vmbr0,ip=dhcp" \
bash proxmox-create-lxc.sh
```

That creates an unprivileged Debian 12 container with `eth0` in the console
network, `eth1` in the stage network, and an optional `eth2` on DHCP so apt
can reach the internet. It installs ArtBridge and starts it. Then open
`http://<container-ip>:8080`.

Untagged port: leave out `tag=`. Need a gateway: add `,gw=192.168.1.1` to
whichever NIC has internet.

## Quick start on any Debian / Ubuntu box

```bash
curl -fsSL https://raw.githubusercontent.com/danieltrillo/artnet-bridge/main/deploy/install.sh | sudo bash
```

This installs `python3-yaml` and `python3-aiohttp` from apt, copies the code
to `/opt/artbridge`, writes `/etc/artbridge/config.yaml` from the example, and
enables the `artbridge` systemd service.

```bash
artbridge interfaces                       # which IPs does this box have?
artbridge check -c /etc/artbridge/config.yaml
sudo nano /etc/artbridge/config.yaml       # or use the web UI
sudo systemctl reload artbridge            # re-read the config (also happens on web Save)
journalctl -u artbridge -f                 # logs
```

## Set up the GrandMA3

1. Give the MA an IP in the console network, for example `2.0.0.1/8`. Give
   the bridge an IP in the same subnet (`2.0.0.10/8` in the examples).
2. Menu → **DMX Protocols** → **Art-Net**. Add an output line for each
   universe (or one line with an amount). Set **Net**, **Subnet** and
   **Universe** to match the route's `from` universe.
3. **Destination IP**: either the broadcast address (`2.255.255.255`) or the
   bridge's IP (`2.0.0.10`) for unicast. Both work. The bridge shows up in the
   MA's Art-Net node list as "ArtBridge" with its universes.
4. Turn the output on. The Status tab of the web UI should show the route
   going live within a second.

Art-Net universe numbers are written `net.subnet.universe` everywhere in
ArtBridge. `0.0.0` is the first universe. Some devices show that one as
"universe 1", so check which way your gear counts.

## Configuration

`/etc/artbridge/config.yaml`. The full annotated example is in
[`artbridge/example-config.yaml`](artbridge/example-config.yaml) and
`artbridge example` prints it.

```yaml
networks:
  console:                        # any name
    cidr: 2.0.0.10/8              # this box's IP on that network
    artnet:
      enabled: true
      reply: broadcast            # ArtPollReply: broadcast | unicast
      poll_interval: 3            # seconds between our own ArtPolls, 0 = never
      short_name: ArtBridge       # how the console lists us (max 17 chars)
    sacn:
      enabled: false
  stage:
    cidr: 10.20.30.5/24
    artnet: { enabled: true }
    sacn: { enabled: true, ttl: 1 }

routes:
  - name: Console to stage
    from: { network: console, protocol: artnet, universes: 0.0.0-0.0.3 }
    to:
      - { network: stage, protocol: artnet, universes: 0.0.0-0.0.3, mode: broadcast }

  - name: Remap to dimmers
    from: { network: console, protocol: artnet, universe: 0.0.4 }
    to:
      - { network: stage, protocol: artnet, universe: 0.1.0, mode: unicast,
          targets: [10.20.30.50, 10.20.30.51] }
      - { network: stage, protocol: sacn, universe: 5, mode: multicast, priority: 100 }
```

### Networks

| key | meaning |
|---|---|
| `cidr` | This box's own IP and prefix on that network, like `2.0.0.10/8`. Or `ip` + `netmask`. |
| `interface` | The NIC this network is on, like `eth1`. The web UI offers a list of this host's interfaces and fills in `cidr` from the one you pick. Each network needs its own interface. If left out, it is found from the IP. |
| `broadcast` | Optional override, for example `255.255.255.255`. |
| `artnet.reply` | `broadcast` (default, what most nodes do) or `unicast` back to whoever polled. |
| `artnet.poll_interval` | How often the bridge sends its own ArtPoll to find nodes. |
| `sacn.ttl` | Multicast TTL for sACN sent on this network. 1 stays in the subnet. |

### Routes

`from` is one universe (`universe: 0.0.1`) or a range (`universes: 0.0.0-0.0.3`).
Each entry in `to` is a destination. With a range in `from`, a destination can
give a matching range or just a start universe.

| `mode` | protocol | what happens |
|---|---|---|
| `broadcast` | Art-Net | Sent to the network's broadcast address. Default. |
| `unicast` | Art-Net, sACN | Sent to every IP in `targets`. |
| `auto` | Art-Net | Unicast to each node that announced this universe in its ArtPollReply, broadcast if none has yet. |
| `multicast` | sACN | Sent to the universe's multicast group. Default for sACN. |

`priority` sets the sACN priority (0-200) for sACN destinations. Frames that
came in as sACN keep their priority unless overridden.

A route whose destination equals its source is refused. A route back onto the
same network with a different universe is fine.

### Options

```yaml
web: { enabled: true, bind: 0.0.0.0, port: 8080 }
log_level: info
forward_sync: true        # pass ArtSync through
forward_other: false      # pass ArtTimeCode, ArtTrigger, ArtCommand through
node_timeout: 10          # seconds without ArtPollReply before a node is dropped
sacn_source_name: ArtBridge
```

## Web UI

- **Status**: each network with live in/out rates, every route with its
  current rate and last source IP, and a "seen but not routed" list for DMX
  that arrives with no matching route (with an "Add route" button).
- **Nodes**: everything that answered an ArtPoll, with the universes it
  listens for.
- **Config**: a form for networks, routes and options, or the raw YAML.
  "Save & apply" validates first and tells you exactly which key is wrong.
- **Log**: the last few hundred log lines.

There is no login. Keep port 8080 inside your management or lighting
networks, or set `web.bind` to one address.

## Testing without gear

The package includes a fake console and a fake node.

```bash
# on a laptop in the stage network: pretend to be a node with 4 universes
artbridge listen --universes 0.0.0-0.0.3 --name FakeNode

# on a laptop in the console network: send 4 universes at 30 fps
artbridge send --to 2.255.255.255 --universe 0.0.0-0.0.3 --fps 30
```

`listen` answers ArtPoll (so it shows up in the bridge's Nodes tab and the
MA's node list) and prints a live table of universes, fps, lost packets and
the first eight channels. `send --sacn` and `listen --sacn` do the same for
sACN. Run `artbridge send -h` / `artbridge listen -h` for all options.

## Lots of universes

A route takes up to 512 universes (`universes: 0.0.0-1.1.15` is 288,
`0.0.0-1.7.15` is 384), and you can have as many routes as you like. The
Status tab shows a range as one route with an "N/M live" count and lists the
universes in it that have gone quiet.

Rough numbers: 384 universes at 44 fps is about 17,000 packets a second in
and the same out. In testing, 288 universes used a bit under half of one CPU
core, most of it in the kernel sending packets. Give the LXC 2 cores for a
rig that size, more if each universe goes to several destinations.

The part that matters most is the UDP receive buffer. A console sends all of
its universes in one burst, and the Linux default buffer holds only about a
hundred packets, so the rest get dropped. ArtBridge asks for 8 MB, but the
kernel caps that at `net.core.rmem_max`. In an unprivileged LXC that limit is
set on the Proxmox host. `proxmox-create-lxc.sh` does this for you; on an
existing setup run this on the Proxmox host, then restart ArtBridge:

```bash
printf 'net.core.rmem_max = 16777216\nnet.core.wmem_max = 16777216\n' > /etc/sysctl.d/90-artbridge.conf
sysctl -p /etc/sysctl.d/90-artbridge.conf
pct exec <CTID> -- systemctl restart artbridge
```

The Status tab warns when the buffer is small and when the kernel has
dropped packets because it was full.

## How it works

- One UDP socket per protocol (6454 for Art-Net, 5568 for sACN) bound to all
  addresses. Linux `IP_PKTINFO` tells us which local address and interface
  every packet arrived on, and lets us pick the source address when sending.
  No raw sockets, no capabilities, runs as an unprivileged user.
- Packets whose source IP is one of the bridge's own addresses are dropped
  first thing. Linux delivers our own broadcasts back to us, and this is what
  keeps the bridge from echoing itself.
- Forwarded ArtDmx gets a fresh sequence number per destination universe, so
  receivers see one clean stream from the bridge.
- The ArtPollReply lists the universes of `from` routes as output ports (the
  console may send them to us) and the universes of `to` routes as input
  ports. Groups of four ports that share Net and Sub-Net go in one reply;
  more ports use `BindIndex` 2, 3, …, which Art-Net 4 controllers merge into
  one node.
- ArtPoll and ArtPollReply are never forwarded across networks. The bridge
  represents the far side to the console.
- The bridge polls each network every `poll_interval` seconds. Nodes that
  stop answering are dropped after `node_timeout` seconds.

## Troubleshooting

| symptom | check |
|---|---|
| Network card shows **no interface** | The IP in `cidr` is not on this box. `artbridge interfaces` lists what is. In an LXC, check the container's network settings in Proxmox. |
| Route stays at 0/s | Status tab → "seen but not routed" shows what is actually arriving and from where. Universe numbering (0 or 1 based) is the usual culprit. |
| MA does not list the bridge | The MA and the bridge must be in the same subnet. Press **Poll now** in the Status tab; the bridge re-announces. Check the MA's Art-Net interface setting. |
| Nodes do not get data in `auto` mode | They must answer ArtPoll. Nodes tab shows what answered; if empty, use `broadcast` or `unicast`. |
| Status says the kernel dropped UDP packets, or some universes flicker | Socket buffer too small for the burst. See "Lots of universes". |
| Send errors on a route | The Status tab shows a red counter. The log says why (usually the interface went down). |
| Something odd on the wire | `log_level: debug`, then `journalctl -u artbridge -f`. |

## Not in this version

- HTP/LTP merging of two sources into one output universe. One source per
  universe; a second source is forwarded as is.
- RDM (ArtTodRequest / ArtRdm) is not forwarded.
- IPv6 (Art-Net is IPv4 only anyway).

## Development

```bash
pip install pyyaml aiohttp pytest ruff
pytest -q
ruff check artbridge tests
python -m artbridge run -c artbridge/example-config.yaml --web-port 8080
```

Layout: `artbridge/artnet.py` and `sacn.py` are the packet codecs,
`transport.py` the UDP socket, `engine.py` the router and ArtPoll logic,
`config.py` the YAML schema, `web/` the aiohttp API and single page UI,
`testtools.py` the `send` / `listen` commands, `deploy/` the installer,
service unit and Proxmox script.

## Credits

Art-Net™ Designed by and Copyright Artistic Licence Holdings Ltd.
sACN is ANSI E1.31. MIT licensed, see [LICENSE](LICENSE).
