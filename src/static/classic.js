/* Bluetooth Classic (BR/EDR) tab. Pure vanilla JS, no libraries. Talks to
   /api/classic/* (one-shot actions, reservation, results) and /api/start for
   the long-running tests; live data arrives through app.js's SSE stream via
   window.BF.hooks. */
(function () {
  "use strict";
  const BF = window.BF;
  if (!BF) { console.error("classic.js needs app.js (window.BF)"); return; }

  const JOB_LABEL = { c_a2dp: "A2DP stream", c_load: "Realistic Classic interference", c_traffic: "Realistic Classic traffic", c_avrcp_watch: "AVRCP button watch", c_cycle: "Connect cycling", c_hfp: "HFP call" };
  const ADDR_RE = /^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$/;
  const MAX_POINTS = 300;
  const ACL_CAP = "ACL throughput, exact (kbit/s) - TX sent / controller-completed / RX";

  let pending = 0;                 // in-flight one-shot actions (UI is locked while > 0)
  let lastDevices = [];
  let infoTimer = null;
  const live = { rssi: [], kbps: [], done: [], rx: [], dAvg: [], dP95: [], dMax: [], jit: [], lost: 0, lb: false,
                 sum: { kbps: 0, done: 0, rx: 0 }, n: 0, active: 0, total: 0, dc: 0, keys: 0, job: null };

  // ---------------------------------------------------------------- utils
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    let j = null;
    try { j = await r.json(); } catch (e) { /* non-JSON error body */ }
    if (!r.ok || (j && j.ok === false)) throw new Error((j && (j.error || j.msg)) || r.statusText);
    return j;
  }
  async function get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  }
  function num(id) { return $(id).value.trim(); }
  function dev() { const v = parseInt($("c_dev").value, 10); return isNaN(v) ? null : v; }
  function addr() { return $("c_addr").value.trim().toUpperCase(); }
  function fmt(v, unit, digits) {
    if (v === null || v === undefined || v === "") return "-";
    return (typeof v === "number" && digits !== undefined ? v.toFixed(digits) : String(v)) + (unit || "");
  }
  function state() { return BF.state(); }
  function isReserved(h) { return (state().classic_reserved || []).indexOf(h) !== -1; }
  function jobOn(h) {
    return (state().running || []).find(function (j) { return j.dev === h && j.kind.indexOf("c_") === 0; });
  }
  function bleJobOn(h) {
    return (state().running || []).find(function (j) { return j.dev === h && j.kind.indexOf("c_") !== 0; });
  }

  // ---------------------------------------------------------------- console / banner
  function consoleLine(text, dev_, forceCls) {
    const box = $("c_console");
    const line = el("div");
    let cls = "dim";
    if (forceCls) cls = forceCls;
    else if (text.indexOf("[*]") === 0) cls = "s";
    else if (text.indexOf("[+]") === 0) cls = "g";
    else if (text.indexOf("[!]") === 0) cls = "y";
    else if (text.indexOf("[x]") === 0) cls = "r";
    line.className = cls;
    line.textContent = (dev_ !== null && dev_ !== undefined ? BF.label(dev_) + ": " : "") + text;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
    while (box.childElementCount > 400) box.removeChild(box.firstChild);
  }
  function banner(text, bad) {
    const b = $("c_pf_banner");
    if (!text) { b.classList.add("hidden"); return; }
    b.className = "c-banner" + (bad ? " bad" : "");
    b.textContent = text;
  }

  // ---------------------------------------------------------------- enablement
  function applyEnablement() {
    const h = dev();
    const reserved = h !== null && isReserved(h);
    const job = h !== null ? jobOn(h) : null;
    const ble = h !== null ? bleJobOn(h) : null;
    const locked = pending > 0;
    // always allowed: choosing a radio, reserving/releasing (server re-checks)
    const needReserved = ["c_scan", "c_list", "c_pair", "c_connect", "c_disconnect", "c_remove", "c_info",
      "c_a2_start", "c_vol_test", "c_w_start", "c_cy_start", "c_hf_start"];
    const stopsStream = ["c_scan", "c_pair", "c_connect", "c_disconnect", "c_remove", "c_a2_start", "c_w_start", "c_cy_start", "c_hf_start"];
    function setDis(node, off) { if (node.disabled !== off) node.disabled = off; }
    needReserved.forEach(function (id) {
      let off = !reserved || locked;
      if (job && stopsStream.indexOf(id) !== -1) off = true;
      setDis($(id), off);
    });
    document.querySelectorAll("[data-plr]").forEach(function (b) { setDis(b, !reserved || locked); });
    setDis($("c_reserve"), locked || (!!ble && !reserved));
    const rlabel = reserved ? "Release to BLE engine" : "Reserve for Classic";
    if ($("c_reserve").textContent !== rlabel) $("c_reserve").textContent = rlabel;
    document.querySelectorAll("#c_devTable .mini-btn").forEach(function (b) {
      setDis(b, locked || !!(job && b.dataset.needsIdle === "1"));
    });
    let txt = "not reserved";
    let pill = "off";
    if (ble) { txt = "BLE job " + ble.id + " running"; pill = "busy"; }
    else if (job) { txt = "Classic job " + job.id + " (" + (JOB_LABEL[job.kind] || job.kind) + ")"; pill = "busy"; }
    else if (reserved) { txt = "reserved - idle"; pill = "ok"; }
    const rs = $("c_radio_state");
    const sig = pill + "|" + txt;
    if (rs.dataset.sig !== sig) {
      rs.dataset.sig = sig;
      rs.innerHTML = "";
      rs.appendChild(el("span", "pill " + pill, txt));
    }
  }

  function populateDev() {
    const sel = $("c_dev");
    const prev = sel.value;
    const all = state().all_hci || [];
    const sig = all.join(",") + "|" + (state().classic_reserved || []).join(",");
    if (sel.dataset.sig === sig) return;
    sel.dataset.sig = sig;
    sel.innerHTML = "";
    all.forEach(function (h) {
      const o = el("option", null, BF.label(h) + (isReserved(h) ? "  (Classic)" : ""));
      o.value = h;
      sel.appendChild(o);
    });
    if (prev && all.indexOf(parseInt(prev, 10)) !== -1) sel.value = prev;
    else if ((state().classic_reserved || []).length) sel.value = state().classic_reserved[0];
    else if (all.length) {                                       // default away from hci0 (bluetoothctl's default), on this unit if it has radios
      const here = all.filter(function (h) { return h < 100; });
      sel.value = (here.length ? here : all).slice(-1)[0];
    }
  }

  // ---------------------------------------------------------------- one-shot actions
  async function act(action, params, quiet) {
    const h = dev();
    if (h === null) throw new Error("no radio selected");
    // Background polls (quiet) must not lock the page: locking on every 5 s
    // auto-refresh made every button grey out and flash back.
    if (!quiet) { pending++; applyEnablement(); }
    try {
      const r = await post("/api/classic/action", { dev: h, action: action, params: params || {} });
      if (!quiet) banner(null);
      return r;
    } catch (e) {
      if (!quiet) { consoleLine("[x] " + action + ": " + e.message, h); banner(action + " failed: " + e.message, true); }
      throw e;
    } finally {
      if (!quiet) { pending--; applyEnablement(); }
    }
  }
  function needAddr() {
    const a = addr();
    if (!ADDR_RE.test(a)) { banner("Enter or select a DUT address first (AA:BB:CC:DD:EE:FF).", true); return null; }
    return a;
  }

  // ---------------------------------------------------------------- adapter + devices
  function shortProfiles(uuids) {
    const keep = [];
    (uuids || []).forEach(function (u) {
      const m = u.match(/^(.*) \(([0-9a-f]{8})\)$/i);
      if (m && m[1] !== "PnP" && m[1] !== "GAP" && m[1] !== "GATT") keep.push(m[1]);
    });
    return keep.join(", ");
  }

  function renderAdapter(a) {
    if (!a) { $("c_adapter").textContent = "-"; return; }
    $("c_adapter").textContent = a.addr + (a.powered ? "  powered" : "  OFF") + (a.discovering ? "  scanning" : "");
  }

  let devSig = "";
  function renderDevices(list) {
    lastDevices = list || [];
    const sig = JSON.stringify(lastDevices) + "|" + addr();
    if (sig === devSig) return;
    devSig = sig;
    const tb = document.querySelector("#c_devTable tbody");
    tb.innerHTML = "";
    if (!lastDevices.length) {
      const tr = el("tr"); const td = el("td", null, "No Classic devices known on this radio yet - run Scan with the DUT in pairing/discoverable mode.");
      td.colSpan = 8; tr.appendChild(td); tb.appendChild(tr); return;
    }
    lastDevices.forEach(function (d) {
      const tr = el("tr", d.addr === addr() ? "running" : (d.connected ? "running" : null));
      tr.appendChild(el("td", null, d.name || "(unnamed)"));
      tr.appendChild(el("td", null, d.addr));
      tr.appendChild(el("td", null, (d.icon || "") + (d.class ? " " + d.class : "")));
      tr.appendChild(el("td", "num", fmt(d.rssi)));
      tr.appendChild(el("td", null, d.paired ? "yes" : "no"));
      tr.appendChild(el("td", null, d.connected ? "yes" : "no"));
      tr.appendChild(el("td", "wrap", shortProfiles(d.uuids)));
      const td = el("td");
      function btn(label, fn, needsIdle) {
        const b = el("button", "mini-btn", label);
        if (needsIdle) b.dataset.needsIdle = "1";
        b.addEventListener("click", fn);
        td.appendChild(b); td.appendChild(document.createTextNode(" "));
      }
      btn("Select", function () { $("c_addr").value = d.addr; renderDevices(lastDevices); refreshInfo(true); });
      tr.appendChild(td);
      tb.appendChild(tr);
    });
    applyEnablement();
  }

  async function loadDevices(scan) {
    const h = dev();
    if (h === null || !isReserved(h)) { renderDevices([]); renderAdapter(null); return; }
    try {
      let r;
      if (scan) {
        consoleLine("[*] scanning (BR/EDR inquiry) on " + BF.label(h) + " ...", null);
        r = await act("scan", { duration: num("c_scan_dur") || 12 });
      } else {
        r = await act("devices", {}, true);
      }
      renderAdapter(r.adapter);
      renderDevices(r.devices);
      if (scan) consoleLine("[+] scan finished: " + r.devices.length + " device(s)", null);
    } catch (e) { /* already reported by act() for scans; silent for passive list */ }
  }

  // ---------------------------------------------------------------- DUT info
  function setText(id, v) { const n = $(id); if (n.textContent !== String(v)) n.textContent = v; }
  function renderInfo(d) {
    const t = d.transport || {};
    const link = d.link || {};
    setText("c_d_conn", d.connected ? "connected" + (d.paired ? " (paired)" : "") : (d.paired ? "paired, not connected" : "not paired"));
    setText("c_d_state", t.state || "-");
    let codec = t.codec || "-";
    if (t.sbc && t.sbc.bitrate_kbps_max) codec += " (" + t.sbc.freq / 1000 + " kHz " + t.sbc.mode + ", bitpool " + t.sbc.bitpool_min + "-" + t.sbc.bitpool_max + ", <= " + t.sbc.bitrate_kbps_max + " kbit/s)";
    setText("c_d_codec", codec);
    setText("c_d_rssi", fmt(link.rssi, " dB (offset)"));
    setText("c_d_lq", fmt(link.lq, " / 255"));
    setText("c_d_txp", fmt(link.tx_power, " dBm"));
    setText("c_d_vol", t.volume !== undefined && t.volume !== null ? t.volume + " / 127" : "n/a");
    setText("c_d_bat", d.battery !== undefined && d.battery !== null ? d.battery + " %" : "n/a");
    setText("c_d_prof", d.audio_card ? d.audio_card.active_profile : "-");
    setText("c_d_uuids", shortProfiles(d.uuids) || "-");
  }
  async function refreshInfo(force) {
    const h = dev();
    const a = addr();
    if (h === null || !isReserved(h) || !ADDR_RE.test(a)) return;
    if (!force && (pending > 0 || document.hidden)) return;
    try {
      const r = await act("info", { addr: a }, true);
      renderInfo(r.device);
    } catch (e) {
      ["c_d_conn", "c_d_state", "c_d_codec", "c_d_rssi", "c_d_lq", "c_d_txp", "c_d_vol", "c_d_bat", "c_d_prof", "c_d_uuids"]
        .forEach(function (id) { setText(id, "-"); });
      setText("c_d_conn", /not known/.test(e.message) ? "unknown to this radio (scan first)" : "-");
    }
  }
  function startInfoTimer() {
    if (infoTimer) clearInterval(infoTimer);
    infoTimer = setInterval(function () {
      if ($("tab-classic").classList.contains("show") && $("c_autoinfo").checked) refreshInfo(false);
    }, 5000);
  }

  // ---------------------------------------------------------------- start / stop tests
  async function startJob(tool, params) {
    const h = dev();
    const a = needAddr();
    if (a === null || h === null) return;
    params.addr = a;
    try {
      const r = await post("/api/start", { tool: tool, dev: h, params: params });
      consoleLine("[*] started " + JOB_LABEL[tool] + " as " + r.job_id, h);
      banner(null);
    } catch (e) {
      consoleLine("[x] could not start " + JOB_LABEL[tool] + ": " + e.message, h);
      banner("Could not start " + JOB_LABEL[tool] + ": " + e.message, true);
    }
  }

  // ---------------------------------------------------------------- results box
  // The delay figures say what they are: link delay is host-observed delivery time of one audio packet, not the audio latency a
  // listener hears, and anything the sink only reports is labelled as reported.
  function delayLines(sm, li) {
    const ld = sm.link_delay;
    if (ld && ld.available) {
      const d = ld.delay_ms, j = ld.jitter_ms;
      li("link delay of " + d.n + " audio packets (ms) min/avg/p50/p95/p99/max " + [d.min, d.avg, d.p50, d.p95, d.p99, d.max].join(" / ") +
        (j ? "; jitter (mean difference between consecutive packets) " + j.avg + " ms, p95 " + j.p95 + " ms, max " + j.max + " ms" : "") +
        (ld.late ? "; " + ld.late.n + " packet(s) over " + ld.late.limit_ms + " ms" : "") +
        (ld.flush_events ? "; " + ld.flush_events + " flush event(s) (the controller dropped audio data)" : ""));
      li("  how: first ACL fragment handed to the controller until the controller reports the last fragment completed, kernel timestamps (" +
        (ld.clock || "?") + (ld.valid ? "" : ", NOT TRUSTWORTHY: " + (ld.notes || []).join("; ")) + "). This is the link, not the audio latency a listener hears: the host's audio buffer (" +
        (sm.stream_latency_setting_ms != null ? sm.stream_latency_setting_ms + " ms requested" : "?") + ") and the sink's own buffer and decoder are not included.");
    } else if (ld) {
      li("link delay: not measured (" + (ld.reason || "no data") + ")");
    }
    const cx = ld && ld.coexistence;                                   // what else the radio did: it shares its time with the Classic link
    if (cx) {
      (cx.warnings || []).forEach(function (w) { li("WARNING: " + w); });
      li("other use of the radio during the run (share of the run): LE scan " + cx.le_scan.enabled_pct + " %, LE advertising " + cx.le_adv.enabled_pct +
        " %, LE connection attempt " + cx.le_connect.enabled_pct + " %, inquiry " + cx.inquiry.enabled_pct + " %");
    }
    const lb = sm.loopback;
    if (lb) {
      li("loopback (internal receiver, both radios of this unit): " + lb.matched + " of " + (lb.matched + lb.lost) + " audio packets received, " + lb.lost + " lost (" + lb.loss_pct +
        " %)" + (lb.delay_ms ? ", true one-way delay avg/p95/max " + lb.delay_ms.avg + " / " + lb.delay_ms.p95 + " / " + lb.delay_ms.max + " ms" : "") +
        (lb.jitter_ms ? ", jitter " + lb.jitter_ms.avg + " ms" : "") + (lb.duplicates ? ", " + lb.duplicates + " duplicate(s)" : "") + (lb.reordered ? ", " + lb.reordered + " out of order" : ""));
      if (lb.completion_minus_arrival_ms) li("  check: the sender-side delay reads " + lb.completion_minus_arrival_ms.p50 + " ms (median) from the receiver's own timestamp of the same packet, range " +
        lb.completion_minus_arrival_ms.min + " to " + lb.completion_minus_arrival_ms.max + " ms");
    }
    const sc = sm.stream_control;                                      // what the two ends said to each other about starting and stopping the stream
    if (sc && sc.counts) {
      const names = Object.keys(sc.counts).sort().map(function (k) { return k.replace(/_/g, " ") + " x" + sc.counts[k]; });
      const sus = (sc.events || []).filter(function (e) { return e.from === "remote" && e.signal === "SUSPEND"; }).map(function (e) { return e.t_s + " s"; });
      li((sc.counts.remote_suspend ? "WARNING: the receiving device suspended the stream (AVDTP SUSPEND, at " + sus.join(", ") + (sc.events_total > (sc.events || []).length ? ", ..." : "") + "). " : "") +
        "stream control seen on the air: " + names.join(", ") +
        (sc.suspended_pct !== undefined && sc.counts.remote_suspend ? "; the other end held the stream suspended for " + sc.suspended_s + " s of " + sc.stream_window_s +
          " s (" + sc.suspended_pct + " %), which is what the active share above is based on (state sampled during the run: " + sm.active_pct_sampled + " %)" : ""));
    }
    if (sm.pulse_sink_resumes) li("PulseAudio suspended the Bluetooth sink " + sm.pulse_sink_resumes + " time(s) and it was resumed each time. With an internal receiver on PulseAudio the receiving radio's device is not an audio device for PulseAudio, so the receiver suspends the stream about every 5 s; use PipeWire, or a speaker as the receiver, to avoid the gaps.");
    if (sm.sink_reported_delay_ms) li("delay reported by the sink itself (AVDTP delay report, not measured): " + sm.sink_reported_delay_ms.last + " ms" +
      (sm.sink_reported_delay_ms.min !== sm.sink_reported_delay_ms.max ? " (range " + sm.sink_reported_delay_ms.min + " to " + sm.sink_reported_delay_ms.max + ")" : ""));
    else if (sm.test === "a2dp") li("the sink did not report its own delay (AVDTP delay reporting not offered)");
  }

  function showResult(sm) {
    const box = $("c_result");
    box.className = "c-result " + (sm.pass ? "pass" : "fail");
    box.innerHTML = "";
    const isLoad = sm.test === "load" || sm.test === "traffic";
    box.appendChild(el("h4", null, (sm.pass ? (isLoad ? "OK" : "PASS") : (isLoad ? "FLAGGED" : "FAIL")) + " - " + (JOB_LABEL["c_" + String(sm.test).replace("-", "_")] || sm.test) +
      " on " + sm.addr + " (" + (typeof sm.hci === "number" ? BF.label(sm.hci) : "hci" + sm.hci) + ")"));
    const ul = el("ul");
    (sm.fail_reasons || []).forEach(function (r) { ul.appendChild(el("li", null, r)); });
    function li(t) { ul.appendChild(el("li", null, t)); }
    if (sm.test === "a2dp" || sm.test === "load") {
      li("duration " + sm.duration_s + " s, codec " + (sm.codec || "?") + ", active " + sm.active_pct + " %, xruns " + sm.xruns +
        ", disconnects " + sm.disconnects + (sm.reconnect_ms && sm.reconnect_ms.length ? ", reconnects " + sm.reconnect_ms.join("/") + " ms" : ""));
      if (sm.rssi) li("RSSI offset min/avg/max " + sm.rssi.min + " / " + sm.rssi.avg + " / " + sm.rssi.max + " dB (BR/EDR Read RSSI: 0 = inside the golden receive range, not dBm)");
      if (sm.tx_kbps) li("ACL TX throughput min/avg/max " + sm.tx_kbps.min + " / " + sm.tx_kbps.avg + " / " + sm.tx_kbps.max + " kbit/s (" + (sm.throughput_src || "?") + ")");
      if (sm.acl_total) li("exact totals over " + sm.acl_total.seconds + " s: sent " + sm.acl_total.tx_bytes + " B, controller-completed " +
        sm.acl_total.done_bytes + " B, received " + sm.acl_total.rx_bytes + " B  =>  average " + sm.acl_total.avg_tx_kbps + " kbit/s TX");
      if (sm.afh) li("Classic hop channels in use (AFH): " + (sm.afh.enabled ? sm.afh.channels_min + " to " + sm.afh.channels_max + " of 79" : "AFH off, all 79") + " (" + sm.afh.reads + " reads)");
      delayLines(sm, li);
      if (sm.avrcp_keys && Object.keys(sm.avrcp_keys).length) li("AVRCP keys seen: " + JSON.stringify(sm.avrcp_keys));
    } else if (sm.test === "traffic") {
      li(sm.kind_label + ": links up " + sm.active_pct + " % of " + sm.duration_s + " s, delivered " + sm.delivered_bytes + " B, average " +
        (sm.app_kbps ? sm.app_kbps.avg : "?") + " kbit/s (busy " + sm.busy_pct + " % of the time), drops " + sm.disconnects +
        (sm.reconnects ? ", reconnects " + sm.reconnects : ""));
      (sm.streams || []).forEach(function (st) {
        const bits = [];
        if (st.files !== undefined) bits.push(st.files + " file(s)" + (st.file_kbps_avg ? ", avg " + st.file_kbps_avg + " kbit/s each" : ""));
        if (st.segments !== undefined) bits.push(st.segments + " segment(s)" + (st.late_segments ? ", " + st.late_segments + " late" : ""));
        li("  " + st.name + " (" + st.proto + ", " + (st.direction === "up" ? "sent" : "received") + " by the radio): " + st.rx_bytes + " B delivered" + (bits.length ? ", " + bits.join(", ") : ""));
      });
      if (sm.acl_kbps) li("ACL traffic, both directions, min/avg/max " + sm.acl_kbps.min + " / " + sm.acl_kbps.avg + " / " + sm.acl_kbps.max + " kbit/s (" + (sm.throughput_src || "?") + ")");
      if (sm.rssi) li("RSSI offset min/avg/max " + sm.rssi.min + " / " + sm.rssi.avg + " / " + sm.rssi.max + " dB");
      if (sm.afh) li("Classic hop channels in use (AFH): " + (sm.afh.enabled ? sm.afh.channels_min + " to " + sm.afh.channels_max + " of 79" : "AFH off, all 79") + " (" + sm.afh.reads + " reads)");
    } else if (sm.test === "hfp") {
      li("scenario " + sm.scenario + ": RFCOMM ch " + sm.rfcomm_channel + " in " + sm.rfcomm_ms + " ms, service link " + (sm.slc_ok ? "UP" : "FAILED") +
        (sm.sco_ms !== null && sm.sco_ms !== undefined ? ", SCO up in " + sm.sco_ms + " ms" : ""));
      if (sm.scenario !== "slc") li("audio over SCO: sent " + sm.sco_tx_bytes + " B (" + fmt(sm.sco_tx_pct_of_expected, " % of expected") + "), received " + sm.sco_rx_bytes + " B; DUT answered: " + (sm.dut_answered ? "yes" : "no"));
      if (sm.rssi) li("RSSI offset min/avg/max " + sm.rssi.min + " / " + sm.rssi.avg + " / " + sm.rssi.max + " dB");
    } else if (sm.test === "cycle") {
      li(sm.ok + "/" + sm.cycles + " cycles OK (" + sm.success_pct + " %)");
      if (sm.connect_ms) li("connect ms min/avg/p95/max " + sm.connect_ms.min + " / " + sm.connect_ms.avg + " / " + sm.connect_ms.p95 + " / " + sm.connect_ms.max);
      if (sm.transport_ms) li("A2DP transport ready ms min/avg/p95/max " + sm.transport_ms.min + " / " + sm.transport_ms.avg + " / " + sm.transport_ms.p95 + " / " + sm.transport_ms.max);
    } else if (sm.test === "avrcp-watch") {
      li("keys seen: " + JSON.stringify(sm.keys) + (sm.volume_changes && sm.volume_changes.length ? "; volume changes " + sm.volume_changes.length : ""));
    }
    if (sm.saved_as) li("saved: " + sm.saved_as + ".json / .csv");
    box.appendChild(ul);
  }

  function showVolumeResult(r) {
    showResult({ pass: r.pass, test: "avrcp-volume", addr: addr(), hci: dev(), fail_reasons: [] });
    const ul = $("c_result").querySelector("ul");
    r.steps.forEach(function (s) {
      ul.appendChild(el("li", null, "set " + s.set + " -> read-back " + s.readback + (s.ok ? "  sent, accepted by BlueZ (" : "  NOT accepted after ") + s.ms + " ms" + (s.ok ? ")" : "")));
    });
    ul.appendChild(el("li", null, "Command path only: BlueZ does not expose a DUT-side confirmation - verify the level change on the DUT."));
  }

  // ---------------------------------------------------------------- saved results table
  async function loadResults() {
    let r;
    try { r = await get("/api/classic/results"); } catch (e) { return; }
    const tb = document.querySelector("#c_resTable tbody");
    tb.innerHTML = "";
    if (!r.results.length) {
      const tr = el("tr"); const td = el("td", null, "No saved Classic results yet."); td.colSpan = 8; tr.appendChild(td); tb.appendChild(tr); return;
    }
    r.results.forEach(function (x) {
      const tr = el("tr", x.pass ? "pass" : "fail");
      tr.appendChild(el("td", null, new Date(x.mtime * 1000).toLocaleString()));
      tr.appendChild(el("td", null, x.test || "-"));
      tr.appendChild(el("td", null, x.addr || "-"));
      tr.appendChild(el("td", null, x.hci === null || x.hci === undefined ? "-" : BF.label(x.hci)));
      tr.appendChild(el("td", null, x.pass ? "PASS" : "FAIL"));
      tr.appendChild(el("td", "num", x.duration_s != null ? x.duration_s + " s" : "-"));
      const sm = x.summary || {};
      let detail = (x.fail_reasons || []).join("; ");
      if (!detail) {
        if (sm.test === "a2dp") detail = (sm.codec || "?") + ", active " + sm.active_pct + " %, xruns " + sm.xruns +
          (sm.link_delay && sm.link_delay.available ? ", link delay p50/p99 " + sm.link_delay.delay_ms.p50 + "/" + sm.link_delay.delay_ms.p99 + " ms" +
            (sm.link_delay.jitter_ms ? ", jitter " + sm.link_delay.jitter_ms.avg + " ms" : "") : "");
        else if (sm.test === "load") detail = "interference stream: " + (sm.codec || "?") + ", streaming " + sm.active_pct + " %, avg " + (sm.tx_kbps ? sm.tx_kbps.avg : "?") + " kbit/s";
        else if (sm.test === "traffic") detail = (sm.kind_label || sm.kind) + ", links up " + sm.active_pct + " %, avg " + (sm.app_kbps ? sm.app_kbps.avg : "?") + " kbit/s delivered";
        else if (sm.test === "cycle") detail = sm.ok + "/" + sm.cycles + " cycles";
        else if (sm.test === "hfp") detail = "SLC " + (sm.slc_ok ? "up" : "failed") + (sm.sco_ms != null ? ", SCO " + sm.sco_ms + " ms" : "");
        else if (sm.test === "avrcp-watch") detail = "keys " + Object.keys(sm.keys || {}).join(",");
      }
      tr.appendChild(el("td", "wrap", detail));
      const td = el("td");
      [["json", x.file], ["csv", x.csv]].forEach(function (p) {
        if (!p[1]) return;
        const a = el("a", "lf-link", p[0]);
        a.href = "/api/classic/result/" + encodeURIComponent(p[1]);
        td.appendChild(a); td.appendChild(document.createTextNode(" "));
      });
      tr.appendChild(td);
      tb.appendChild(tr);
    });
  }

  // ---------------------------------------------------------------- jobs table
  let jobsSig = "";
  function renderJobs() {
    const tb = document.querySelector("#c_jobsTable tbody");
    const running = (state().running || []).filter(function (j) { return j.kind.indexOf("c_") === 0; });
    // uptime ticks every refresh; compare ids only so the table (and its Stop
    // buttons) is not rebuilt under the user's cursor
    const sig = running.map(function (j) { return j.id; }).join(",");
    if (sig === jobsSig) {
      running.forEach(function (j, i) { const t = tb.rows[i] && tb.rows[i].cells[3]; if (t && j.uptime != null) t.textContent = j.uptime.toFixed(1) + "s"; });
      return;
    }
    jobsSig = sig;
    tb.innerHTML = "";
    running.forEach(function (j) {
      const tr = el("tr", "running");
      tr.appendChild(el("td", null, j.id));
      tr.appendChild(el("td", null, BF.label(j.dev)));
      tr.appendChild(el("td", null, JOB_LABEL[j.kind] || j.kind));
      tr.appendChild(el("td", "num", j.uptime != null ? j.uptime.toFixed(1) + "s" : "-"));
      const td = el("td");
      const b = el("button", "mini-btn", "Stop");
      b.addEventListener("click", function () {
        post("/api/stop", { job_id: j.id }).catch(function (e) { banner("Stop failed: " + e.message, true); });
      });
      td.appendChild(b); tr.appendChild(td); tb.appendChild(tr);
    });
  }

  // ---------------------------------------------------------------- live metrics + charts
  function resetLive(jobLabel) {
    live.rssi = []; live.kbps = []; live.done = []; live.rx = []; live.sum = { kbps: 0, done: 0, rx: 0 }; live.n = 0; live.active = 0; live.total = 0; live.dc = 0; live.keys = 0; live.job = jobLabel;
    live.dAvg = []; live.dP95 = []; live.dMax = []; live.jit = []; live.lost = 0; live.lb = false;
    $("c_l_lb_box").classList.add("hidden");
    setText("c_l_job", jobLabel || "idle");
    ["c_l_t", "c_l_state", "c_l_rssi", "c_l_lq", "c_l_kbps", "c_l_xr", "c_l_dly", "c_l_jit", "c_l_lb", "c_l_act"].forEach(function (id) { setText(id, "-"); });
    setText("c_l_dc", "0"); setText("c_l_keys", "0");
    setText("c_ch_kbps_cap", ACL_CAP);
    drawAll();
  }
  function onSample(s) {
    if (s.acl_kbps != null) s = Object.assign({}, s, { tx_kbps: s.acl_kbps, done_kbps: s.acl_kbps, rx_kbps: 0 });   // data traffic: both directions
    if (s.connect_ms !== undefined || s.error !== undefined) {           // connect-cycle row
      setText("c_l_state", "cycle " + s.n + ": " + (s.ok ? "ok" : "FAILED"));
      setText("c_l_t", s.t + " s");
      if (s.rssi !== null && s.rssi !== undefined) { live.rssi.push(s.rssi); setText("c_l_rssi", s.rssi + " dB"); }
      drawAll();
      return;
    }
    if (s.sco_tx_Bps !== undefined) {                                  // HFP voice sample: SCO byte rates only
      const tx = s.sco_tx_Bps * 8 / 1000, rx = s.sco_rx_Bps * 8 / 1000;
      setText("c_l_t", s.t.toFixed(0) + " s");
      setText("c_l_state", "SCO voice");
      setText("c_l_rssi", fmt(s.rssi, " dB"));
      setText("c_l_lq", fmt(s.lq, " / 255"));
      setText("c_l_kbps", tx.toFixed(1) + " TX / " + rx.toFixed(1) + " RX kbit/s (SCO)");
      setText("c_l_act", "n/a (A2DP only)");
      setText("c_ch_kbps_cap", "SCO voice throughput (kbit/s) - TX / RX");
      if (s.rssi !== null && s.rssi !== undefined) live.rssi.push(s.rssi);
      live.kbps.push(tx); live.rx.push(rx);
      live.n++; live.sum.kbps += tx; live.sum.rx += rx;
      [live.rssi, live.kbps, live.rx].forEach(function (a) { if (a.length > MAX_POINTS) a.shift(); });
      drawAll();
      return;
    }
    setText("c_ch_kbps_cap", ACL_CAP);
    setText("c_l_t", s.t.toFixed(0) + " s");
    setText("c_l_state", s.state || "-");
    setText("c_l_rssi", fmt(s.rssi, " dB"));
    setText("c_l_lq", fmt(s.lq, " / 255"));
    setText("c_l_kbps", s.tx_kbps == null ? "-" : s.tx_kbps.toFixed(1) + " kbit/s" +
      (s.done_kbps != null && Math.abs(s.done_kbps - s.tx_kbps) > 0.5 ? " (completed " + s.done_kbps.toFixed(1) + ")" : ""));
    setText("c_l_xr", fmt(s.xruns));
    if (s.delay_n !== undefined) {                                     // A2DP rows carry the per-interval link delay (absent in older results)
      if (s.delay_n > 0 && s.delay_avg_ms != null) {
        setText("c_l_dly", s.delay_avg_ms.toFixed(1) + " / " + s.delay_p95_ms.toFixed(1) + " / " + s.delay_max_ms.toFixed(1) + " ms");
        setText("c_l_jit", s.jitter_ms == null ? "-" : s.jitter_ms.toFixed(2) + " ms");
        live.dAvg.push(s.delay_avg_ms); live.dP95.push(s.delay_p95_ms); live.dMax.push(s.delay_max_ms);
        if (s.jitter_ms != null) live.jit.push(s.jitter_ms);
      } else {
        setText("c_l_dly", "no audio packets in this interval");
        setText("c_l_jit", "-");
      }
    }
    if (s.lb_lost !== undefined) {                                     // internal receiver: packets the other radio never received
      live.lb = true; live.lost += s.lb_lost;
      $("c_l_lb_box").classList.remove("hidden");
      setText("c_l_lb", String(live.lost));
    }
    live.total++;
    if (s.state === "active") live.active++;
    setText("c_l_act", live.total ? (100 * live.active / live.total).toFixed(1) + " %" : "-");
    if (s.rssi !== null && s.rssi !== undefined) live.rssi.push(s.rssi);
    if (s.tx_kbps !== null && s.tx_kbps !== undefined) {
      live.kbps.push(s.tx_kbps);
      live.done.push(s.done_kbps == null ? s.tx_kbps : s.done_kbps);
      live.rx.push(s.rx_kbps == null ? 0 : s.rx_kbps);
      // whole-run averages: the arrays above are capped at MAX_POINTS for drawing, so average separately
      live.n++;
      live.sum.kbps += s.tx_kbps;
      live.sum.done += (s.done_kbps == null ? s.tx_kbps : s.done_kbps);
      live.sum.rx += (s.rx_kbps == null ? 0 : s.rx_kbps);
    }
    [live.rssi, live.kbps, live.done, live.rx, live.dAvg, live.dP95, live.dMax, live.jit].forEach(function (a) { if (a.length > MAX_POINTS) a.shift(); });
    drawAll();
  }
  function onEvent(e) {
    if (e.event === "disconnect") { live.dc++; setText("c_l_dc", String(live.dc)); }
    if (e.event === "avrcp_key") { live.keys++; setText("c_l_keys", String(live.keys)); }
    const skip = { ready: 1 };
    const extra = Object.keys(e).filter(function (k) { return k !== "m" && k !== "event" && k !== "t"; })
      .map(function (k) { return k + "=" + e[k]; }).join(" ");
    consoleLine("[!] +" + e.t + "s " + e.event + (extra && !skip[e.event] ? " " + extra : ""), null, e.event === "disconnect" ? "r" : "y");
  }

  // series: [{ data: [...], color, label }]; opts.zero pins the axis at 0 (throughput must not be auto-zoomed
  // into a flat line that looks like a swing)
  function drawChart(cv, series, unit, opts) {
    opts = opts || {};
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || 520, h = cv.clientHeight || 130;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const g = cv.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    g.font = "10px sans-serif";
    const all = [].concat.apply([], series.map(function (s) { return s.data; }));
    if (!all.length) { g.fillStyle = "#99a"; g.fillText("no data yet", 8, 16); return; }
    let lo = opts.zero ? 0 : Math.min.apply(null, all), hi = Math.max.apply(null, all);
    if (opts.zero) { hi = hi <= 0 ? 1 : hi * 1.1; }
    else if (hi - lo < 4) { const mid = (hi + lo) / 2; lo = mid - 2; hi = mid + 2; }
    const pad = { l: 44, r: 8, t: 8, b: 16 };
    const n = Math.max.apply(null, series.map(function (s) { return s.data.length; }));
    const X = function (i) { return pad.l + (n <= 1 ? 0 : i * (w - pad.l - pad.r) / (n - 1)); };
    const Y = function (v) { return pad.t + (hi - v) * (h - pad.t - pad.b) / (hi - lo); };
    g.strokeStyle = "#dde3ea"; g.fillStyle = "#678"; g.lineWidth = 1;
    [lo, (lo + hi) / 2, hi].forEach(function (v) {
      g.beginPath(); g.moveTo(pad.l, Y(v)); g.lineTo(w - pad.r, Y(v)); g.stroke();
      g.fillText(v.toFixed(Math.abs(hi - lo) < 20 ? 1 : 0), 2, Y(v) + 3);
    });
    series.forEach(function (s, si) {
      if (!s.data.length) return;
      g.strokeStyle = s.color; g.lineWidth = si === 0 ? 1.8 : 1.2; g.beginPath();
      s.data.forEach(function (v, i) { if (i === 0) g.moveTo(X(i), Y(v)); else g.lineTo(X(i), Y(v)); });
      g.stroke();
    });
    // legend with current value and run average, per series
    let lx = pad.l;
    series.forEach(function (s) {
      if (!s.data.length) return;
      const cur = s.data[s.data.length - 1];
      const avg = s.avg != null ? s.avg : s.data.reduce(function (a, b) { return a + b; }, 0) / s.data.length;
      const t = (s.label ? s.label + " " : "") + cur.toFixed(1) + " " + unit + (opts.avg ? " (run avg " + avg.toFixed(1) + ")" : "");
      g.fillStyle = s.color; g.fillRect(lx, h - 11, 8, 3);
      g.fillText(t, lx + 11, h - 5);
      lx += 11 + g.measureText(t).width + 12;
    });
  }
  function drawAll() {
    drawChart($("c_ch_rssi"), [{ data: live.rssi, color: "#1f5fbf" }], "dB", {});
    drawChart($("c_ch_kbps"), [
      { data: live.kbps, color: "#0a8a3a", label: "TX", avg: live.n ? live.sum.kbps / live.n : null },
      { data: live.done, color: "#d9822b", label: "completed", avg: live.n ? live.sum.done / live.n : null },
      { data: live.rx, color: "#7a4fbf", label: "RX", avg: live.n ? live.sum.rx / live.n : null }], "kbit/s", { zero: true, avg: true });
    drawChart($("c_ch_dly"), [
      { data: live.dAvg, color: "#0a8a3a", label: "avg" },
      { data: live.dP95, color: "#d9822b", label: "p95" },
      { data: live.dMax, color: "#c0392b", label: "max" }], "ms", { zero: true });
    drawChart($("c_ch_jit"), [{ data: live.jit, color: "#7a4fbf", label: "jitter" }], "ms", { zero: true });
  }

  // ---------------------------------------------------------------- SSE hook
  BF.hooks.push(function (evt, kind, dev_) {
    if (evt.type === "render") { populateDev(); applyEnablement(); renderJobs(); return; }
    if (evt.type === "status" && evt.job && evt.job.kind.indexOf("c_") === 0) {
      if (evt.event === "started") { $("c_result").classList.add("hidden"); resetLive(JOB_LABEL[evt.job.kind] + " (" + evt.job.id + ")"); }
      if (evt.event === "finished") {
        consoleLine("[*] " + JOB_LABEL[evt.job.kind] + " " + evt.job.id + " finished (exit " + evt.job.exit_code + ")", evt.job.dev);
        setText("c_l_job", "idle (last: " + evt.job.id + ")");
        loadResults();
        refreshInfo(true);
      }
      return;
    }
    if (evt.type !== "line") return;
    const text = evt.text || "";
    if (text.charAt(0) === "{") {
      let o;
      try { o = JSON.parse(text); } catch (e) { return; }
      if (o.m === "sample") onSample(o);
      else if (o.m === "event") onEvent(o);
      else if (o.m === "summary") { showResult(o); $("c_result").classList.remove("hidden"); }
      return;
    }
    consoleLine(text, dev_);
  });

  // ---------------------------------------------------------------- wiring
  function wire() {
    $("c_dev").addEventListener("change", function () { applyEnablement(); loadDevices(false); refreshInfo(true); });
    $("c_reserve").addEventListener("click", async function () {
      const h = dev();
      if (h === null) return;
      const on = !isReserved(h);
      try {
        const r = await post("/api/classic/reserve", { dev: h, on: on });
        consoleLine("[+] " + r.msg, h);
        await BF.refresh();
        if (on) { await loadPreflight(); loadDevices(false); }
      } catch (e) { consoleLine("[x] " + e.message, h); banner(e.message, true); }
    });
    $("c_scan").addEventListener("click", function () { loadDevices(true); });
    $("c_list").addEventListener("click", function () { loadDevices(false); });
    $("c_addr").addEventListener("input", function () { renderDevices(lastDevices); });
    $("c_addr").addEventListener("change", function () { refreshInfo(true); });

    $("c_pair").addEventListener("click", async function () {
      const a = needAddr(); if (!a) return;
      consoleLine("[*] pairing " + a + " ...", dev());
      try { await act("pair", { addr: a, pin: num("c_pin") || "0000" }); consoleLine("[+] paired + trusted " + a, dev()); }
      catch (e) { return; }
      loadDevices(false); refreshInfo(true);
    });
    $("c_connect").addEventListener("click", async function () {
      const a = needAddr(); if (!a) return;
      consoleLine("[*] connecting " + a + " ...", dev());
      try {
        const r = await act("connect", { addr: a });
        consoleLine("[+] connected in " + r.connect_ms + " ms" + (r.transport_ms ? ", A2DP transport ready after " + r.transport_ms + " ms" : ""), dev());
      } catch (e) { return; }
      loadDevices(false); refreshInfo(true);
    });
    $("c_disconnect").addEventListener("click", async function () {
      const a = needAddr(); if (!a) return;
      try { await act("disconnect", { addr: a }); } catch (e) { return; }
      loadDevices(false); refreshInfo(true);
    });
    $("c_remove").addEventListener("click", async function () {
      const a = needAddr(); if (!a) return;
      if (!confirm("Forget " + a + "? This deletes its pairing from " + BF.label(dev()) + ".")) return;
      try { await act("remove", { addr: a }); } catch (e) { return; }
      loadDevices(false); refreshInfo(true);
    });
    $("c_info").addEventListener("click", function () { if (needAddr()) refreshInfo(true); });

    $("c_a2_src").addEventListener("change", function () {
      $("c_a2_file_f").classList.toggle("hidden", $("c_a2_src").value !== "file");
    });
    $("c_a2_start").addEventListener("click", function () {
      let src = $("c_a2_src").value;
      if (src === "file") {
        const p = $("c_a2_file").value.trim();
        if (!p) { banner("Enter the audio file path (on this unit).", true); return; }
        src = "file:" + p;
      }
      startJob("c_a2dp", {
        duration: num("c_a2_dur"), source: src, codec: $("c_a2_codec").value,
        min_active_pct: num("c_a2_act"), max_xruns: num("c_a2_xr"), max_disconnects: num("c_a2_dc"),
        min_rssi: num("c_a2_rssi"), max_delay_ms: num("c_a2_dly"), max_jitter_ms: num("c_a2_jit"),
        reconnect: $("c_a2_reconnect").checked,
      });
    });
    $("c_vol_test").addEventListener("click", async function () {
      const a = needAddr(); if (!a) return;
      consoleLine("[*] AVRCP absolute-volume test ...", dev());
      try {
        const r = await act("avrcp-volume", { addr: a, steps: num("c_vol_steps") });
        showVolumeResult(r);
        $("c_result").classList.remove("hidden");
      } catch (e) { /* reported */ }
    });
    document.querySelectorAll("[data-plr]").forEach(function (b) {
      b.addEventListener("click", async function () {
        const a = needAddr(); if (!a) return;
        try {
          const r = await act("avrcp-player", { addr: a, cmd: b.dataset.plr });
          const p = r.player || {};
          consoleLine("[+] DUT player: " + (p.name || "?") + " status=" + (p.status || "?") +
            (p.track && p.track.Title ? " track=" + p.track.Title : ""), dev());
        } catch (e) { /* reported */ }
      });
    });
    $("c_w_start").addEventListener("click", function () {
      const keys = [...document.querySelectorAll("#c_keys input:checked")].map(function (c) { return c.value; });
      startJob("c_avrcp_watch", { duration: num("c_w_dur"), expect: keys.join(",") });
    });
    $("c_hf_start").addEventListener("click", function () {
      startJob("c_hfp", { scenario: $("c_hf_scn").value, duration: num("c_hf_dur"), number: num("c_hf_num"), ring_timeout: num("c_hf_ring") });
    });
    $("c_cy_start").addEventListener("click", function () {
      startJob("c_cycle", { count: num("c_cy_n"), gap: num("c_cy_gap"), min_success: num("c_cy_ok") });
    });

    document.querySelector('.lf-tab[data-tab="classic"]').addEventListener("click", function () {
      loadPreflight(); loadResults(); drawAll();
      if (dev() !== null && isReserved(dev())) { loadDevices(false); refreshInfo(true); }
    });
    window.addEventListener("resize", drawAll);
  }

  async function loadPreflight() {
    try {
      const pf = await get("/api/classic/preflight");
      const notes = (pf.notes || []).slice();
      if (pf.error) notes.push(pf.error);
      const perUnit = {};
      (pf.all_hci || []).forEach(function (h) { const b = Math.floor(h / 100); perUnit[b] = (perUnit[b] || 0) + 1; });
      const blocks = Object.keys(perUnit);
      if (!blocks.length) notes.push("This unit has no Bluetooth controller.");
      blocks.forEach(function (b) {
        if (perUnit[b] < 2) notes.push((blocks.length > 1 ? "Unit " + BF.unitName(b * 100) : "This unit") + " has only " + perUnit[b] +
          " Bluetooth controller(s): reserving it for Classic disables BLE jobs on it.");
      });
      // hard error only when the engine cannot run at all (PyGObject missing); otherwise a soft warning
      banner(notes.length ? "Check: " + notes.join("  |  ") : null, pf.gi === false);
    } catch (e) { banner("Could not run the Classic preflight: " + e.message, true); }
  }

  // ---------------------------------------------------------------- Interference tab: realistic Classic load (A2DP stream)
  const CL = { ids: {}, err: {}, hops: null, reconnects: 0, active: 0, total: 0, last: null, traffic: false };

  function clDev() { const v = parseInt($("cl_dev").value, 10); return isNaN(v) ? null : v; }
  function isClKind(k) { return k === "c_load" || k === "c_traffic"; }
  function clJobOn(h) { return (state().running || []).find(function (j) { return j.dev === h && isClKind(j.kind); }); }
  function clKind() { return $("cl_kind").value; }
  function clIsAudio() { return clKind() === "audio"; }
  function clMsg(text, bad) { const n = $("cl_msg"); n.textContent = text || ""; n.style.color = bad ? "#b00020" : ""; }

  function populateClDev() {
    const sel = $("cl_dev");
    const all = state().all_hci || [];
    const sig = all.join(",") + "|" + (state().classic_reserved || []).join(",") + "|" +
      (state().running || []).map(function (j) { return j.dev + j.kind; }).join(",");
    if (sel.dataset.sig === sig) return;
    sel.dataset.sig = sig;
    const prev = sel.value;
    sel.innerHTML = "";
    all.forEach(function (h) {
      const job = jobOn(h), ble = bleJobOn(h);
      let label = BF.label(h);
      if (job) label += " (" + (JOB_LABEL[job.kind] || job.kind) + " running)";
      else if (ble) label += " (BLE job running)";
      else if (isReserved(h)) label += " (Classic, reserved)";
      else label += " (will be reserved for Classic)";
      const o = el("option", null, label);
      o.value = h;
      o.disabled = !!ble;
      sel.appendChild(o);
    });
    const ok = function (v) { const o = [...sel.options].find(function (x) { return x.value === v; }); return o && !o.disabled; };
    if (prev && ok(prev)) sel.value = prev;
    else {
      const pick = (state().classic_reserved || []).find(function (h) { return ok(String(h)); });
      const usable = [...sel.options].filter(function (o) { return !o.disabled; });
      const freeHere = usable.filter(function (o) { return parseInt(o.value, 10) < 100; });
      const free = (freeHere.length ? freeHere : usable).slice(-1)[0];                    // away from hci0, on this unit first, like the Classic tab
      if (pick !== undefined) sel.value = String(pick); else if (free) sel.value = free.value;
    }
    clRenderAddr();
  }

  // Streaming-device list: this radio's paired devices, plus every OTHER radio of this unit as an internal receiver.
  function clRenderAddr(paired) {
    const list = paired === undefined ? (CL.paired || []) : paired;
    CL.paired = list;
    const h = clDev();
    const sel = $("cl_addr");
    const keep = sel.value;
    sel.innerHTML = "";
    const kind = clKind();
    const needsLocal = kind !== "audio" && kind !== "file";     // only BluForge's own receiving end can take the other data types
    (needsLocal ? [] : list).forEach(function (d) {
      const sink = (d.uuids || []).some(function (u) { return /(a2dp|audio) sink/i.test(u); });
      const o = el("option", null, (d.name || "(unnamed)") + " (" + d.addr + ")" + (sink && kind === "audio" ? " - A2DP sink" : "") + (d.connected ? " - connected" : ""));
      o.value = d.addr;
      sel.appendChild(o);
    });
    (state().all_hci || []).filter(function (x) { return x !== h && BF.sameUnit(x, h) && !bleJobOn(x) && !jobOn(x); }).forEach(function (x) {
      const o = el("option", null, "Same unit: " + BF.label(x) + " (internal receiver" + (clIsAudio() ? " - no earbuds needed)" : ")"));
      o.value = "local:" + x;
      sel.appendChild(o);
    });
    if (!sel.options.length) {
      const o = el("option", null, '(press "Reserve radio & list devices")');
      o.value = "";
      sel.appendChild(o);
    }
    if (keep && [...sel.options].some(function (o) { return o.value === keep; })) sel.value = keep;
  }

  async function clLoadDevices() {
    const h = clDev();
    if (h === null) { clMsg("No Bluetooth radio is available for Classic.", true); return; }
    clMsg("Reserving " + BF.label(h) + " for Classic and reading its devices ...");
    try {
      if (!isReserved(h)) { await post("/api/classic/reserve", { dev: h, on: true }); await BF.refresh(); }
      const r = await post("/api/classic/action", { dev: h, action: "devices", params: {} });
      const paired = (r.devices || []).filter(function (d) { return d.paired; });
      clRenderAddr(paired);
      clMsg(paired.length + " paired device(s) on " + BF.label(h) + ". Pick one, or pick another radio of the same unit as an internal receiver.");
    } catch (e) { clMsg("Could not list devices: " + e.message, true); }
  }

  function clEnable() {
    const h = clDev();
    const busy = h === null || !!jobOn(h) || !!bleJobOn(h);
    const b = $("cl_start");
    if (b.disabled !== busy) b.disabled = busy;
  }

  let clJobsSig = "";
  function renderClJobs() {
    const tb = document.querySelector("#clJobsTable tbody");
    const running = (state().running || []).filter(function (j) { return isClKind(j.kind); });
    const sig = running.map(function (j) { return j.id; }).join(",");
    if (sig === clJobsSig) {
      running.forEach(function (j, i) { const t = tb.rows[i] && tb.rows[i].cells[3]; if (t && j.uptime != null) t.textContent = j.uptime.toFixed(1) + "s"; });
      return;
    }
    clJobsSig = sig;
    tb.innerHTML = "";
    running.forEach(function (j) {
      CL.ids[j.id] = true;
      const tr = el("tr", "running");
      tr.appendChild(el("td", null, j.id));
      tr.appendChild(el("td", null, BF.label(j.dev)));
      tr.appendChild(el("td", null, j.addr || "-"));
      tr.appendChild(el("td", "num", j.uptime != null ? j.uptime.toFixed(1) + "s" : "-"));
      const td = el("td");
      const b = el("button", "mini-btn", "Stop");
      b.addEventListener("click", function () {
        post("/api/stop", { job_id: j.id }).catch(function (e) { clMsg("Stop failed: " + e.message, true); });
      });
      td.appendChild(b); tr.appendChild(td); tb.appendChild(tr);
    });
  }

  function clShowLive() {
    const l = CL.last;
    if (!l) { $("cl_live").textContent = ""; return; }
    const audio = !CL.traffic;
    $("cl_live").textContent = "Delivering: " + (l.state === "active" ? (audio ? "streaming" : "traffic flowing") : (audio ? "NOT streaming (" : "links DOWN (") + l.state + ")") +
      (audio ? "" : "  |  delivered " + (l.app == null ? "-" : l.app.toFixed(1) + " kbit/s")) +
      "  |  ACL " + (l.kbps == null ? "-" : l.kbps.toFixed(1) + " kbit/s") +
      "  |  hop channels in use " + (CL.hops == null ? "-" : CL.hops + " of 79") +
      "  |  " + (audio ? "streaming " : "links up ") + (CL.total ? (100 * CL.active / CL.total).toFixed(1) : "-") + " % of the time" +
      "  |  reconnects " + CL.reconnects;
  }

  function clShowResult(sm) {
    const parts = [];
    const traffic = sm.test === "traffic";
    parts.push((sm.pass ? "Run finished: " : "Run FLAGGED: ") + (traffic ? "links up " : "streaming ") + sm.active_pct + " % of " + sm.duration_s + " s");
    if (traffic) {
      if (sm.app_kbps) parts.push("delivered avg " + sm.app_kbps.avg + " kbit/s (max " + sm.app_kbps.max + ")");
      if (sm.acl_kbps) parts.push("ACL avg " + sm.acl_kbps.avg + " kbit/s both directions");
    } else if (sm.tx_kbps) parts.push("average ACL TX " + sm.tx_kbps.avg + " kbit/s (max " + sm.tx_kbps.max + ")");
    if (sm.afh) parts.push("hop channels " + (sm.afh.enabled ? sm.afh.channels_min + " to " + sm.afh.channels_max : "79 (AFH off)") + " of 79");
    parts.push("reconnects " + (sm.reconnects !== undefined ? sm.reconnects : (sm.reconnect_ms ? sm.reconnect_ms.length : 0)) + " (failed attempts " + (sm.reconnect_failures || 0) + ")");
    if (sm.fail_reasons && sm.fail_reasons.length) parts.push(sm.fail_reasons.join("; "));
    const n = $("cl_result");
    n.textContent = parts.join("  |  ");
    n.style.color = sm.pass ? "" : "#b00020";
  }

  function clApplyKind() {
    const audio = clIsAudio();
    document.querySelectorAll("#cl_group .cl-a").forEach(function (n) { n.classList.toggle("hidden", !audio); });
    document.querySelectorAll("#cl_group .cl-t").forEach(function (n) { n.classList.toggle("hidden", audio); });
    if (audio) $("cl_file_f").classList.toggle("hidden", $("cl_src").value !== "file");
    else $("cl_file_f").classList.add("hidden");
    const k = clKind();
    $("cl_fmb_f").classList.toggle("hidden", !(k === "file"));
    $("cl_vbr_f").classList.toggle("hidden", !(k === "video"));
    clRenderAddr();
  }

  function wireLoad() {
    $("cl_kind").addEventListener("change", clApplyKind);
    $("cl_src").addEventListener("change", function () { $("cl_file_f").classList.toggle("hidden", !clIsAudio() || $("cl_src").value !== "file"); });
    $("cl_dev").addEventListener("change", function () { clRenderAddr([]); clEnable(); });
    $("cl_refresh").addEventListener("click", clLoadDevices);
    $("cl_start").addEventListener("click", async function () {
      const h = clDev();
      let a = $("cl_addr").value;
      let local = null;
      if (h === null) { clMsg("Choose a Classic radio first.", true); return; }
      if (a.indexOf("local:") === 0) {
        local = parseInt(a.slice(6), 10);
        $("cl_start").disabled = true;
        clMsg("Preparing " + BF.label(local) + " as the internal receiver (the two radios pair with each other the first time, about 10 s) ...");
        try {
          if (!isReserved(h)) await post("/api/classic/reserve", { dev: h, on: true });
          if (!isReserved(local)) await post("/api/classic/reserve", { dev: local, on: true });
          await BF.refresh();
          const lk = await post("/api/classic/action", { dev: h, action: "link-local", params: { peer: local } });
          a = lk.addr;
        } catch (e) { clMsg("Could not prepare the internal receiver: " + e.message, true); clEnable(); return; }
        clEnable();
      }
      if (!ADDR_RE.test(a)) { clMsg('Choose a paired streaming device first (press "Reserve radio & list devices").', true); return; }
      const audio = clIsAudio();
      let tool, params;
      if (audio) {
        let src = $("cl_src").value;
        if (src === "file") {
          const p = $("cl_file").value.trim();
          if (!p) { clMsg("Enter the audio file path (on this unit).", true); return; }
          src = "file:" + p;
        }
        tool = "c_load";
        params = { addr: a, duration: $("cl_dur").value.trim(), source: src, codec: $("cl_codec").value };
      } else {
        tool = "c_traffic";
        params = { addr: a, kind: clKind(), duration: $("cl_dur").value.trim(),
                   file_mb: $("cl_fmb").value.trim(), video_kbps: $("cl_vbr").value.trim() };
      }
      try {
        const r = await post("/api/start", { tool: tool, dev: h, params: params });
        CL.traffic = !audio;
        CL.ids[r.job_id] = true;
        CL.hops = null; CL.reconnects = 0; CL.active = 0; CL.total = 0; CL.last = null;
        $("cl_result").textContent = "";
        clMsg("Started " + r.job_id + " on " + BF.label(h) + ": " + (audio ? "streaming to " : "sending " + $("cl_kind").selectedOptions[0].text + " to ") + (local !== null ? BF.label(local) + " of the same unit (" + a + "), which stays reserved for Classic until you release it on the Bluetooth Classic tab" : a) +
          ". The Bluetooth Classic tab shows the full live charts.");
      } catch (e) { clMsg("Could not start: " + e.message, true); }
    });
    BF.hooks.push(function (evt) {
      if (evt.type === "render") { populateClDev(); clEnable(); renderClJobs(); return; }
      if (evt.type === "status" && evt.job && isClKind(evt.job.kind)) {
        CL.ids[evt.job.id] = true;
        if (evt.event === "finished") {
          CL.last = null; clShowLive();
          if (evt.job.exit_code === 1 && CL.err[evt.job.id]) {
            clMsg("Job " + evt.job.id + " stopped with an error: " + CL.err[evt.job.id] + " (check the streaming device is on, in range and paired).", true);
          } else clMsg("Job " + evt.job.id + " finished (exit " + evt.job.exit_code + ").", evt.job.exit_code === 1);
        }
        return;
      }
      if (evt.type !== "line" || !CL.ids[evt.job_id]) return;
      const text = evt.text || "";
      if (text.indexOf("[x]") === 0) CL.err[evt.job_id] = text.replace(/^\[x\]\s*/, "");
      if (text.charAt(0) !== "{") return;
      let o;
      try { o = JSON.parse(text); } catch (e) { return; }
      if (o.m === "sample" && o.state !== undefined) {
        const k = o.acl_kbps !== undefined ? o.acl_kbps : o.tx_kbps;
        CL.last = { state: o.state, kbps: k === undefined ? null : k, app: o.app_kbps === undefined ? null : o.app_kbps };
        CL.total++;
        if (o.state === "active") CL.active++;
        clShowLive();
      } else if (o.m === "event") {
        if (o.event === "afh" && o.channels !== undefined) CL.hops = o.channels;
        if (o.event === "reconnected") CL.reconnects++;
      } else if (o.m === "summary" && (o.test === "load" || o.test === "traffic")) clShowResult(o);
    });
  }

  wire();
  wireLoad();
  clApplyKind();
  startInfoTimer();
  drawAll();
})();
