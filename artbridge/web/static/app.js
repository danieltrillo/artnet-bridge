/* ArtBridge web UI. Plain JS, no build step. */
(function () {
  "use strict";

  const $ = (sel, el) => (el || document).querySelector(sel);
  const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clone = (o) => JSON.parse(JSON.stringify(o));

  const state = {
    tab: "status",
    status: null,
    raw: null,
    draft: null,
    yamlText: "",
    yamlMode: false,
    addresses: [],
    interfaces: [],
    udpBase: null,
    configLoaded: false,
    lastLogCount: 0,
  };

  // ------------------------------------------------------------------ api

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    let body = null;
    try { body = await res.json(); } catch (e) { body = { ok: false, error: "bad response from server" }; }
    if (!res.ok && body && body.error === undefined) body.error = res.statusText;
    return body;
  }

  // ------------------------------------------------------------------ tabs

  function showTab(name) {
    state.tab = name;
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
    if (name === "config" && !state.configLoaded) loadConfig();
    if (name === "log") refreshLog();
    if (name === "status" || name === "nodes") refreshStatus();
    try { localStorage.setItem("artbridge.tab", name); } catch (e) { /* ignore */ }
  }

  $("#tabs").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-tab]");
    if (btn) showTab(btn.dataset.tab);
  });

  // ------------------------------------------------------------------ status

  function pps(counter) {
    if (!counter) return '<span class="idle">–</span>';
    const live = counter.age != null && counter.age < 3 && counter.pps > 0;
    return `<span class="${live ? "live" : "idle"}">${counter.pps.toFixed(1)}/s</span>`;
  }

  function fmtAge(age) {
    if (age == null) return "never";
    if (age < 60) return `${Math.round(age)}s ago`;
    if (age < 3600) return `${Math.round(age / 60)}m ago`;
    return `${Math.round(age / 3600)}h ago`;
  }

  function fmtUptime(s) {
    if (!s) return "";
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
  }

  function protoName(p) { return p === "sacn" ? "sACN" : "Art-Net"; }

  function renderStatus() {
    const st = state.status;
    if (!st) return;
    $("#host").textContent = st.hostname ? `on ${st.hostname}` : "";
    $("#version").textContent = `ArtBridge ${st.version || ""}${st.uptime ? " · up " + fmtUptime(st.uptime) : ""}`;

    const health = $("#health");
    health.className = "health " + (st.config_error ? "bad" : st.running ? "good" : "warn");
    $("#health-text").textContent = st.config_error ? "config error" : st.running ? "running" : "stopped";

    const banner = $("#banner");
    if (st.config_error) {
      banner.textContent = "Config problem: " + st.config_error + " — fix it in the Config tab.";
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }

    // networks
    $("#networks").innerHTML = (st.networks || []).map((n) => {
      const present = n.present
        ? `<span class="pill good">${esc(n.interface)}</span>`
        : `<span class="pill bad" title="This IP is not on any interface of this host">no interface</span>`;
      const protos = [];
      if (n.artnet.enabled) protos.push(`<dt>Art-Net</dt><dd>in ${pps(n.artnet.rx)} · out ${pps(n.artnet.tx)}</dd>`);
      if (n.sacn.enabled) protos.push(`<dt>sACN</dt><dd>in ${pps(n.sacn.rx)} · out ${pps(n.sacn.tx)}</dd>`);
      const polls = n.artnet.enabled
        ? `<dt>Polls</dt><dd>${n.artnet.polls_rx.total} received, ${n.artnet.replies_tx.total} answered, ${n.artnet.replies_rx.total} node replies</dd>`
        : "";
      const accepts = n.artnet.accepts || [];
      const sends = n.artnet.sends || [];
      const ports = (n.artnet.enabled && (accepts.length || sends.length)
        ? `<dt>Node name</dt><dd>${esc(n.artnet.short_name)}</dd>` : "")
        + (accepts.length ? `<dt>Accepts</dt><dd title="Universes a console on this network can send to this bridge">${accepts.map((u) => `<span class="pill accent">${esc(u)}</span>`).join(" ")}</dd>` : "")
        + (sends.length ? `<dt>Sends</dt><dd title="Universes this bridge puts onto this network">${sends.map((u) => `<span class="pill">${esc(u)}</span>`).join(" ")}</dd>` : "");
      const live = n.live_universes.length ? `<dt>Live now</dt><dd class="live">${n.live_universes.map(esc).join(", ")}</dd>` : "";
      const listening = n.sacn.enabled && n.sacn.listening.length ? `<dt>sACN groups</dt><dd>${n.sacn.listening.join(", ")}</dd>` : "";
      return `<div class="card">
        <h4>${esc(n.name)} ${present}</h4>
        <dl class="kv">
          <dt>Address</dt><dd class="mono">${esc(n.cidr)} <span class="muted">bcast ${esc(n.broadcast)}</span></dd>
          ${protos.join("")}${polls}${ports}${live}${listening}
        </dl>
      </div>`;
    }).join("") || '<p class="muted">No networks configured.</p>';

    // socket buffers and kernel drops
    const notes = [];
    const art = st.transports && st.transports.artnet;
    if (art && art.rcvbuf && art.rcvbuf < 1024 * 1024) {
      notes.push(`<span class="warn-text">Art-Net receive buffer is only ${Math.round(art.rcvbuf / 1024)} KB, big universe counts can drop packets. See README, "Lots of universes".</span>`);
    }
    if (st.udp) {
      if (state.udpBase == null) state.udpBase = st.udp.rcvbuf_errors;
      const lost = st.udp.rcvbuf_errors - state.udpBase;
      if (lost > 0) notes.push(`<span class="warn-text">Kernel dropped ${lost} UDP packets since this page opened (receive buffer full).</span>`);
    }
    $("#perf").innerHTML = notes.join("<br>");
    $("#perf").classList.toggle("hidden", !notes.length);

    // routes
    $("#routes").innerHTML = (st.routes || []).map((r) => {
      const src = r.from;
      const from = `<div class="endpoint">
          <div class="big">${esc(src.universe)} <span class="muted">${protoName(src.protocol)}</span></div>
          <div class="sub">on ${esc(src.network)} · ${pps(r.rx)}${r.last_source ? " · from " + esc(r.last_source) : ""}</div>
        </div>`;
      const outs = r.outputs.map((o) => {
        const where = o.mode === "broadcast" ? `broadcast` : o.mode === "multicast" ? `multicast` : o.mode === "auto" ? `auto → ${o.targets.length ? o.targets.map(esc).join(", ") : "broadcast (no nodes yet)"}` : `unicast → ${o.targets.map(esc).join(", ")}`;
        const err = o.errors ? ` · <span class="pill bad">${o.errors} send errors</span>` : "";
        const prio = o.priority != null ? ` · prio ${o.priority}` : "";
        return `<div class="endpoint">
          <div class="big">${esc(o.universe)} <span class="muted">${protoName(o.protocol)}</span></div>
          <div class="sub">on ${esc(o.network)} · ${where}${prio} · ${pps(o.tx)}${err}</div>
        </div>`;
      }).join("");
      const count = r.count > 1
        ? ` <span class="pill ${r.live === r.count ? "good" : r.live ? "warn" : ""}" title="Universes in this route that are receiving DMX">${r.live}/${r.count} live</span>`
        : "";
      const missing = r.missing && r.missing.length
        ? `<div class="sub warn-text" title="Part of this range is flowing, these universes are not">No DMX on ${r.missing.map(esc).join(", ")}</div>`
        : "";
      return `<div class="route ${r.enabled ? "" : "off"}">
        <div class="head"><span class="name">${esc(r.name)}</span>${count}${r.enabled ? "" : '<span class="pill">disabled</span>'}</div>
        ${missing}
        <div class="flow"><div>${from}</div><div class="arrow">→</div><div>${outs}</div></div>
      </div>`;
    }).join("") || '<p class="muted">No routes yet. Add one in the Config tab.</p>';

    // unrouted
    const un = st.unrouted || [];
    $("#unrouted-wrap").classList.toggle("hidden", un.length === 0);
    $("#unrouted").innerHTML = un.length ? `<table><thead><tr><th>Network</th><th>Protocol</th><th>Universe</th><th>From</th><th class="num">Rate</th><th></th></tr></thead><tbody>` +
      un.map((u, i) => `<tr><td>${esc(u.network)}</td><td>${protoName(u.protocol)}</td><td class="mono">${esc(u.universe)}</td><td class="mono">${esc(u.source)}</td><td class="num">${pps(u.rx)}</td><td><button class="btn small" data-add-route="${i}">Add route</button></td></tr>`).join("") +
      `</tbody></table>` : "";

    // nodes
    const nodes = st.nodes || [];
    $("#nodes-count").textContent = nodes.length ? `${nodes.length} found` : "none found yet";
    $("#nodes").innerHTML = nodes.length ? `<table><thead><tr><th>Network</th><th>IP</th><th>Name</th><th>Outputs</th><th>Inputs</th><th>Type</th><th>Seen</th></tr></thead><tbody>` +
      nodes.map((n) => `<tr>
        <td>${esc(n.network)}</td>
        <td class="mono">${esc(n.ip)}${n.bind_index > 1 ? ` <span class="muted">#${n.bind_index}</span>` : ""}</td>
        <td>${esc(n.short_name)}<div class="muted" style="font-size:.8rem">${esc(n.long_name)}${n.mac && n.mac !== "00:00:00:00:00:00" ? " · " + esc(n.mac) : ""}</div></td>
        <td class="mono">${n.outputs.map(esc).join(", ") || "–"}</td>
        <td class="mono">${n.inputs.map(esc).join(", ") || "–"}</td>
        <td>${esc(n.style)}</td>
        <td>${fmtAge(n.age)}</td>
      </tr>`).join("") + `</tbody></table>` : '<p class="muted">Nothing has answered an ArtPoll yet. Devices normally show up within a few seconds.</p>';
  }

  $("#unrouted").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-add-route]");
    if (!btn) return;
    const u = state.status.unrouted[Number(btn.dataset.addRoute)];
    addRouteFromUnrouted(u);
  });

  async function refreshStatus() {
    try {
      const st = await api("/api/status");
      state.status = st;
      renderStatus();
    } catch (e) {
      $("#health").className = "health bad";
      $("#health-text").textContent = "unreachable";
    }
  }

  // ------------------------------------------------------------------ log

  async function refreshLog() {
    try {
      const data = await api("/api/log");
      const pre = $("#log");
      pre.innerHTML = data.lines.map((l) => `<span class="${l.level}"><span class="t">${esc(l.time)}</span> ${esc(l.message)}</span>`).join("\n");
      if ($("#log-follow").checked) pre.scrollTop = pre.scrollHeight;
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------ config editor

  function netmaskToPrefix(mask) {
    const parts = String(mask).split(".").map(Number);
    if (parts.length !== 4 || parts.some((p) => isNaN(p))) return null;
    const bits = parts.map((p) => p.toString(2).padStart(8, "0")).join("");
    const ones = bits.indexOf("0") === -1 ? 32 : bits.indexOf("0");
    return bits.slice(ones).includes("1") ? null : ones;
  }

  function normaliseDraft(raw) {
    const d = { networks: [], routes: [], web: {}, options: {} };
    const nets = raw && raw.networks && typeof raw.networks === "object" ? raw.networks : {};
    for (const name of Object.keys(nets)) {
      const n = nets[name] || {};
      let cidr = n.cidr || "";
      if (!cidr && n.ip) {
        const prefix = n.prefix != null ? n.prefix : n.netmask ? netmaskToPrefix(n.netmask) : null;
        cidr = prefix != null ? `${n.ip}/${prefix}` : String(n.ip);
      }
      const a = n.artnet || {};
      const s = n.sacn || {};
      d.networks.push({
        name,
        cidr,
        interface: n.interface || "",
        broadcast: n.broadcast || "",
        artnet: {
          enabled: a.enabled == null ? true : !!a.enabled,
          reply: a.reply || "broadcast",
          poll_interval: a.poll_interval == null ? 3 : a.poll_interval,
          short_name: a.short_name == null ? "ArtBridge" : a.short_name,
          long_name: a.long_name || "",
        },
        sacn: { enabled: !!s.enabled, ttl: s.ttl == null ? 1 : s.ttl },
      });
    }
    const routes = Array.isArray(raw && raw.routes) ? raw.routes : [];
    for (const r of routes) {
      const from = (r && r.from) || {};
      let to = (r && r.to) || [];
      if (!Array.isArray(to)) to = [to];
      d.routes.push({
        name: (r && r.name) || "",
        enabled: !r || r.enabled == null ? true : !!r.enabled,
        from: { network: from.network || "", protocol: from.protocol || "artnet", universe: String(from.universes != null ? from.universes : from.universe != null ? from.universe : "") },
        to: to.map((o) => ({
          network: (o && o.network) || "",
          protocol: (o && o.protocol) || from.protocol || "artnet",
          universe: String(o && (o.universes != null ? o.universes : o.universe != null ? o.universe : "")),
          mode: (o && o.mode) || "",
          targets: Array.isArray(o && o.targets) ? o.targets.join(", ") : (o && o.targets) || "",
          priority: o && o.priority != null ? String(o.priority) : "",
        })),
      });
    }
    const w = (raw && raw.web) || {};
    d.web = { enabled: w.enabled == null ? true : !!w.enabled, bind: w.bind || "0.0.0.0", port: w.port == null ? 8080 : w.port };
    d.options = {
      log_level: (raw && raw.log_level) || "info",
      forward_sync: !raw || raw.forward_sync == null ? true : !!raw.forward_sync,
      forward_other: !!(raw && raw.forward_other),
      node_timeout: raw && raw.node_timeout != null ? raw.node_timeout : 10,
      sacn_source_name: (raw && raw.sacn_source_name) || "ArtBridge",
      sacn_cid: (raw && raw.sacn_cid) || "",
    };
    return d;
  }

  function draftToConfig(d) {
    const networks = {};
    for (const n of d.networks) {
      const entry = { cidr: (n.cidr || "").trim() };
      if ((n.interface || "").trim()) entry.interface = n.interface.trim();
      if ((n.broadcast || "").trim()) entry.broadcast = n.broadcast.trim();
      entry.artnet = {
        enabled: !!n.artnet.enabled,
        reply: n.artnet.reply,
        poll_interval: Number(n.artnet.poll_interval),
        short_name: n.artnet.short_name,
      };
      if ((n.artnet.long_name || "").trim()) entry.artnet.long_name = n.artnet.long_name.trim();
      entry.sacn = { enabled: !!n.sacn.enabled, ttl: Number(n.sacn.ttl) };
      networks[(n.name || "").trim()] = entry;
    }
    const routes = d.routes.map((r) => {
      const route = {
        name: r.name,
        enabled: !!r.enabled,
        from: { network: r.from.network, protocol: r.from.protocol, universe: (r.from.universe || "").trim() },
        to: r.to.map((o) => {
          const out = { network: o.network, protocol: o.protocol, universe: (o.universe || "").trim() };
          if (o.mode) out.mode = o.mode;
          const targets = (o.targets || "").split(/[,\s;]+/).filter(Boolean);
          if (targets.length) out.targets = targets;
          if (String(o.priority || "").trim() !== "") out.priority = Number(o.priority);
          return out;
        }),
      };
      return route;
    });
    const cfg = {
      networks,
      routes,
      web: { enabled: !!d.web.enabled, bind: d.web.bind, port: Number(d.web.port) },
      log_level: d.options.log_level,
      forward_sync: !!d.options.forward_sync,
      forward_other: !!d.options.forward_other,
      node_timeout: Number(d.options.node_timeout),
      sacn_source_name: d.options.sacn_source_name,
    };
    if ((d.options.sacn_cid || "").trim()) cfg.sacn_cid = d.options.sacn_cid.trim();
    return cfg;
  }

  // small DOM helpers ---------------------------------------------------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const k of Object.keys(attrs || {})) {
      if (k === "class") node.className = attrs[k];
      else if (k === "text") node.textContent = attrs[k];
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), attrs[k]);
      else if (k in node && typeof attrs[k] === "boolean") node[k] = attrs[k];
      else node.setAttribute(k, attrs[k]);
    }
    for (const c of children || []) node.append(c);
    return node;
  }

  function field(label, input, extraClass) {
    return el("label", { class: "field " + (extraClass || "") }, [el("span", { text: label }), input]);
  }

  function textInput(obj, key, opts) {
    opts = opts || {};
    const input = el("input", { type: opts.type || "text", value: obj[key] == null ? "" : obj[key], placeholder: opts.placeholder || "", class: opts.mono ? "mono" : "" });
    if (opts.min != null) input.min = opts.min;
    if (opts.max != null) input.max = opts.max;
    if (opts.step != null) input.step = opts.step;
    input.addEventListener("input", () => { obj[key] = input.value; });
    return input;
  }

  function select(obj, key, options, onchange) {
    const sel = el("select");
    for (const [value, label] of options) sel.append(el("option", { value, text: label }));
    sel.value = obj[key];
    if (sel.value !== obj[key]) { sel.value = options[0][0]; obj[key] = sel.value; }
    sel.addEventListener("change", () => { obj[key] = sel.value; if (onchange) onchange(); });
    return sel;
  }

  function checkbox(obj, key, label) {
    const input = el("input", { type: "checkbox", checked: !!obj[key] });
    input.addEventListener("change", () => { obj[key] = input.checked; });
    return el("label", { class: "check" }, [input, el("span", { text: label })]);
  }

  function networkOptions() {
    const names = state.draft.networks.map((n) => n.name).filter(Boolean);
    return names.length ? names.map((n) => [n, n]) : [["", "(no networks)"]];
  }

  const PROTOCOLS = [["artnet", "Art-Net"], ["sacn", "sACN"]];
  const MODES = {
    artnet: [["broadcast", "broadcast"], ["unicast", "unicast to targets"], ["auto", "auto (unicast to found nodes)"]],
    sacn: [["multicast", "multicast"], ["unicast", "unicast to targets"]],
  };

  // interfaces -----------------------------------------------------------

  function hostInterface(name) {
    return state.interfaces.find((i) => i.name === name) || null;
  }

  // Which interface a network is on: the one picked, or the one holding its IP.
  function effectiveInterface(n) {
    if ((n.interface || "").trim()) return n.interface.trim();
    const ip = String(n.cidr || "").split("/")[0].trim();
    const hit = state.addresses.find((a) => a.ip === ip);
    return hit ? hit.interface : "";
  }

  function interfaceOwner(name, exceptIndex) {
    return state.draft.networks.find((n, i) => i !== exceptIndex && name && effectiveInterface(n) === name) || null;
  }

  function duplicateInterfaces(d) {
    const seen = {};
    const dupes = new Set();
    for (const n of d.networks) {
      const name = effectiveInterface(n);
      if (!name) continue;
      if (seen[name]) dupes.add(name);
      seen[name] = true;
    }
    return dupes;
  }

  function interfaceLabel(i) {
    const addrs = i.addresses.map((a) => a.cidr).join(", ") || "no IPv4 address";
    return `${i.name} · ${addrs}${i.up ? "" : " (down)"}`;
  }

  function interfaceSelect(n, index, cidrInput) {
    const sel = el("select");
    sel.append(el("option", { value: "", text: state.interfaces.length ? "Pick an interface…" : "No interfaces found" }));
    for (const i of state.interfaces) {
      const owner = interfaceOwner(i.name, index);
      const opt = el("option", { value: i.name, text: interfaceLabel(i) + (owner ? ` · used by ${owner.name || "another network"}` : "") });
      if (owner) opt.disabled = true;
      sel.append(opt);
    }
    const current = (n.interface || "").trim();
    if (current && !hostInterface(current)) {
      // Configured, but this host does not have it (renamed, or a typo).
      sel.append(el("option", { value: current, text: `${current} · not on this host` }));
    }
    sel.value = current;
    sel.addEventListener("change", () => {
      n.interface = sel.value;
      const iface = hostInterface(sel.value);
      if (iface && iface.addresses.length) {
        // Fill in this box's address on that interface; keep a manual pick if it is one of them.
        const keep = iface.addresses.find((a) => a.cidr === n.cidr);
        n.cidr = (keep || iface.addresses[0]).cidr;
        cidrInput.value = n.cidr;
        n.broadcast = "";
      }
      if (iface && !n.name) n.name = iface.name;
      renderEditor();
    });
    return sel;
  }

  function interfaceHint(n, dupes) {
    const name = (n.interface || "").trim();
    const eff = effectiveInterface(n);
    if (eff && dupes.has(eff)) return { level: "bad", text: `${eff} is used by more than one network. Each network needs its own interface.` };
    if (!name) {
      return eff
        ? { level: "warn", text: `No interface picked; this IP is on ${eff}. Pick it above to lock it in.` }
        : { level: "warn", text: "Pick the interface this network is on." };
    }
    const iface = hostInterface(name);
    if (!iface) return { level: "bad", text: `${name} is not on this host. Pick one from the list.` };
    const cidr = String(n.cidr || "").trim();
    if (!iface.addresses.length) return { level: "warn", text: `${name} has no IPv4 address yet; type the address you will give it.` };
    if (!iface.addresses.some((a) => a.cidr === cidr)) {
      const ip = cidr.split("/")[0];
      const fix = (iface.addresses.find((a) => a.ip === ip) || iface.addresses[0]).cidr;
      return { level: "bad", fix, text: `${name} has ${iface.addresses.map((a) => a.cidr).join(", ")}, not ${cidr || "this"}.` };
    }
    return null;
  }

  function renderEditor() {
    const d = state.draft;
    if (!d) return;
    $("#host-addresses").textContent = state.addresses.length ? state.addresses.map((a) => `${a.cidr} (${a.interface})`).join(", ") : "none found";
    const dupes = duplicateInterfaces(d);

    // networks
    const netWrap = $("#ed-networks");
    netWrap.innerHTML = "";
    d.networks.forEach((n, i) => {
      const card = el("div", { class: "card" });
      const cidrInput = textInput(n, "cidr", { placeholder: "2.0.0.10/8", mono: true });
      const hint = el("div", { class: "hint" });
      const showHint = () => {
        const msg = interfaceHint(n, dupes);
        hint.textContent = msg ? msg.text + " " : "";
        hint.className = "hint " + (msg ? msg.level : "");
        if (msg && msg.fix) {
          hint.append(el("button", { class: "btn small", text: `Use ${msg.fix}`, onclick: () => { n.cidr = msg.fix; n.broadcast = ""; renderEditor(); } }));
        }
      };
      cidrInput.addEventListener("input", showHint);
      card.append(el("div", { class: "grid c2" }, [
        field("Name", textInput(n, "name", { placeholder: "console" })),
        field("Interface", interfaceSelect(n, i, cidrInput)),
        field("This box's IP / prefix", cidrInput),
        field("Broadcast (optional)", textInput(n, "broadcast", { placeholder: "auto", mono: true })),
      ]));
      card.append(hint);
      showHint();
      card.append(el("div", { class: "section", text: "Art-Net" }));
      card.append(el("div", { class: "grid c3" }, [
        checkbox(n.artnet, "enabled", "Enabled"),
        field("ArtPollReply", select(n.artnet, "reply", [["broadcast", "broadcast"], ["unicast", "unicast to poller"]])),
        field("Poll every (s)", textInput(n.artnet, "poll_interval", { type: "number", min: 0, step: 0.5 })),
        field("Short name", textInput(n.artnet, "short_name", { placeholder: "ArtBridge" })),
        field("Long name", textInput(n.artnet, "long_name", { placeholder: "optional" })),
      ]));
      card.append(el("div", { class: "section", text: "sACN" }));
      card.append(el("div", { class: "grid c3" }, [
        checkbox(n.sacn, "enabled", "Enabled"),
        field("Multicast TTL", textInput(n.sacn, "ttl", { type: "number", min: 1, max: 255 })),
      ]));
      card.append(el("div", { class: "tools" }, [
        el("button", { class: "btn small danger", text: "Remove network", onclick: () => { d.networks.splice(i, 1); renderEditor(); } }),
      ]));
      netWrap.append(card);
    });
    if (!d.networks.length) netWrap.append(el("p", { class: "muted", text: "No networks. Add one." }));

    // routes
    const routeWrap = $("#ed-routes");
    routeWrap.innerHTML = "";
    d.routes.forEach((r, i) => {
      const box = el("div", { class: "ed-route" });
      box.append(el("div", { class: "head" }, [
        field("Route name", textInput(r, "name", { placeholder: "Console to stage" })),
        checkbox(r, "enabled", "Enabled"),
        el("button", { class: "btn small danger", text: "Remove", onclick: () => { d.routes.splice(i, 1); renderEditor(); } }),
      ]));
      box.append(el("div", { class: "section", text: "From" }));
      box.append(el("div", { class: "grid c3" }, [
        field("Network", select(r.from, "network", networkOptions())),
        field("Protocol", select(r.from, "protocol", PROTOCOLS)),
        field("Universe", textInput(r.from, "universe", { placeholder: "0.0.0 or 0.0.0-0.0.3", mono: true })),
      ]));
      box.append(el("div", { class: "section", text: "To" }));
      r.to.forEach((o, j) => {
        const out = el("div", { class: "ed-out" });
        const grid = el("div", { class: "grid c4" });
        const build = () => {
          grid.innerHTML = "";
          const modes = MODES[o.protocol] || MODES.artnet;
          if (!modes.some((m) => m[0] === o.mode)) o.mode = modes[0][0];
          grid.append(
            field("Network", select(o, "network", networkOptions())),
            field("Protocol", select(o, "protocol", PROTOCOLS, build)),
            field("Universe", textInput(o, "universe", { placeholder: "same, or 0.1.0", mono: true })),
            field("Mode", select(o, "mode", modes, build)),
          );
          if (o.mode === "unicast") grid.append(field("Target IPs (comma separated)", textInput(o, "targets", { placeholder: "10.20.30.50, 10.20.30.51", mono: true })));
          if (o.protocol === "sacn") grid.append(field("Priority (0-200)", textInput(o, "priority", { type: "number", min: 0, max: 200, placeholder: "100" })));
        };
        build();
        out.append(grid, el("button", { class: "btn small danger", text: "×", title: "Remove destination", onclick: () => { r.to.splice(j, 1); renderEditor(); } }));
        box.append(out);
      });
      box.append(el("button", { class: "btn small", text: "+ destination", onclick: () => { r.to.push(newOutput(r)); renderEditor(); } }));
      routeWrap.append(box);
    });
    if (!d.routes.length) routeWrap.append(el("p", { class: "muted", text: "No routes. Nothing will be forwarded until you add one." }));

    // options
    const opt = $("#ed-options");
    opt.innerHTML = "";
    opt.append(el("div", { class: "grid c3" }, [
      field("Log level", select(d.options, "log_level", [["debug", "debug"], ["info", "info"], ["warning", "warning"], ["error", "error"]])),
      field("Node timeout (s)", textInput(d.options, "node_timeout", { type: "number", min: 1 })),
      field("sACN source name", textInput(d.options, "sacn_source_name")),
      checkbox(d.options, "forward_sync", "Pass ArtSync through"),
      checkbox(d.options, "forward_other", "Pass ArtTimeCode / ArtTrigger / ArtCommand through"),
      field("sACN CID (optional UUID)", textInput(d.options, "sacn_cid", { placeholder: "auto", mono: true })),
      field("Web UI port", textInput(d.web, "port", { type: "number", min: 1, max: 65535 })),
      field("Web UI bind address", textInput(d.web, "bind", { mono: true })),
      checkbox(d.web, "enabled", "Web UI enabled"),
    ]));
  }

  function newOutput(route) {
    const other = state.draft.networks.find((n) => n.name && n.name !== route.from.network);
    const protocol = route.from.protocol || "artnet";
    return { network: other ? other.name : route.from.network, protocol, universe: route.from.universe, mode: protocol === "sacn" ? "multicast" : "broadcast", targets: "", priority: "" };
  }

  function newRoute() {
    const first = state.draft.networks[0];
    const r = { name: `Route ${state.draft.routes.length + 1}`, enabled: true, from: { network: first ? first.name : "", protocol: "artnet", universe: "0.0.0" }, to: [] };
    r.to.push(newOutput(r));
    return r;
  }

  function addRouteFromUnrouted(u) {
    const go = () => {
      const r = { name: `${u.network} ${u.universe}`, enabled: true, from: { network: u.network, protocol: u.protocol, universe: u.universe }, to: [] };
      r.to.push(newOutput(r));
      state.draft.routes.push(r);
      state.yamlMode = false;
      $("#yaml-mode").checked = false;
      applyMode();
      renderEditor();
      showTab("config");
      window.scrollTo(0, document.body.scrollHeight);
    };
    if (state.configLoaded) go(); else loadConfig().then(go);
  }

  $("#btn-add-network").addEventListener("click", async () => {
    await loadInterfaces();
    const n = { name: "", cidr: "", interface: "", broadcast: "", artnet: { enabled: true, reply: "broadcast", poll_interval: 3, short_name: "ArtBridge", long_name: "" }, sacn: { enabled: false, ttl: 1 } };
    const free = state.interfaces.find((i) => i.addresses.length && !interfaceOwner(i.name, -1));
    if (free) {
      n.interface = free.name;
      n.cidr = free.addresses[0].cidr;
      if (!state.draft.networks.some((x) => x.name === free.name)) n.name = free.name;
    }
    state.draft.networks.push(n);
    renderEditor();
  });

  $("#btn-rescan").addEventListener("click", async () => { await loadInterfaces(); renderEditor(); });

  async function loadInterfaces() {
    const ifs = await api("/api/interfaces");
    state.addresses = (ifs && ifs.addresses) || [];
    state.interfaces = (ifs && ifs.interfaces) || [];
  }
  $("#btn-add-route").addEventListener("click", () => { state.draft.routes.push(newRoute()); renderEditor(); });

  function showConfigMessage(ok, text) {
    $("#config-error").classList.toggle("hidden", ok || !text);
    $("#config-ok").classList.toggle("hidden", !ok || !text);
    (ok ? $("#config-ok") : $("#config-error")).textContent = text || "";
  }

  async function loadConfig() {
    const [cfg] = await Promise.all([api("/api/config"), loadInterfaces()]);
    state.raw = cfg.raw;
    state.yamlText = cfg.yaml || "";
    state.draft = normaliseDraft(cfg.raw || {});
    state.configLoaded = true;
    $("#config-path").textContent = cfg.path ? `File: ${cfg.path}` : "";
    $("#yaml").value = state.yamlText;
    if (cfg.raw == null && cfg.yaml) {
      state.yamlMode = true;
      $("#yaml-mode").checked = true;
    }
    showConfigMessage(false, cfg.error || "");
    applyMode();
    renderEditor();
  }

  function applyMode() {
    $("#editor").classList.toggle("hidden", state.yamlMode);
    $("#yaml-editor").classList.toggle("hidden", !state.yamlMode);
  }

  $("#yaml-mode").addEventListener("change", (ev) => {
    state.yamlMode = ev.target.checked;
    if (state.yamlMode) {
      // keep whatever is in the textarea; if untouched, show the file as is
      if (!$("#yaml").value) $("#yaml").value = state.yamlText;
    }
    applyMode();
  });

  $("#btn-discard").addEventListener("click", () => {
    state.draft = normaliseDraft(state.raw || {});
    $("#yaml").value = state.yamlText;
    showConfigMessage(true, "");
    renderEditor();
  });

  $("#btn-save").addEventListener("click", async () => {
    const btn = $("#btn-save");
    btn.disabled = true;
    try {
      if (!state.yamlMode) {
        const dupes = duplicateInterfaces(state.draft);
        if (dupes.size) {
          showConfigMessage(false, `Interface ${[...dupes].join(", ")} is used by more than one network. Each network needs its own interface.`);
          return;
        }
      }
      const body = state.yamlMode ? { yaml: $("#yaml").value } : { config: draftToConfig(state.draft) };
      const res = await api("/api/config", { method: "PUT", body: JSON.stringify(body) });
      if (!res.ok) {
        showConfigMessage(false, res.error || "could not save");
        return;
      }
      state.raw = res.raw;
      state.yamlText = res.yaml || "";
      state.draft = normaliseDraft(res.raw || {});
      $("#yaml").value = state.yamlText;
      showConfigMessage(true, res.error ? "Saved, but: " + res.error : "Saved and applied.");
      renderEditor();
      refreshStatus();
    } finally {
      btn.disabled = false;
    }
  });

  $("#btn-poll").addEventListener("click", async () => {
    await api("/api/poll", { method: "POST", body: "{}" });
    setTimeout(refreshStatus, 600);
  });

  // ------------------------------------------------------------------ boot

  setInterval(() => {
    if (document.hidden) return;
    if (state.tab === "status" || state.tab === "nodes") refreshStatus();
    else if (state.tab === "log") refreshLog();
  }, 1000);

  let initial = "status";
  try { initial = localStorage.getItem("artbridge.tab") || "status"; } catch (e) { /* ignore */ }
  refreshStatus();
  showTab(initial);
})();
