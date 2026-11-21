/* Ericsson BLE demo GUI - client logic. Pure vanilla JS, no build step,
   no external libraries (matches the project's "no deps to install"
   constraint end to end: this file is static and ships with the project). */
(function () {
  "use strict";

  // Populated from the server's own detection (server.py's all_hci(), which
  // reads /sys/class/bluetooth/hci* on whatever machine it's actually
  // running on) as soon as the first /api/status response arrives -- NOT a
  // fixed [0..7]. Confirmed live as a real bug otherwise: this GUI was
  // built and first tested on an 8-controller unit, and a hardcoded range
  // offered hci4-hci7 as selectable radios on a second physical unit that
  // only had 4, none of which actually existed there.
  let ALL_HCI = [];
  let CAPS = null;
  let STATE = { running: [], finished: [], available_hci: [], all_hci: [] };
  // Small surface for the Classic tab (classic.js): it receives every SSE
  // event with the job's kind/dev already resolved, and re-renders on state.
  const BF = window.BF = {
    hooks: [],
    state: function () { return STATE; },
    refresh: function () { return refreshStatus(); },
  };
  // A radio is one number. The radios of the unit serving this page keep their plain hci number; every other unit of a LANforge
  // cluster owns a block of 100 (resource 1.2 might be 100..131). The server names them ("1.2.hci1"); a lone unit keeps "hci1".
  function devLabel(dev) { const l = (STATE.labels || {})[String(dev)]; return l || "hci" + dev; }
  function unitBlock(dev) { return Math.floor(dev / 100); }
  function unitOfBlock(block) { return (STATE.units || []).find(function (u) { return u.base === block * 100; }) || null; }
  BF.label = devLabel;
  BF.sameUnit = function (a, b) { return unitBlock(a) === unitBlock(b); };
  BF.unitName = function (dev) { const u = unitOfBlock(unitBlock(dev)); return u ? (u.eid || u.host || "this unit") : "this unit"; };
  function fireHooks(evt, kind, dev) {
    BF.hooks.forEach(function (h) { try { h(evt, kind, dev); } catch (e) { console.warn("hook error", e); } });
  }

  // ---------------------------------------------------------------- utils
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function fmtUptime(job) {
    if (job.uptime != null) return job.uptime.toFixed(1) + "s";
    if (job.stopped) return "stopped";
    return "-";
  }
  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) {
      let msg = r.statusText;
      try { const j = await r.json(); msg = j.error || j.msg || msg; } catch (e) { }
      throw new Error(msg);
    }
    return r.json();
  }
  function post(path, body) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }

  // ---------------------------------------------------------------- tabs
  document.querySelectorAll(".lf-tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      document.querySelectorAll(".lf-tab").forEach(function (t) { t.classList.remove("sel"); });
      document.querySelectorAll(".lf-section").forEach(function (s) { s.classList.remove("show"); });
      tab.classList.add("sel");
      $("tab-" + tab.dataset.tab).classList.add("show");
    });
  });

  // ---------------------------------------------------------------- caps -> form population
  function fillSelect(sel, items, labelFn) {
    sel.innerHTML = "";
    items.forEach(function (it) {
      const opt = el("option");
      opt.value = typeof it === "object" ? it.value : it;
      opt.textContent = labelFn ? labelFn(it) : opt.value;
      sel.appendChild(opt);
    });
  }

  function populateDevSelects() {
    ["sn_dev"].forEach(function (id) {
      const sel = $(id);
      const prev = sel.value;
      sel.innerHTML = "";
      ALL_HCI.forEach(function (h) {
        const reserved = (STATE.classic_reserved || []).indexOf(h) !== -1;
        const opt = el("option", null, devLabel(h) + (STATE.available_hci.indexOf(h) === -1 ? (reserved ? "  (Classic)" : "  (busy)") : "  (idle)"));
        opt.value = h;
        if (STATE.available_hci.indexOf(h) === -1) opt.disabled = true;
        sel.appendChild(opt);
      });
      if (prev && [...sel.options].some(function (o) { return o.value === prev; })) sel.value = prev;
      else {
        const firstFree = [...sel.options].find(function (o) { return !o.disabled; });
        if (firstFree) sel.value = firstFree.value;
      }
    });
    populateInterfereChecks();
    populateRadioChecks("adv_dev_checks");
  }

  function populateInterfereChecks() { populateRadioChecks("if_dev_checks"); }

  // One checkbox per radio, shared by the tabs that can run on several radios at once (Interference, Advertise).
  function populateRadioChecks(boxId) {
    const box = $(boxId);
    // preserve which boxes the user already checked across periodic refreshes
    const checked = new Set(
      [...box.querySelectorAll("input[type=checkbox]")].filter(function (c) { return c.checked; })
        .map(function (c) { return c.value; }));
    box.innerHTML = "";
    ALL_HCI.forEach(function (h) {
      const busy = STATE.available_hci.indexOf(h) === -1;
      const label = el("label", busy ? "busy" : null);
      const cb = el("input");
      cb.type = "checkbox";
      cb.value = String(h);
      cb.disabled = busy;
      if (checked.has(String(h))) cb.checked = true;
      label.appendChild(cb);
      const rsv = (STATE.classic_reserved || []).indexOf(h) !== -1;
      label.appendChild(el("span", null, devLabel(h) + (busy ? (rsv ? " (Classic)" : " (busy)") : "")));
      box.appendChild(label);
    });
  }

  function selectedRadios(boxId) {
    return [...$(boxId).querySelectorAll("input[type=checkbox]:checked:not(:disabled)")]
      .map(function (c) { return parseInt(c.value, 10); });
  }

  function selectedInterfereDevs() {
    // :checked:not(:disabled) -- a radio already running a job stays
    // checked-and-disabled across refreshes (by design, see
    // populateInterfereChecks), but plain :checked also matched it, so
    // re-clicking Start resubmitted already-running radios and the 409
    // response surfaced as a false "did not start" alarm for jobs that were
    // actually fine (confirmed by code review).
    return [...$("if_dev_checks").querySelectorAll("input[type=checkbox]:checked:not(:disabled)")]
      .map(function (c) { return parseInt(c.value, 10); });
  }

  async function loadCaps() {
    CAPS = await api("/api/caps");
    if (CAPS.error) { console.warn("caps error", CAPS.error); return; }
    if (CAPS.hardware && Array.isArray(CAPS.hardware.all_hci)) ALL_HCI = CAPS.hardware.all_hci;
    fillSelect($("if_payload"), CAPS.interfere.payloads);
    const profSel = $("adv_profile");
    CAPS.adv.profiles.forEach(function (p) {
      const opt = el("option", null, p);
      opt.value = p;
      profSel.appendChild(opt);
    });
  }

  // ---------------------------------------------------------------- interfere mode -> arg field
  const IF_ARG_LABELS = {
    single: "Channel (0-39)", sweep: "Range a-b (e.g. 0-39)", hop: "Channel list (e.g. 0,12,24,39)",
    wifi: "Wi-Fi channel (1-13)", band: "", "adv-flood": "",
  };
  const IF_ARG_DEFAULTS = { single: "19", sweep: "0-39", hop: "0,12,24,39", wifi: "6" };
  $("if_mode").addEventListener("change", function () {
    const m = $("if_mode").value;
    const field = $("if_arg_field");
    if (m === "band" || m === "adv-flood") { field.classList.add("hidden"); }
    else {
      field.classList.remove("hidden"); $("if_arg_label").textContent = IF_ARG_LABELS[m];
      $("if_arg").value = IF_ARG_DEFAULTS[m];          // the previous mode's value (e.g. 19) is wrong for this one
    }
  });

  // ---------------------------------------------------------------- adv type -> peer field
  $("adv_type").addEventListener("change", function () {
    const directed = $("adv_type").value.indexOf("direct") === 0;
    $("adv_peer_field").style.visibility = directed ? "visible" : "hidden";
    $("adv_payload_row").style.opacity = directed ? "0.4" : "1";
    [...$("adv_payload_row").querySelectorAll("input,select")].forEach(function (i) { i.disabled = directed; });
  });
  $("adv_type").dispatchEvent(new Event("change"));

  // ---------------------------------------------------------------- start/stop actions
  $("if_start").addEventListener("click", async function () {
    const devs = selectedInterfereDevs();
    if (devs.length === 0) { alert("Select at least one radio (hci) to interfere on."); return; }
    const mode = $("if_mode").value;
    const params = {
      mode: mode, arg: $("if_arg").value, bw: $("if_bw").value, len: $("if_len").value,
      payload: $("if_payload").value, phy: $("if_phy").value, dwell: $("if_dwell").value,
      power: $("if_power").value || null, duration: $("if_duration").value,
      map: $("if_map").checked,
    };
    // Start one independent job per selected radio, all running the same
    // plan concurrently (e.g. two radios jamming two different channels, or
    // the same channel from two radios at once). Each POST creates its own
    // job_id/process, so a failure starting one radio doesn't block the rest.
    const results = await Promise.all(devs.map(function (dev) {
      return post("/api/start", { tool: "interfere", dev: dev, params: params })
        .then(function (r) { return { dev: dev, r: r }; })
        .catch(function (e) { return { dev: dev, r: { ok: false, msg: e.message } }; });
    }));
    const failed = results.filter(function (x) { return !x.r.ok; });
    if (failed.length) {
      alert("Some radios did not start:\n" + failed.map(function (x) {
        return devLabel(x.dev) + ": " + x.r.msg;
      }).join("\n"));
    }
    refreshStatus();
  });

  $("adv_start").addEventListener("click", async function () {
    const params = {
      type: $("adv_type").value, interval: $("adv_interval").value,
      peer: $("adv_peer").value || null, name: $("adv_name").value,
      profile: $("adv_profile").value || null, profile_cfg: $("adv_profile_cfg").value || null,
      duration: $("adv_duration").value, power: $("adv_power").value || null,
    };
    const devs = selectedRadios("adv_dev_checks");
    if (devs.length === 0) { alert("Select at least one radio (hci) to advertise from."); return; }
    // One independent job per selected radio, all with the same settings. A failure on one radio does not stop the rest.
    const results = await Promise.all(devs.map(function (dev) {
      return post("/api/start", { tool: "adv", dev: dev, params: params })
        .then(function (r) { return { dev: dev, r: r }; })
        .catch(function (e) { return { dev: dev, r: { ok: false, msg: e.message } }; });
    }));
    const failed = results.filter(function (x) { return !x.r.ok; });
    if (failed.length) {
      alert("Some radios did not start:\n" + failed.map(function (x) { return devLabel(x.dev) + ": " + x.r.msg; }).join("\n"));
    }
    refreshStatus();
  });

  $("sn_start").addEventListener("click", async function () {
    const params = {
      active: $("sn_active").checked, coded: $("sn_coded").checked, pcap: $("sn_pcap").checked,
      filter: $("sn_filter").value || null, duration: $("sn_duration").value,
    };
    try {
      const r = await post("/api/start", { tool: "sniff", dev: $("sn_dev").value, params: params });
      if (!r.ok) throw new Error(r.msg);
    } catch (e) { alert("Could not start: " + e.message); }
  });

  $("stopAllBtn").addEventListener("click", function () {
    // Previously fired-and-forgot: a network failure on this specific panic
    // button gave zero on-screen indication anything was wrong, while
    // interference kept transmitting (confirmed by code review).
    post("/api/stop_all", {}).catch(function (e) { alert("Stop All failed: " + e.message); });
  });
  $("refreshBtn").addEventListener("click", function () { refreshStatus(); loadCaps(); });
  $("helpBtn").addEventListener("click", function () {
    alert("BE200 BLE Lab - Interference (DTM) + Advertisement generator across the BE200 "
      + "Bluetooth controllers of this unit and, on a LANforge manager, of every clustered resource that runs BluForge (named like 1.2.hci0). "
      + "Start a job on an idle radio from the "
      + "Interference/Advertise/Sniff tabs; the Radios tab shows what's running where. "
      + "The Bluetooth Classic tab reserves one radio for BR/EDR work (pair, A2DP, AVRCP) - it stays under BlueZ. "
      + "The Interference tab can also stream a live A2DP link from a Classic radio as realistic Classic interference.");
  });

  function stopJob(jobId) {
    post("/api/stop", { job_id: jobId }).then(function (r) {
      if (!r.ok) alert("Stop failed for " + jobId + ": " + (r.msg || "unknown error"));
    }).catch(function (e) { alert("Stop failed for " + jobId + ": " + e.message); });
  }

  // ---------------------------------------------------------------- rendering
  function renderRadios() {
    const tbody = document.querySelector("#radiosTable tbody");
    tbody.innerHTML = "";
    const byDev = {};
    STATE.running.forEach(function (j) { byDev[j.dev] = j; });
    let lastBlock = null;
    ALL_HCI.forEach(function (h) {
      if ((STATE.units || []).length > 1 && unitBlock(h) !== lastBlock) {
        lastBlock = unitBlock(h);
        const u = unitOfBlock(lastBlock);
        const head = el("tr", "unitrow");
        const td = el("td", null, u ? (u.eid ? u.eid + "  -  " : "") + (u.host || "") + (u.ip ? "  (" + u.ip + ")" : "") : "");
        td.colSpan = 6;
        head.appendChild(td);
        tbody.appendChild(head);
      }
      const j = byDev[h];
      const tr = el("tr", j ? "running" : "idle");
      tr.appendChild(el("td", null, devLabel(h)));
      const stPill = el("td");
      const rsv = (STATE.classic_reserved || []).indexOf(h) !== -1;
      const pill = el("span", "pill " + (j ? "busy" : rsv ? "off" : "ok"), j ? "RUNNING" : rsv ? "CLASSIC (reserved)" : "idle");
      stPill.appendChild(pill);
      tr.appendChild(stPill);
      tr.appendChild(el("td", null, j ? j.id : "-"));
      tr.appendChild(el("td", null, j ? j.kind : "-"));
      tr.appendChild(el("td", "num", j ? fmtUptime(j) : "-"));
      const act = el("td");
      if (j) {
        const btn = el("button", "mini-btn", "Stop");
        btn.addEventListener("click", function () { stopJob(j.id); });
        act.appendChild(btn);
      }
      tr.appendChild(act);
      tbody.appendChild(tr);
    });

    const htbody = document.querySelector("#historyTable tbody");
    htbody.innerHTML = "";
    STATE.finished.slice().reverse().forEach(function (j) {
      const tr = el("tr", j.exit_code === 0 ? "idle" : "err");
      tr.appendChild(el("td", null, j.id));
      tr.appendChild(el("td", null, devLabel(j.dev)));
      tr.appendChild(el("td", null, j.kind));
      tr.appendChild(el("td", null, j.started ? new Date(j.started * 1000).toLocaleTimeString() : "-"));
      tr.appendChild(el("td", "num", j.exit_code == null ? "-" : String(j.exit_code)));
      htbody.appendChild(tr);
    });
  }

  function renderKindTable(tableId, kind, cols) {
    const tbody = document.querySelector("#" + tableId + " tbody");
    tbody.innerHTML = "";
    STATE.running.filter(function (j) { return j.kind === kind; }).forEach(function (j) {
      const tr = el("tr", "running");
      cols.forEach(function (c) { tr.appendChild(el("td", c.num ? "num" : null, c.fn(j))); });
      const act = el("td");
      const btn = el("button", "mini-btn", "Stop");
      btn.addEventListener("click", function () { stopJob(j.id); });
      act.appendChild(btn);
      tr.appendChild(act);
      tbody.appendChild(tr);
    });
  }

  // ---- units (LANforge cluster): the manager's own radios plus those of every resource that runs BluForge
  function renderUnits() {
    const units = STATE.units || [], others = STATE.other_resources || [];
    const wrap = $("unitsWrap");
    const show = units.length > 1 || others.length > 0;
    wrap.style.display = show ? "" : "none";
    $("radiosCap").textContent = units.length > 1
      ? "Bluetooth controllers of every unit in the cluster (named resource.hci)"
      : "Bluetooth controllers on this unit";
    if (!show) return;
    const tbody = document.querySelector("#unitsTable tbody");
    tbody.innerHTML = "";
    function row(cells, cls) {
      const tr = el("tr", cls);
      cells.forEach(function (c) {
        const td = el("td");
        if (c && c.pill) td.appendChild(el("span", "pill " + c.pill, c.text)); else td.textContent = c == null ? "" : String(c);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
    units.forEach(function (u) {
      const st = u.local ? { pill: "ok", text: "this unit" } : u.state === "online" ? { pill: "ok", text: "online" } : { pill: "busy", text: "not answering" };
      const note = u.local ? "" : u.state === "online" ? (u.version ? "BluForge " + u.version : "")
        : "Its radios are hidden until it answers again" + (u.error ? " (" + u.error + ")" : "");
      row([u.eid || "-", u.host || "-", u.ip || "-", st, u.radios == null ? "-" : u.radios,
           u.bluez === false ? "BlueZ is not installed there. " + note : note], u.local ? "running" : "");
    });
    others.forEach(function (o) {
      row([o.eid, o.host || "-", o.ip || "-", { pill: "off", text: "no BluForge" }, "-", o.note + ". Install BluForge there to control its radios."], "idle");
    });
    const cl = STATE.cluster || {};
    $("unitsNote").textContent = cl.manager_ok === false
      ? "The LANforge manager's resource list could not be read (" + (cl.manager_error || "no answer") + "), so only this unit is shown."
      : "Resources come from the LANforge manager's own list. Anything you start on a resource radio runs on that resource.";
  }

  function render() {
    renderUnits();
    renderRadios();
    renderKindTable("ifJobsTable", "interfere", [
      { fn: function (j) { return j.id; } },
      { fn: function (j) { return devLabel(j.dev); } },
      { fn: function (j) { return j.args || ""; } },
      { fn: fmtUptime, num: true },
    ]);
    renderKindTable("advJobsTable", "adv", [
      { fn: function (j) { return j.id; } },
      { fn: function (j) { return devLabel(j.dev); } },
      { fn: function (j) { return j.args || ""; } },
      { fn: fmtUptime, num: true },
    ]);
    renderKindTable("snJobsTable", "sniff", [
      { fn: function (j) { return j.id; } },
      { fn: function (j) { return devLabel(j.dev); } },
      { fn: fmtUptime, num: true },
    ]);
    populateDevSelects();
    BF.hooks.length && fireHooks({ type: "render" }, null, null);
    $("statusJobs").textContent = STATE.running.length + " job(s) running";
  }

  // Confirmed by code review: starting N radios together fires N
  // near-simultaneous SSE "status: started" events, each independently
  // triggering refreshStatus() with no sequencing -- an earlier-issued GET
  // resolving after a later one could silently overwrite fresh STATE with a
  // stale snapshot (a genuinely running job flickering back to "idle" for
  // one refresh cycle). A simple monotonic request token fixes it: only the
  // most-recently-issued response is ever applied.
  let _statusReqSeq = 0;
  // BlueZ is installed by the end-user, so until it is present the page offers the installer
  // and every Bluetooth action is refused by the server with the same explanation.
  let bluezBusy = false;
  function applyBluez(b) {
    const banner = $("bluezBanner");
    if (!b) return;
    const show = !b.installed;
    banner.style.display = show ? "" : "none";
    if (!show) { bluezBusy = false; return; }
    $("bluezNotice").textContent = b.notice || "";
    $("bluezInstallBtn").disabled = bluezBusy || b.installing || !b.can_install;
    $("bluezInstallBtn").title = b.can_install ? "" : "installer script (bluforge-bluez) not found on this unit";
  }
  $("bluezInstallBtn").addEventListener("click", async function () {
    const notice = $("bluezNotice").textContent;
    if (!window.confirm(notice + "\n\nInstall BlueZ now?")) return;
    const log = $("bluezLog");
    log.style.display = "";
    log.textContent = "";
    bluezBusy = true;
    $("bluezInstallBtn").disabled = true;
    try { await post("/api/bluez/install", { accept: true }); }
    catch (e) { bluezBusy = false; $("bluezInstallBtn").disabled = false; appendBluezLine("[x] " + e.message); }
  });
  function appendBluezLine(t) {
    const log = $("bluezLog");
    log.style.display = "";
    const d = el("div", t.indexOf("[x]") === 0 ? "r" : t.indexOf("[+]") === 0 ? "g" : t.indexOf("[!]") === 0 ? "y" : "dim", t);
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
  }

  // Self-check results from the server (moved install folder, unwritable/full results disk, no radio)
  function applyHealth(hl) {
    const b = $("healthBanner");
    if (!hl) return;
    const warns = hl.warnings || [];
    b.style.display = warns.length ? "" : "none";
    $("healthText").textContent = warns.join("  |  ");
  }

  function applyState(s) {
    STATE = s;
    applyBluez(s.bluez);
    applyHealth(s.health);
    if (s.hostname) {
      $("hostInfo").textContent = s.hostname;
      const others = (s.units || []).length - 1;
      $("statusHost").textContent = s.hostname + (others > 0 ? "  (+ " + others + " resource" + (others > 1 ? "s" : "") + " in the cluster)" : "");
    }
    if (Array.isArray(s.all_hci)) ALL_HCI = s.all_hci;
  }
  async function refreshStatus() {
    const myReq = ++_statusReqSeq;
    try {
      const s = await api("/api/status");
      if (myReq !== _statusReqSeq) return; // a newer request already won
      applyState(s);
      render();
    } catch (e) { /* server not reachable yet */ }
  }

  // ---------------------------------------------------------------- console
  function consoleFor(kind) {
    return kind === "interfere" ? $("if_console") : kind === "adv" ? $("adv_console") : kind === "sniff" ? $("sn_console") : null;
  }
  // Every "line" SSE event carries the source job's dev/hci (server.py's
  // _reader publishes {job_id, dev, text} per line), but it was previously
  // discarded: all concurrent jobs of the same kind shared one console with
  // nothing to tell their output apart (confirmed by code review -- exactly
  // the multi-radio scenario this GUI now supports). Prefix every line with
  // its source radio instead.
  function appendConsoleLine(kind, dev, text) {
    const box = consoleFor(kind);
    if (!box) return;
    const line = el("div");
    let cls = "dim";
    if (text.indexOf("[*]") === 0) cls = "s";
    else if (text.indexOf("[+]") === 0) cls = "g";
    else if (text.indexOf("[!]") === 0) cls = "y";
    else if (text.indexOf("[x]") === 0) cls = "r";
    line.className = cls;
    line.textContent = (dev != null ? devLabel(dev) + ": " : "") + text;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
    while (box.childElementCount > 300) box.removeChild(box.firstChild);
  }

  // ---------------------------------------------------------------- sniff survey
  // The sniffer process aggregates per device and the server forwards one
  // event per second with only the devices that changed, so this side just
  // upserts records and repaints on a timer (no per-packet DOM work).
  const snDevs = new Map();          // addr -> record (+ _rx: performance.now() of last update)
  const snRows = new Map();          // addr -> { tr, cells }
  let snSelected = null, snStats = {};
  // Clear: addr -> that device's last_seen at the moment of Clear. Both values come from the same clock
  // (the server's, or the browser's for Classic rows), so no cross-clock comparison is needed.
  const snCleared = new Map();

  function snIngest(devs, stats, serverNow) {
    const nowMs = performance.now();
    (devs || []).forEach(function (d) {
      const clearedAt = snCleared.get(d.addr);
      if (clearedAt !== undefined) {
        if (d.last_seen <= clearedAt) return;      // not heard since Clear
        snCleared.delete(d.addr);
      }
      const prev = snDevs.get(d.addr);
      if (prev && prev.last_seen > d.last_seen) return;
      d._rx = serverNow ? nowMs - Math.max(serverNow - d.last_seen, 0) * 1000 : nowMs;
      snDevs.set(d.addr, d);
    });
    if (stats) snStats = stats;
  }

  async function snResync() {
    try {
      const r = await fetch("/api/sniff/devices");
      const j = await r.json();
      (j.sniffs || []).forEach(function (sn) { snIngest(sn.devices, sn.stats, j.now); });
    } catch (e) { /* the SSE stream will bring the next update */ }
    snLoadCaps();
  }

  // ---- packet captures (.pcap for Wireshark) ----
  let snCaps = [], snCapsAt = 0;
  function snBytes(n) {
    if (n == null) return "";
    return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
  }
  async function snLoadCaps() {
    snCapsAt = Date.now();
    try {
      const r = await fetch("/api/sniff/captures");
      const j = await r.json();
      snCaps = j.captures || [];
      renderSnCaps();
    } catch (e) { /* the next sniff update retries */ }
  }
  // The server answers a missing capture with JSON, so fetch first: a failure becomes a message, not a blank page.
  async function snGetCapture(name) {
    try {
      const r = await fetch("/api/sniff/capture/" + encodeURIComponent(name));
      if (!r.ok) {
        let msg = "HTTP " + r.status;
        try { msg = (await r.json()).error || msg; } catch (e) { /* not JSON */ }
        throw new Error(msg);
      }
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = name.replace(/^\d+__/, "");
      document.body.appendChild(a);
      a.click();
      setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    } catch (e) { alert("Could not download the capture: " + e.message); }
  }
  function renderSnCaps() {
    const tbody = document.querySelector("#snCapsTable tbody");
    if (!tbody) return;
    tbody.textContent = "";
    if (!snCaps.length) {
      const tr = el("tr"); const td = el("td", null, "No captures yet - tick \"record packet capture\" and start a survey.");
      td.colSpan = 5; tr.appendChild(td); tbody.appendChild(tr);
      return;
    }
    snCaps.forEach(function (c) {
      const tr = el("tr");
      tr.appendChild(el("td", null, c.file.replace(/^\d+__/, "")));
      tr.appendChild(el("td", null, c.hci == null ? "" : devLabel(c.hci)));
      tr.appendChild(el("td", "num", snBytes(c.size)));
      tr.appendChild(el("td", null, new Date(c.mtime * 1000).toLocaleString() + (c.running ? " (recording)" : "")));
      const act = el("td");
      const b = el("button", "lf-btn", "Download");
      b.addEventListener("click", function () { snGetCapture(c.file); });
      act.appendChild(b); tr.appendChild(act);
      tbody.appendChild(tr);
    });
  }
  $("sn_exp_pcap").addEventListener("click", async function () {
    await snLoadCaps();
    const dev = parseInt($("sn_dev").value, 10);
    // the list is newest first: prefer the selected radio, otherwise the newest capture of any radio
    const c = snCaps.find(function (x) { return x.hci === dev; }) || snCaps[0];
    if (!c) { alert("There is no packet capture yet. Tick \"record packet capture\", start a survey, then download."); return; }
    snGetCapture(c.file);
  });

  function snAgo(d) { return Math.max((performance.now() - d._rx) / 1000, 0); }
  function snAgoText(d) {
    const a = snAgo(d);
    return a < 2 ? "now" : a < 90 ? Math.round(a) + "s ago" : Math.round(a / 60) + "m ago";
  }
  function snSummary(d) {
    const parts = [];
    (d.services || []).slice(0, 4).forEach(function (sv) { parts.push(sv.name || sv.uuid); });
    if ((d.services || []).length > 4) parts.push("+" + (d.services.length - 4) + " more");
    const fmts = (d.mfg || []).map(function (m) { return m.format; }).filter(Boolean);
    if (d.beacon && fmts.indexOf(d.beacon.type) < 0) parts.push("beacon: " + d.beacon.type);
    fmts.forEach(function (f) { parts.push(f); });
    (d.service_data || []).forEach(function (sd) { if (!sd.decoded) parts.push("data " + (sd.name || sd.uuid)); });
    if (d.appearance) parts.push(d.appearance);
    return parts.join(", ");
  }
  function snKindText(d) {
    const co = d.company || ((d.mfg && d.mfg[0]) ? "mfg 0x" + d.mfg[0].id.toString(16).padStart(4, "0") : "");
    return d.kind && co && d.kind.indexOf(co) < 0 ? co + " - " + d.kind : (d.kind || co);
  }
  function snRssiText(d) {
    return d.rssi == null ? "" : d.rssi + (d.rssi_avg != null ? " (avg " + Math.round(d.rssi_avg) + ")" : "");
  }
  function snVisible(d) {
    if ($("sn_hidestale").checked && snAgo(d) > 30) return false;
    const q = $("sn_search").value.trim().toLowerCase();
    if (!q) return true;
    const hay = [d.addr, d.name, d.company, d.kind, snSummary(d), (d.supports || []).join(" ")].join(" ").toLowerCase();
    return hay.indexOf(q) >= 0;
  }
  const SN_SORTS = {
    rssi: function (a, b) { return (b.rssi == null ? -999 : b.rssi) - (a.rssi == null ? -999 : a.rssi); },
    first: function (a, b) { return a.first_seen - b.first_seen; },
    last: function (a, b) { return b._rx - a._rx; },
    count: function (a, b) { return b.count - a.count; },
    name: function (a, b) {
      if (!a.name !== !b.name) return a.name ? -1 : 1;          // unnamed devices last
      return (a.name || "").localeCompare(b.name || "") || a.addr.localeCompare(b.addr);
    },
  };

  function snSetCell(row, i, text) {
    const c = row.cells[i];
    if (c.textContent !== text) c.textContent = text;
  }

  function renderSniffTable() {
    const tbody = document.querySelector("#sniffTable tbody");
    const list = Array.from(snDevs.values()).filter(snVisible).sort(SN_SORTS[$("sn_sort").value] || SN_SORTS.rssi);
    const keep = new Set();
    let prevNode = null;
    list.forEach(function (d) {
      keep.add(d.addr);
      let row = snRows.get(d.addr);
      if (!row) {
        const tr = el("tr");
        tr.style.cursor = "pointer";
        tr.addEventListener("click", function () { snSelected = d.addr; renderSniffTable(); });
        const cells = [];
        for (let i = 0; i < 9; i++) { const td = el("td", i === 4 || i === 5 ? "num" : null); tr.appendChild(td); cells.push(td); }
        row = { tr: tr, cells: cells };
        snRows.set(d.addr, row);
      }
      const cur = snDevs.get(d.addr);
      snSetCell(row, 0, cur.addr);
      snSetCell(row, 1, (cur.addr_type === "classic" ? "classic" : (cur.addr_kind || cur.addr_type || "")).replace("random-", "").replace(" (RPA)", " RPA"));
      snSetCell(row, 2, cur.name || "");
      snSetCell(row, 3, snKindText(cur));
      snSetCell(row, 4, snRssiText(cur));
      snSetCell(row, 5, cur.addr_type === "classic" ? "-" : String(cur.count));
      snSetCell(row, 6, cur.addr_type === "classic" ? (cur.classic && cur.classic.connected ? "connected" : "classic") :
                          cur.directed ? "directed" : cur.connectable ? "yes" : "no");
      snSetCell(row, 7, snSummary(cur));
      snSetCell(row, 8, snAgoText(cur));
      row.tr.style.background = snSelected === d.addr ? "var(--lf-select-bg)" : "";
      // keep DOM order == sort order with the fewest moves
      const want = prevNode ? prevNode.nextSibling : tbody.firstChild;
      if (want !== row.tr) tbody.insertBefore(row.tr, want);
      prevNode = row.tr;
    });
    snRows.forEach(function (row, addr) {
      if (!keep.has(addr)) { row.tr.remove(); snRows.delete(addr); }
    });
    snFillRadios();         // the radio pickers must be filled even before any device is selected
    const st = snStats || {};
    $("sn_stats").textContent = list.length + " shown / " + snDevs.size + " devices" +
      (st.rate != null ? ", " + st.rate + " pkt/s" : "") +
      (st.pcap_packets != null ? ", capture " + st.pcap_packets + " packets / " + snBytes(st.pcap_bytes) + (st.pcap_full ? " (FULL, recording stopped)" : "") : "") +
      (st.idle_s != null && st.idle_s > 5 ? ", no packets for " + Math.round(st.idle_s) + "s" : "") +
      (st.evicted ? ", " + st.evicted + " evicted" : "");
    renderSniffDetail();
  }

  function renderSniffDetail() {
    const box = $("sn_detail");
    const d = snSelected && snDevs.get(snSelected);
    if (!d) { box.style.display = "none"; return; }
    box.style.display = "";
    snFillRadios();
    snRenderInterro();
    $("sn_int_btn").disabled = snIntBusy;
    $("sn_detail_title").textContent = "Device detail - " + d.addr + (d.name ? "  (" + d.name + ")" : "");
    const body = $("sn_detail_body");
    body.textContent = "";
    function sect(title, lines) {
      if (!lines || !lines.length) return;
      const h = el("div", null, title);
      h.style.cssText = "font-weight:700;margin-top:8px;color:#0082fc";
      body.appendChild(h);
      lines.forEach(function (t) { const l = el("div", null, t); l.style.paddingLeft = "10px"; body.appendChild(l); });
    }
    sect("Identity", [
      "Address: " + d.addr + "  [" + (d.addr_kind || d.addr_type) + "]",
      d.name ? "Name: " + d.name + (d.name_complete === false ? " (shortened)" : "") : null,
      snKindText(d) ? "Vendor / kind: " + snKindText(d) : null,
      d.appearance ? "Appearance: " + d.appearance : null,
    ].filter(Boolean));
    sect("What it advertises it supports", d.supports);
    if (d.addr_type === "classic") {
      sect("Classic device", [d.addr_kind, d.classic && d.classic.class ? "Class of device: " + d.classic.class : null,
        d.classic && d.classic.icon ? "Icon: " + d.classic.icon : null,
        "RSSI: " + (d.rssi != null ? d.rssi + " dBm" : "not heard in this inquiry")].filter(Boolean));
    } else sect("Radio", [
      "RSSI: last " + d.rssi + ", avg " + d.rssi_avg + ", min " + d.rssi_min + ", max " + d.rssi_max + " dBm",
      "Packets: " + d.count + (d.rate_hz ? " (" + d.rate_hz + "/s as seen)" : ""),
      "PDU types: " + Object.keys(d.pdus || {}).map(function (k) { return k + " x" + d.pdus[k]; }).join(", "),
      "PHY: " + ((d.phys || []).join(", ") || "1M (legacy)") + (d.ext ? "   extended advertising" + (d.sid != null ? " (SID " + d.sid + ")" : "") : ""),
      d.tx_power != null ? "Advertised Tx power: " + d.tx_power + " dBm" : null,
    ].filter(Boolean));
    sect("Flags", d.flags);
    sect("Services (advertised)", (d.services || []).map(function (s) { return (s.name ? s.name + "  " : "") + s.uuid; }));
    sect("Service data", (d.service_data || []).map(function (s) {
      return (s.name || s.uuid) + ": " + (s.data || "(empty)") + (s.decoded ? "   " + JSON.stringify(s.decoded) : "");
    }));
    sect("Manufacturer data", (d.mfg || []).map(function (m) {
      return (m.company || "company 0x" + m.id.toString(16).padStart(4, "0")) + (m.format ? " [" + m.format + "]" : "") + ": " + (m.data || "(empty)");
    }));
    sect("Beacon", d.beacon ? Object.keys(d.beacon).map(function (k) { return k + ": " + d.beacon[k]; }) : null);
    sect("URI", d.uri ? [d.uri] : null);
    sect("Raw advertising data", Object.keys(d.raw || {}).map(function (k) { return k + ": " + d.raw[k]; }));
  }

  // ---- interrogate (connect read-only, list GATT + SDP) ----
  const snInterro = new Map();       // addr -> result of the last interrogation
  let snIntBusy = false, snIntShown = null, snIntDirty = false;

  function snFillRadios() {
    snFillRadioSelect($("sn_int_dev"));
    snFillRadioSelect($("sn_cl_dev"));
  }
  function snFillRadioSelect(sel) {
    const busyBle = {};
    STATE.running.forEach(function (j) { busyBle[j.dev] = true; });
    const opts = ALL_HCI.filter(function (h) { return !busyBle[h]; });
    const key = opts.join(",") + "|" + (STATE.classic_reserved || []).join(",");
    if (sel.dataset.key === key) return;
    sel.dataset.key = key;
    const keep = sel.value;
    sel.textContent = "";
    opts.forEach(function (h) {
      const rsv = (STATE.classic_reserved || []).indexOf(h) !== -1;
      const o = el("option", null, devLabel(h) + (rsv ? " (ready)" : " (will reserve)"));
      o.value = String(h);
      sel.appendChild(o);
    });
    if (keep) sel.value = keep;
  }

  // ---- Classic (BR/EDR) inquiry: merges into the same table ----
  const COD_MAJOR = { 1: "Computer", 2: "Phone", 3: "Network access point", 4: "Audio/Video", 5: "Peripheral",
                      6: "Imaging", 7: "Wearable", 8: "Toy", 9: "Health" };
  const COD_AV = { 1: "Wearable headset", 2: "Hands-free", 4: "Microphone", 5: "Loudspeaker", 6: "Headphones",
                   7: "Portable audio", 8: "Car audio", 9: "Set-top box", 10: "HiFi audio", 11: "VCR",
                   12: "Video camera", 13: "Camcorder", 14: "Video monitor", 15: "Video display + loudspeaker",
                   16: "Video conferencing", 18: "Gaming/toy" };
  function snCodText(cls) {
    if (!cls) return "";
    const v = parseInt(cls, 16);
    if (isNaN(v)) return "";
    const major = (v >> 8) & 0x1f, minor = (v >> 2) & 0x3f;
    return (major === 4 && COD_AV[minor]) ? COD_AV[minor] : (COD_MAJOR[major] || "Class 0x" + v.toString(16));
  }
  async function snClassicScan() {
    const hci = parseInt($("sn_cl_dev").value, 10);
    const st = $("sn_cl_status");
    if (isNaN(hci)) { st.textContent = "no radio available (stop the survey on one radio, or release one)"; return; }
    $("sn_cl_btn").disabled = true;
    try {
      if ((STATE.classic_reserved || []).indexOf(hci) === -1) {
        st.textContent = "reserving " + devLabel(hci) + " for BlueZ...";
        await post("/api/classic/reserve", { dev: hci, on: true });
        await refreshStatus();
      }
      const secs = Math.max(3, Math.min(60, parseInt($("sn_cl_secs").value, 10) || 12));
      st.textContent = "inquiry running (" + secs + " s)...";
      const res = await post("/api/classic/action", { dev: hci, action: "scan", params: { duration: secs, transport: "auto" } });
      const nowS = Date.now() / 1000;
      // BR/EDR devices carry a class of device; LE-only entries BlueZ cached are the survey's job
      const devs = (res.devices || []).filter(function (d) {
        const prev = snDevs.get(d.addr);
        return d.class && !(prev && prev.addr_type !== "classic");
      }).map(function (d) {
        const heard = d.rssi != null;
        const extra = [];
        if (d.paired) extra.push("paired");
        if (d.connected) extra.push("connected");
        return {
          m: "dev", addr: d.addr, addr_type: "classic", addr_kind: "BR/EDR " + (heard ? "(heard)" : d.connected ? "(connected)" : "(known, not heard)"),
          first_seen: nowS, last_seen: nowS, count: 0, rssi: d.rssi, rssi_avg: null, rssi_min: null, rssi_max: null,
          pdus: {}, connectable: true, scannable: false, directed: false, ext: false, phys: [],
          name: d.name && d.name !== d.addr.replace(/:/g, "-") ? d.name : "", flags: [],
          appearance: null, services: (d.uuids || []).map(function (u) { return { uuid: u, name: null }; }),
          mfg: [], service_data: [], beacon: null, raw: {},
          kind: snCodText(d.class) || d.icon || "", company: null,
          supports: ["Bluetooth Classic (BR/EDR) device"].concat(extra, d.class ? ["Class of device " + d.class] : [],
                      d.modalias ? ["Modalias " + d.modalias] : []),
          classic: { class: d.class, icon: d.icon, paired: d.paired, connected: d.connected, heard: heard },
        };
      });
      snIngest(devs, null, nowS);
      st.textContent = devs.length + " device(s): " + devs.filter(function (d) { return d.classic.heard; }).length + " heard in this inquiry";
    } catch (e) {
      st.textContent = "failed: " + e.message;
    } finally {
      $("sn_cl_btn").disabled = false;
    }
  }
  $("sn_cl_btn").addEventListener("click", snClassicScan);

  function snRenderInterro() {
    const out = $("sn_int_out");
    const r = snSelected && snInterro.get(snSelected);
    const sig = r ? snSelected + ":" + r.when : "";
    if (sig === snIntShown && !snIntDirty) return;
    snIntShown = sig; snIntDirty = false;
    out.textContent = "";
    if (!r) return;
    function line(t, indent, bold) {
      const d = el("div", null, t);
      d.style.paddingLeft = (indent * 14) + "px";
      if (bold) d.style.fontWeight = "700";
      out.appendChild(d);
    }
    const h = el("div", null, "Interrogation result (" + new Date(r.when).toLocaleTimeString() + ", via " + devLabel(r.hci) + ")");
    h.style.cssText = "font-weight:700;margin-top:8px;color:#0082fc";
    out.appendChild(h);
    const dv = r.device || {};
    line("Name: " + (dv.name || "-") + "   address type: " + (dv.address_type || "-") +
         (dv.class ? "   class: " + dv.class : "") + (dv.icon ? "   icon: " + dv.icon : "") +
         (dv.modalias ? "   modalias: " + dv.modalias : ""), 1);
    const gs = r.gatt_stats;
    line("GATT: " + (r.gatt || []).length + " service(s)" + (gs ? ", " + gs.read_ok + " value(s) read, " + gs.read_failed +
         " read failed" + (gs.needs_security ? " (" + gs.needs_security + " because the device wants security)" : "") + ", " +
         gs.skipped_secure + " need encryption (not read)" : ""), 1, true);
    if (r.paired_during_interrogation) line("Note: this device asked for security and paired during the read; the pairing was removed afterwards.", 1, true);
    (r.gatt || []).forEach(function (sv) {
      line((sv.name || "Unknown/vendor service") + "   " + sv.uuid, 1, true);
      (sv.characteristics || []).forEach(function (c) {
        line((c.name || "characteristic") + "   " + c.uuid + "   [" + (c.flags || []).join(", ") + "]", 2);
        if (c.value_hex != null) line("= " + (c.value != null ? c.value + "   (" + c.value_hex + ")" : c.value_hex || "(empty)"), 3);
        else if (c.read_error) line("read failed: " + c.read_error, 3);
        (c.descriptors || []).forEach(function (d) { line("descriptor: " + (d.name || d.uuid), 3); });
      });
    });
    if (!(r.gatt || []).length) line("No GATT services resolved (BR/EDR-only device, or it requires pairing).", 2);
    const uu = (r.uuids || []).filter(function (u) { return u.name; });
    if (uu.length) {
      line("Profiles reported by BlueZ (UUIDs)", 1, true);
      uu.forEach(function (u) { line(u.name + "   " + u.uuid, 2); });
    }
    if (r.sdp && r.sdp.length) {
      line("Bluetooth Classic SDP records: " + r.sdp.length, 1, true);
      r.sdp.forEach(function (s) {
        const prof = (s.profiles || []).map(function (p) { return p.profile + (p.version ? " v" + p.version : ""); }).join(", ");
        line((s.name || (s.classes && s.classes[0]) || "record " + s.handle) +
             (s.rfcomm_channel != null ? "   RFCOMM ch " + s.rfcomm_channel : "") +
             (s.l2cap_psm ? "   L2CAP PSM " + s.l2cap_psm : "") + (prof ? "   profile: " + prof : ""), 2);
        if (s.classes && s.classes.length) line("classes: " + s.classes.join(", "), 3);
      });
    } else if (r.sdp_note) line("SDP: " + r.sdp_note, 1);
  }

  $("sn_int_btn").addEventListener("click", async function () {
    const addr = snSelected;
    const hci = parseInt($("sn_int_dev").value, 10);
    if (!addr || isNaN(hci) || snIntBusy) return;
    const st = $("sn_int_status");
    snIntBusy = true; $("sn_int_btn").disabled = true;
    try {
      if ((STATE.classic_reserved || []).indexOf(hci) === -1) {
        st.textContent = "reserving " + devLabel(hci) + " for BlueZ...";
        await post("/api/classic/reserve", { dev: hci, on: true });
        await refreshStatus();
      }
      st.textContent = "interrogating " + addr + " (up to ~2 min)...";
      const det = snDevs.get(addr) || {};
      const res = await post("/api/classic/action", { dev: hci, action: "interrogate", params: {
        addr: addr, read: $("sn_int_read").checked,
        transport: det.addr_type === "random" ? "le" : det.addr_type === "classic" ? "bredr" : "auto" } });
      res.when = Date.now(); res.hci = hci;
      snInterro.set(addr, res);
      snIntDirty = true;
      st.textContent = "done";
    } catch (e) {
      st.textContent = "failed: " + e.message;
    } finally {
      snIntBusy = false; $("sn_int_btn").disabled = false;
    }
  });

  function snDownload(name, mime, text) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([text], { type: mime }));
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }
  function snCsvCell(v) {
    let t = v == null ? "" : String(v);
    // Text can come from the air (device names): stop a spreadsheet reading it as a formula. Numbers
    // (RSSI such as -60) are not text and must stay numbers.
    if (typeof v === "string" && /^[=+\-@\t\r]/.test(t)) t = "'" + t;
    return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  }
  function snExportRecords() {
    return Array.from(snDevs.values()).map(function (d) {
      const o = Object.assign({}, d);
      delete o._rx;
      if (snInterro.has(d.addr)) o.interrogation = snInterro.get(d.addr);
      return o;
    });
  }
  const SN_STAMP = function () { return new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19); };
  $("sn_exp_json").addEventListener("click", function () {
    // Self-describing export: what wrote it, when, and the units, so the file still means something later.
    const doc = {
      schema: 1, tool: { name: "BluForge", version: STATE.version || null },
      exported_at: new Date().toISOString(), unit: STATE.hostname || null,
      units: { rssi: "dBm (BLE rows); Classic rows carry no RSSI from an inquiry", first_seen: "server clock, epoch seconds",
               last_seen: "server clock, epoch seconds (browser clock for Classic rows)" },
      device_count: snDevs.size, devices: snExportRecords(),
    };
    snDownload("ble-survey-" + SN_STAMP() + ".json", "application/json", JSON.stringify(doc, null, 1));
  });
  $("sn_exp_csv").addEventListener("click", function () {
    const cols = ["addr", "addr_kind", "name", "company", "kind", "rssi", "rssi_avg", "rssi_min", "rssi_max",
                  "count", "connectable", "scannable", "ext", "phys", "services", "supports", "first_seen", "last_seen"];
    const lines = [cols.join(",")];
    snExportRecords().forEach(function (d) {
      const row = Object.assign({}, d, {
        phys: (d.phys || []).join(" "),
        services: (d.services || []).map(function (s) { return s.name ? s.name + " " + s.uuid : s.uuid; }).join("; "),
        supports: (d.supports || []).join("; "),
      });
      lines.push(cols.map(function (c) { return snCsvCell(row[c]); }).join(","));
    });
    snDownload("ble-survey-" + SN_STAMP() + ".csv", "text/csv", lines.join("\n") + "\n");
  });
  $("sn_clear").addEventListener("click", function () {
    snDevs.forEach(function (d) { snCleared.set(d.addr, d.last_seen); });   // a device reappears when heard again
    snDevs.clear(); snSelected = null; renderSniffTable();
  });
  ["sn_search", "sn_sort", "sn_hidestale"].forEach(function (id) {
    $(id).addEventListener("input", function () { renderSniffTable(); });
  });
  setInterval(renderSniffTable, 1000);

  // ---------------------------------------------------------------- SSE
  // job_id -> {kind, dev}, updated SYNCHRONOUSLY from each "status" event's
  // embedded job data. A "line" event for a just-started job can arrive
  // before the async refreshStatus() GET triggered by that same "started"
  // event resolves; looking kind/dev up only in STATE.running (which that
  // fetch populates) silently dropped such lines (confirmed by code
  // review). This map has no such race -- it's set from data already in
  // hand, not fetched.
  const jobMeta = new Map();

  function connectStream() {
    const es = new EventSource("/api/stream");
    es.onopen = function () { $("connDot").classList.add("up"); $("connText").textContent = "connected"; snResync(); };
    es.onerror = function () { $("connDot").classList.remove("up"); $("connText").textContent = "reconnecting..."; };
    es.onmessage = function (ev) {
      let evt;
      try { evt = JSON.parse(ev.data); } catch (e) { return; }
      if (evt.type === "status") {
        if (evt.status) { applyState(evt.status); render(); }
        else {
          if (evt.job) jobMeta.set(evt.job.id, { kind: evt.job.kind, dev: evt.job.dev });
          fireHooks(evt, evt.job ? evt.job.kind : null, evt.job ? evt.job.dev : null);
          refreshStatus();
          if (evt.job && evt.job.kind === "sniff") snLoadCaps();       // a capture file appears / is complete
        }
      } else if (evt.type === "bluez") {
        if (evt.done) {
          bluezBusy = false;
          appendBluezLine(evt.ok ? "[+] BlueZ is ready - the page is unlocked." : "[x] BlueZ installation did not complete - see the log above.");
          refreshStatus();
        } else if (evt.text) appendBluezLine(evt.text);
      } else if (evt.type === "sniff") {
        snIngest(evt.devices, evt.stats, evt.now);
        if (Date.now() - snCapsAt > 5000) snLoadCaps();                // keeps the sizes of a running capture fresh
      } else if (evt.type === "line") {
        const meta = jobMeta.get(evt.job_id) ||
          STATE.running.find(function (j) { return j.id === evt.job_id; });
        let kind = meta ? meta.kind : null;
        const dev = meta ? meta.dev : evt.dev;
        if (evt.kind) kind = evt.kind;            // one-shot Classic actions carry their own kind
        if (kind && kind.indexOf("c_") === 0) fireHooks(evt, kind, dev);
        else if (kind === "sniff") appendConsoleLine(kind, dev, evt.text);
        else appendConsoleLine(kind, dev, evt.text);
      }
    };
  }


  // ---------------------------------------------------------------- (i) help tips
  // Plain-language help beside each control on the Interference and Advertise tabs. The icon is added next to
  // the label (never inside it: the channel label's text is rewritten per mode), shows on hover or keyboard focus.
  const TIPS = {
    if_dev_checks: "Which Bluetooth radios transmit. Each ticked radio runs its own job with the settings below. A radio already running a job is busy and cannot be selected.",
    if_mode: "What to transmit and where. Single: one fixed channel. Sweep: step through a channel range. Hop: step through a list you type. Wi-Fi: cover the BLE channels that sit under a Wi-Fi channel. Band: step through all 40 channels. Adv-flood: instead of test packets, send real advertising beacons very quickly.",
    if_arg: "",
    if_payload: "The bit pattern inside each test packet. PRBS9 looks like random data (closest to real traffic). The all-ones, all-zeros and alternating patterns give a steadier, more regular signal.",
    if_phy: "Radio speed of the test packets. 1M = 1 Mbit/s (the standard BLE speed, about 1 MHz wide). 2M = 2 Mbit/s (faster, about 2 MHz wide).",
    if_len: "Bytes of data in each test packet, 0-255. Longer packets keep the channel busy for more of the time.",
    if_dwell: "How long to stay on each channel before moving to the next, in milliseconds. Only matters when more than one channel is selected.",
    if_power: "How strong the transmission is. Default = the controller's own level. On the BE200 the setting is followed up to about +5 dBm and capped at about +8 dBm, so higher gives the same result. Below about -20 dBm packets start to be lost at close range, and there is a floor near -40 dBm. DTM cannot report the power it applied; adv-flood does (see the console).",
    if_bw: "Width of the Wi-Fi channel being covered, 20 or 40 MHz. Only used in wifi mode.",
    if_duration: "How long to run, in seconds. 0 = keep going until you press Stop.",
    if_map: "Show the plan (channels and their frequencies) in the console without transmitting anything.",
    cl_dev: "The Bluetooth radio that will stream. It is handed to BlueZ (Classic) while it is reserved, so it cannot run BLE jobs. Each Classic radio carries one stream.",
    cl_addr: "What receives the traffic. Audio: a paired speaker, earbuds or headset (pair it on the Bluetooth Classic tab; powered on and in range). Data types: another radio of this unit, which runs the receiving end. File transfer can also go to a paired device that offers OBEX Object Push. Everything it receives is real Classic traffic on the air.",
    cl_kind: "What kind of Classic traffic to generate. Audio streams A2DP. File transfer, video and HID are real data links shaped like those applications. The data types need the internal receiver (another radio of this unit); a real device can only take file transfer.",
    cl_fmb: "Size of each file sent. Files go back to back with a short pause between them; the result reports the speed of every completed file.",
    cl_vbr: "Bitrate of the video being downloaded. The player buffers three segments up front, then fetches one segment every four seconds; segments that take longer than that to arrive are counted as late.",
    cl_src: "What audio is streamed. White noise keeps variable-bitrate codecs (AAC, aptX, LDAC) at their highest rate; SBC is a fixed rate whatever you stream. An audio file gives real music, looped.",
    cl_codec: "Force a codec, or leave on Auto to use the best one both sides support. Higher-bitrate codecs put more load on the air.",
    cl_dur: "How long to run, in seconds. 0 = keep going until you press Stop. It runs unattended and reconnects by itself if the link drops.",
    adv_dev_checks: "Which Bluetooth radios send the advertisement. Tick as many as you like: each ticked radio runs its own advertising job with the settings below, at the same time. A radio already running a job is busy and cannot be ticked.",
    adv_type: "The kind of advertising packet. ADV_IND: connectable and scannable, like a typical device. ADV_SCAN_IND: scannable but not connectable. ADV_NONCONN_IND: beacon only, nobody can connect or ask for more. ADV_DIRECT_IND: aimed at one specific device (needs its address); high duty repeats quickly and stops by itself after about 1.28 s, low duty keeps going at your interval.",
    adv_interval: "Time between advertisements, in milliseconds (20-10240). Shorter means more packets on the air and quicker detection; longer is quieter.",
    adv_peer: "Bluetooth address (AA:BB:CC:DD:EE:FF) of the one device a directed advertisement is aimed at. Only used for the two ADV_DIRECT_IND types.",
    adv_profile: "A ready-made beacon payload: iBeacon, Eddystone-UID, Eddystone-URL, AltBeacon or a generic sensor. With none selected, only the local name is sent.",
    adv_profile_cfg: "Settings for the chosen profile as key=value pairs separated by commas, e.g. major=1,minor=2. The power= key here is the calibration value written inside the beacon (phones use it to estimate distance); it is not the radio's TX power.",
    adv_name: "The device name shown in scanners. Used when no beacon profile is chosen. At most 29 bytes.",
    adv_power: "How strong the advertisement is sent. Default = the controller's own level (about +2 dBm). A chosen value is sent to the controller and the console shows the power it actually applied. The BE200 caps at about +8 dBm.",
    adv_duration: "How long to advertise, in seconds. 0 = keep going until you press Stop.",
  };
  const IF_ARG_TIPS = {
    single: "The BLE channel to transmit on, 0-39. Channel N is at 2402 + 2 x N MHz, so 19 is 2440 MHz.",
    sweep: "First and last BLE channel to step through, e.g. 0-39. Channel N is at 2402 + 2 x N MHz.",
    hop: "BLE channels to step through, separated by commas, e.g. 0,12,24,39. Channel N is at 2402 + 2 x N MHz.",
    wifi: "The 2.4 GHz Wi-Fi channel (1-13) to interfere with. The BLE channels underneath it are picked for you.",
  };
  const tipIcons = {};
  function makeIcon(text) {
    const icon = el("span", "lf-tip", "i");
    icon.tabIndex = 0;
    icon.setAttribute("role", "note");
    const set = function (t) { icon.dataset.tip = t; icon.setAttribute("aria-label", t); };
    set(text);
    icon.setTip = set;
    const place = function () {
      const r = icon.getBoundingClientRect(), W = 270, pad = 8;
      let left = r.left - 6;
      if (left + W > window.innerWidth - pad) left = r.right + 6 - W;     // near the right edge: open leftwards
      icon.style.setProperty("--tx", Math.max(pad, left) + "px");
      const up = r.bottom + 150 > window.innerHeight && r.top > 160;       // near the bottom: open upwards
      icon.classList.toggle("up", up);
      icon.style.setProperty("--ty", (r.bottom + 4) + "px");
      icon.style.setProperty("--by", (window.innerHeight - r.top + 4) + "px");
    };
    icon.addEventListener("mouseenter", place);
    icon.addEventListener("focus", place);
    return icon;
  }
  // beside a labelled field (the label's text may be rewritten, so the icon is never put inside it)
  function addTip(id, text) {
    const ctl = $(id);
    if (!ctl) return;
    const icon = makeIcon(text);
    tipIcons[id] = icon.setTip;
    const field = ctl.closest(".lf-field");
    const label = field && [...field.children].find(function (c) { return c.tagName === "LABEL"; });
    if (label) {
      const row = el("div", "lf-labelrow");
      field.insertBefore(row, label);
      row.appendChild(label);
      row.appendChild(icon);
    } else if (ctl.closest("label")) {
      ctl.closest("label").appendChild(icon);                // checkbox: icon follows its caption
    }
  }
  Object.keys(TIPS).forEach(function (id) { if (id !== "if_arg") addTip(id, TIPS[id]); });
  addTip("if_arg", IF_ARG_TIPS.single);
  $("if_mode").addEventListener("change", function () {
    if (tipIcons.if_arg && IF_ARG_TIPS[$("if_mode").value]) tipIcons.if_arg(IF_ARG_TIPS[$("if_mode").value]);
  });


  // ---------------------------------------------------------------- init
  loadCaps().then(refreshStatus);
  connectStream();
  setInterval(refreshStatus, 5000);
})();
