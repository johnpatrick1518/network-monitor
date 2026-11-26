"use strict";

const state = {
  devices: [], events: [], sort: "ip", descending: false, refreshing: false,
  deviceId: null, range: "24h", chart: null, history: null, historyVersion: 0,
  toastTimer: null, lastSuccess: null,
};

/** Find an element by its unique ID. */
function $(id) { return document.getElementById(id); }

/** Escape all externally sourced values before inserting HTML. */
function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

/** Fetch JSON with browser-managed Basic credentials and a bounded wait. */
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(45000),
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    if (response.status === 401) throw new Error("Login expired. Reload this page to sign in.");
    let detail = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail :
        (Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg).join("; ") : detail);
    } catch (_) { /* Keep the status-based message for non-JSON errors. */ }
    throw new Error(detail);
  }
  return response.json();
}

/** Display a temporary accessible notification. */
function toast(message, error = false) {
  clearTimeout(state.toastTimer);
  const target = $("device-dialog").open ? $("device-dialog") : document.body;
  if ($("toast").parentElement !== target) target.appendChild($("toast"));
  $("toast").textContent = message;
  $("toast").className = `toast${error ? " error" : ""}`;
  $("toast").hidden = false;
  state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 7000);
}

/** Format a value without representing absent data as zero. */
function number(value, suffix = "") {
  return value == null ? "—" : `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
}

/** Format elapsed time, including timestamps slightly ahead of the browser clock. */
function relativeTime(value) {
  if (!value) return "Never";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 10) return "Just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/** Pick the best available human-readable identity. */
function deviceName(device) { return device.friendly_name || device.hostname || device.ip; }

/** Render a status that distinguishes pending checks from untrusted devices. */
function statusBadge(status) {
  const label = { up: "Online", down: "Offline", unknown: "Pending" }[status] || "Pending";
  return `<span class="badge ${escapeHTML(status)}"><i class="status-dot ${escapeHTML(status)}"></i>${label}</span>`;
}

/** Update the subnet and top-level counters. */
function renderSummary(summary) {
  $("demo-banner").hidden = !summary.demo_mode;
  for (const key of ["total", "online", "offline", "unknown"]) $(key).textContent = number(summary[key]);
  $("average-latency").textContent = number(summary.average_latency_ms);
  $("pending-label").textContent = summary.pending ? `${summary.pending} awaiting first check` : "Debounced failure detection";
  $("subnet-label").textContent = summary.subnet || "No valid subnet configured";
  $("network-name").textContent = summary.wifi_ssid ? `Wi-Fi · ${summary.wifi_ssid}` :
    (summary.network_interface || "Local network");
  $("network-subnet").textContent = summary.subnet || "";
  $("wifi-signal").textContent = summary.wifi_signal_percent == null ? "" : `${summary.wifi_signal_percent}% signal`;
  $("network-name").title = summary.wifi_ssid ?
    `Connected Wi-Fi on ${summary.network_interface || "the monitoring host"}` :
    "Wi-Fi name unavailable: this may be a wired connection or OS privacy restriction.";
  $("last-scan").textContent = summary.last_scan_at ? `Last scan ${relativeTime(summary.last_scan_at)}` : "No scans completed yet";
  $("scan-button").disabled = summary.scan_running || !summary.scheduler_running;
  $("scan-button").textContent = summary.scan_running ? "⌁ Scanning…" : "⌁ Scan now";
  $("scan-error").hidden = !summary.last_scan_error;
  $("scan-error").textContent = summary.last_scan_error || "";
}

/** Keep the tag filter synchronized without discarding the current selection. */
function renderTags() {
  const selected = $("tag-filter").value;
  const tags = [...new Set(state.devices.flatMap((device) => device.tags))].sort();
  $("tag-filter").replaceChildren(new Option("All tags", ""));
  for (const tag of tags) $("tag-filter").add(new Option(tag, tag));
  if (tags.includes(selected)) $("tag-filter").value = selected;
}

/** Compare rows numerically where possible, always keeping missing values last. */
function compareDevices(left, right) {
  let a = state.sort === "name" ? deviceName(left) : left[state.sort];
  let b = state.sort === "name" ? deviceName(right) : right[state.sort];
  if (a == null || b == null) return a == null ? (b == null ? 0 : 1) : -1;
  let result;
  if (typeof a === "number" && typeof b === "number") result = a - b;
  else result = String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
  return state.descending ? -result : result;
}

/** Render filtered inventory, safely handling user names, tags, and DNS results. */
function renderDevices() {
  const search = $("search").value.trim().toLowerCase();
  const status = $("status-filter").value;
  const tag = $("tag-filter").value;
  const trust = $("trust-filter").value;
  const devices = state.devices.filter((device) => {
    const haystack = [deviceName(device), device.ip, device.mac, device.vendor, device.hostname, ...device.tags].join(" ").toLowerCase();
    return (!search || haystack.includes(search)) && (!status || device.status === status) &&
      (!tag || device.tags.includes(tag)) && (!trust || String(device.trusted) === trust);
  }).sort(compareDevices);
  $("device-count").textContent = String(devices.length);
  $("device-rows").innerHTML = devices.map((device) => {
    const trusted = device.trusted ? '<span>✓ Trusted</span>' : '<span class="unknown-mark">◇ Unknown</span>';
    const tags = device.tags.slice(0, 2).map((tagName) => `<span class="tag">${escapeHTML(tagName)}</span>`).join("");
    return `<tr tabindex="0" data-device-id="${device.id}" aria-label="View ${escapeHTML(deviceName(device))}">
      <td><div class="device-cell"><span class="device-avatar" aria-hidden="true">${device.tags.includes("phone") ? "▯" : device.tags.includes("server") ? "▤" : "▣"}</span><div>
        <div class="device-name">${escapeHTML(deviceName(device))}</div><div class="device-sub">${trusted}${tags}</div>
      </div></div></td>
      <td class="mono">${escapeHTML(device.ip)}</td>
      <td><span class="mono">${escapeHTML(device.mac || "Unavailable")}</span><div class="vendor" title="${escapeHTML(device.vendor || "")}">${escapeHTML(device.vendor || "Vendor unavailable")}</div></td>
      <td>${statusBadge(device.status)}</td>
      <td class="mono">${number(device.latency_ms, " ms")}</td>
      <td class="mono ${device.uptime_24h >= 99 ? "uptime" : "muted"}">${number(device.uptime_24h, "%")}</td>
      <td class="muted" title="${escapeHTML(new Date(device.last_seen).toLocaleString())}">${relativeTime(device.last_seen)}</td>
    </tr>`;
  }).join("");
  $("empty-state").hidden = devices.length > 0;
  if (!devices.length) {
    $("empty-state").querySelector("h3").textContent = state.devices.length ? "No matching devices" : "No devices discovered yet";
    $("empty-state").querySelector("p").textContent = state.devices.length ?
      "Try a different search or clear your filters." : "A scan starts automatically. Check any discovery error above, or select Scan now.";
  }
  document.querySelectorAll("th[data-active]").forEach((th) => { th.removeAttribute("aria-sort"); th.removeAttribute("data-active"); });
  const active = document.querySelector(`[data-sort="${state.sort}"]`);
  if (active) {
    active.parentElement.setAttribute("aria-sort", state.descending ? "descending" : "ascending");
    active.parentElement.dataset.active = "true";
  }
}

/** Render recent events with delivery diagnostics available on hover. */
function renderEvents() {
  const titles = { new_device: "New device discovered", device_down: "Device went offline", device_up: "Device back online", scan_completed: "Network scan complete", scan_failed: "Discovery needs attention" };
  const icons = { new_device: "+", device_down: "↓", device_up: "↑", scan_completed: "✓", scan_failed: "!" };
  $("event-list").innerHTML = state.events.length ? state.events.map((event) => `
    <article class="event ${escapeHTML(event.kind)}">
      <span class="event-icon" aria-hidden="true">${icons[event.kind] || "·"}</span>
      <div><div class="event-title">${escapeHTML(titles[event.kind] || event.kind)}</div>
      <p class="event-message">${escapeHTML(event.message)}</p>
      <time class="event-time" datetime="${escapeHTML(event.created_at)}" title="${escapeHTML(new Date(event.created_at).toLocaleString())} · notification: ${escapeHTML(event.notification_status)}">${relativeTime(event.created_at)}</time></div>
    </article>`).join("") : '<p class="event-empty">No events yet. Discovery is starting.</p>';
}

/** Refresh without reloading the page or overwriting unsaved device edits. */
async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try {
    const [summary, devices, events] = await Promise.all([
      api("/api/summary"), api("/api/devices"), api("/api/events?limit=60"),
    ]);
    state.devices = devices;
    state.events = events;
    state.lastSuccess = new Date();
    renderSummary(summary);
    renderTags();
    renderDevices();
    renderEvents();
    $("connection").className = "connection connected";
    $("connection").innerHTML = summary.demo_mode ? "<i></i> Demo · simulated" : "<i></i> Monitoring live";
    $("updated-at").textContent = `Updated ${state.lastSuccess.toLocaleTimeString()}`;
    if ($("device-dialog").open && state.deviceId !== null) {
      const device = devices.find((item) => item.id === state.deviceId);
      if (device) renderDetail(device);
      await loadHistory(false);
    }
  } catch (error) {
    $("connection").className = "connection disconnected";
    $("connection").innerHTML = "<i></i> Connection lost";
    $("connection").title = error.message;
    $("updated-at").textContent = state.lastSuccess ? `Last update ${state.lastSuccess.toLocaleTimeString()} · retrying` : "Cannot connect · retrying";
  } finally { state.refreshing = false; }
}

/** Render the read-only portion of the selected device detail. */
function renderDetail(device) {
  $("detail-title").textContent = deviceName(device);
  $("detail-address").textContent = `${device.ip}  ·  ${device.mac || "MAC unavailable"}`;
  $("detail-meta").innerHTML = `${statusBadge(device.status)}<span>${escapeHTML(device.vendor || "Vendor unavailable")}</span><span>First seen ${escapeHTML(new Date(device.first_seen).toLocaleString())}</span>`;
  $("uptime-24h").textContent = number(device.uptime_24h, "%");
  $("uptime-7d").textContent = number(device.uptime_7d, "%");
  $("uptime-30d").textContent = number(device.uptime_30d, "%");
}

/** Open the native modal and initialize editable fields once. */
async function openDevice(id) {
  const device = state.devices.find((item) => item.id === id);
  if (!device) return;
  state.deviceId = id;
  renderDetail(device);
  $("friendly-name").value = device.friendly_name || "";
  $("device-tags").value = device.tags.join(", ");
  $("device-trusted").checked = device.trusted;
  $("device-dialog").showModal();
  $("chart-message").textContent = "Loading history…";
  $("chart-message").hidden = false;
  await loadHistory(true);
}

/** Load history with a generation guard against out-of-order requests. */
async function loadHistory(showError) {
  const version = ++state.historyVersion;
  const id = state.deviceId;
  const range = state.range;
  try {
    const history = await api(`/api/devices/${id}/history?range=${range}`);
    if (version !== state.historyVersion || !$("device-dialog").open || id !== state.deviceId) return;
    state.history = history;
    drawHistory(history);
  } catch (error) {
    if (version !== state.historyVersion) return;
    if (showError) toast(error.message, true);
    $("chart-message").textContent = "History unavailable. Retrying on the next refresh.";
    $("chart-message").hidden = false;
  }
}

/** Render latency gaps for outages and an observation-based availability strip. */
function drawHistory(history) {
  const samples = history.samples;
  const duration = { "1h": 3600000, "24h": 86400000, "7d": 604800000 }[history.range];
  const start = Date.now() - duration;
  const segments = Array.from({ length: 120 }, () => ({ up: 0, down: 0 }));
  for (const sample of samples) {
    const index = Math.max(0, Math.min(119, Math.floor((new Date(sample.timestamp).getTime() - start) / duration * 120)));
    segments[index][sample.is_up ? "up" : "down"] += 1;
  }
  $("uptime-timeline").innerHTML = segments.map((segment, index) => {
    const label = segment.down ? "Missed ping" : segment.up ? "Up" : "No observations";
    const time = new Date(start + index * duration / 120).toLocaleString();
    return `<div class="timeline-segment ${segment.down ? "down" : segment.up ? "up" : "unknown"}" title="${escapeHTML(`${time}: ${label}`)}"></div>`;
  }).join("");
  $("timeline-start").textContent = new Date(start).toLocaleString();
  $("chart-message").hidden = samples.length > 0 && typeof Chart !== "undefined";
  if (!samples.length) $("chart-message").textContent = "No ping observations in this time range yet.";
  else if (typeof Chart === "undefined") $("chart-message").textContent = "Chart.js could not load. Check CDN access; monitoring and the timeline still work.";
  if (typeof Chart === "undefined") return;
  if (state.chart) state.chart.destroy();
  const styles = getComputedStyle(document.documentElement);
  const green = styles.getPropertyValue("--green").trim();
  const muted = styles.getPropertyValue("--muted").trim();
  const border = styles.getPropertyValue("--border").trim();
  // Explicit null points prevent a line spanning periods with no monitoring data.
  const points = [];
  for (const sample of samples) {
    const previous = points[points.length - 1];
    if (previous && new Date(sample.timestamp) - new Date(previous.timestamp) > Math.max(history.bucket_seconds * 2, 180) * 1000) {
      points.push({ timestamp: new Date(new Date(previous.timestamp).getTime() + 1000).toISOString(), is_up: false, latency_ms: null });
    }
    points.push(sample);
  }
  state.chart = new Chart($("latency-chart"), {
    type: "line",
    data: {
      datasets: [{
        label: "Latency (ms)", data: points.map((sample) => ({
          x: new Date(sample.timestamp).getTime(), y: sample.is_up ? sample.latency_ms : null,
        })),
        borderColor: green, backgroundColor: `${green}15`, borderWidth: 2, pointRadius: points.length < 2 ? 3 : 0,
        pointHitRadius: 10, fill: true, tension: .2, spanGaps: false,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          callbacks: { title: (items) => items.length ? new Date(items[0].parsed.x).toLocaleString() : "" },
        },
      },
      scales: {
        x: {
          type: "linear", min: start, max: Date.now(),
          grid: { display: false },
          ticks: {
            color: muted, maxTicksLimit: 7, maxRotation: 0, font: { size: 9 },
            callback: (value) => new Date(value).toLocaleString(undefined, {
              month: history.range === "7d" ? "short" : undefined,
              day: history.range === "7d" ? "numeric" : undefined,
              hour: "2-digit", minute: "2-digit",
            }),
          },
          border: { color: border },
        },
        y: { beginAtZero: true, grid: { color: border }, border: { display: false }, ticks: { color: muted, font: { size: 9 } } },
      },
    },
  });
}

/** Submit device settings without closing or resetting the chart. */
async function saveDevice(event) {
  event.preventDefault();
  const id = state.deviceId;
  if (id === null) return;
  $("save-device").disabled = true;
  try {
    const device = await api(`/api/devices/${id}`, {
      method: "PATCH", body: JSON.stringify({
        friendly_name: $("friendly-name").value || null,
        tags: $("device-tags").value.split(",").map((tag) => tag.trim()).filter(Boolean),
        trusted: $("device-trusted").checked,
      }),
    });
    const index = state.devices.findIndex((item) => item.id === id);
    if (index >= 0) state.devices[index] = device;
    if (state.deviceId === id && $("device-dialog").open) renderDetail(device);
    renderTags();
    renderDevices();
    toast("Device settings saved.");
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally { $("save-device").disabled = false; }
}

/** Queue a scan and immediately show the background job's state. */
async function scanNow() {
  $("scan-button").disabled = true;
  try {
    const result = await api("/api/scan", { method: "POST" });
    toast(result.message);
    await refresh();
  } catch (error) { toast(error.message, true); $("scan-button").disabled = false; }
}

/** Send a rate-limited test through Telegram and/or SMTP. */
async function testAlert() {
  $("test-alert").disabled = true;
  try {
    const result = await api("/api/alerts/test", { method: "POST" });
    const messages = {
      sent: "Test sent. Check your configured notification channels.",
      simulated: "Demo alert simulated successfully. No Telegram message or email was sent.",
      failed: "Delivery failed. Check Telegram/SMTP settings and server logs.",
      disabled: "No alert channel configured. Add Telegram or SMTP settings to .env and restart.",
      cooldown: "Test alert is in cooldown. Try again after ALERT_COOLDOWN_SECONDS.",
      backoff: "Please wait 30 seconds before retrying a failed delivery.",
    };
    const channels = Object.entries(result.channels).map(([name, status]) => `${name}: ${status}`).join(", ");
    toast(`${messages[result.status] || result.status}${channels ? ` (${channels})` : ""}`, result.status === "failed");
  } catch (error) { toast(error.message, true); }
  finally { $("test-alert").disabled = false; }
}

/** Toggle the preferred theme without requiring any external assets. */
function toggleTheme() {
  const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("signal-theme", theme); } catch (_) { /* Storage may be disabled. */ }
  if (state.history && $("device-dialog").open) drawHistory(state.history);
}

/** Wire controls once and start polling without overlapping refresh cycles. */
function initialize() {
  try { document.documentElement.dataset.theme = localStorage.getItem("signal-theme") || "dark"; } catch (_) { /* Dark is the default. */ }
  $("search").addEventListener("input", renderDevices);
  for (const id of ["status-filter", "tag-filter", "trust-filter"]) $(id).addEventListener("change", renderDevices);
  document.querySelectorAll("[data-sort]").forEach((button) => button.addEventListener("click", () => {
    state.descending = state.sort === button.dataset.sort ? !state.descending : false;
    state.sort = button.dataset.sort;
    renderDevices();
  }));
  $("device-rows").addEventListener("click", (event) => {
    const row = event.target.closest("[data-device-id]");
    if (row) openDevice(Number(row.dataset.deviceId));
  });
  $("device-rows").addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      const row = event.target.closest("[data-device-id]");
      if (row) { event.preventDefault(); openDevice(Number(row.dataset.deviceId)); }
    }
  });
  $("scan-button").addEventListener("click", scanNow);
  $("test-alert").addEventListener("click", testAlert);
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("device-form").addEventListener("submit", saveDevice);
  $("close-detail").addEventListener("click", () => $("device-dialog").close());
  $("device-dialog").addEventListener("close", () => {
    state.deviceId = null;
    state.historyVersion += 1;
    state.history = null;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
  });
  document.querySelectorAll("[data-range]").forEach((button) => button.addEventListener("click", () => {
    state.range = button.dataset.range;
    document.querySelectorAll("[data-range]").forEach((item) => item.classList.toggle("selected", item === button));
    loadHistory(true);
  }));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 10000);
}

initialize();
