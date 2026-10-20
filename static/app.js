/* STT Wizard — single session page.
 * State comes from the server (bootstrap JSON + Server-Sent Events); the page
 * never holds the source of truth, it only renders it and sends actions.
 */
(() => {
  "use strict";

  const boot = JSON.parse(document.getElementById("bootstrap").textContent);
  const state = {
    session: boot.session,
    sessions: boot.sessions,
    settings: boot.settings,
    models: boot.models,
    reviewFilter: "pending",
    activeLine: null,
    inputMode: "record",
    replacing: false,
    monitoring: false,
    devices: { available: true, error: null, mics: [], systems: [] },
  };
  const $ = (id) => document.getElementById(id);
  const sid = () => state.session.id;

  // ── Utilities ────────────────────────────────────────────────────────
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  const fmtTime = (seconds) => {
    if (seconds === null || seconds === undefined) return "";
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  };

  async function api(path, options = {}) {
    const init = { method: options.method || "GET", headers: {} };
    if (options.json !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.json);
    } else if (options.body) {
      init.body = options.body;
    }
    const response = await fetch(path, init);
    let payload = null;
    try { payload = await response.json(); } catch { /* empty body */ }
    if (!response.ok) {
      throw new Error((payload && payload.detail) || `Request failed (${response.status})`);
    }
    return payload;
  }

  let toastTimer = null;
  function toast(message, kind = "info") {
    const el = $("toast");
    el.textContent = message;
    el.className = `banner banner-${kind}`;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.hidden = true), kind === "error" ? 7000 : 3500);
  }

  async function run(action) {
    try { return await action(); } catch (error) { toast(error.message, "error"); return null; }
  }

  const busy = () => ["recording", "transcribing", "reviewing", "analyzing"].includes(state.session.status);
  const segments = () => (state.session.transcript && state.session.transcript.segments) || [];
  const suggestions = () => (state.session.review && state.session.review.suggestions) || [];

  // ── Sidebar ──────────────────────────────────────────────────────────
  const STATUS_LABEL = { idle: "", recording: "Recording", transcribing: "Transcribing", reviewing: "Reviewing", analyzing: "Analysing", ready: "", failed: "Failed" };

  function renderSidebar() {
    const current = state.sessions.find((item) => item.id === sid());
    if (current) Object.assign(current, { name: state.session.name, status: state.session.status });
    $("session-list").innerHTML = state.sessions
      .map((item) => {
        const status = STATUS_LABEL[item.status] || "";
        const date = new Date(item.created_at || item.updated_at);
        return `<a href="/sessions/${item.id}" class="session-item ${item.id === sid() ? "is-active" : ""}">
          <span class="session-item-name">${esc(item.name)}</span>
          <span class="session-item-meta">${date.toLocaleDateString(undefined, { day: "numeric", month: "short" })}
            ${item.duration ? " · " + fmtTime(item.duration) : ""}
            ${status ? `<span class="pill pill-${item.status}">${status}</span>` : ""}</span>
        </a>`;
      })
      .join("");
  }

  // ── Header / stepper ─────────────────────────────────────────────────
  function renderHeader() {
    const s = state.session;
    const nameInput = $("session-name");
    if (document.activeElement !== nameInput) nameInput.value = s.name;
    document.title = `${s.name} — STT Wizard`;

    const done = {
      audio: Boolean(s.audio || (s.transcript && s.transcript.source === "import")),
      transcript: Boolean(s.transcript && s.transcript.completed_at),
      review: Boolean(s.review),
      analysis: Boolean(s.analysis),
    };
    const activeStep = { recording: "audio", transcribing: "transcript", reviewing: "review", analyzing: "analysis" }[s.status];
    document.querySelectorAll("#stepper li").forEach((li) => {
      li.classList.toggle("is-done", done[li.dataset.step]);
      li.classList.toggle("is-active", li.dataset.step === activeStep);
    });

    const errorBanner = $("error-banner");
    errorBanner.hidden = !(s.status === "failed" && s.error);
    errorBanner.textContent = s.error || "";
    $("notice-banner").hidden = !s.notice;
    $("notice-banner").textContent = s.notice || "";
  }

  // ── Input card ───────────────────────────────────────────────────────
  function renderInput() {
    const s = state.session;
    const hasAudio = Boolean(s.audio);
    const imported = Boolean(s.transcript && s.transcript.source === "import");
    const recording = s.status === "recording";
    const choosing = recording || state.replacing || (!hasAudio && !imported);
    $("input-card").hidden = imported && !choosing;
    $("input-head").hidden = recording || !choosing;
    $("cancel-replace").hidden = !(state.replacing && (hasAudio || imported));
    document.querySelectorAll("#input-head [data-mode]").forEach((b) => b.classList.toggle("is-active", b.dataset.mode === state.inputMode));
    $("record-panel").hidden = !choosing || (state.inputMode !== "record" && !recording);
    $("upload-panel").hidden = !choosing || recording || state.inputMode !== "upload";
    $("audio-bar").hidden = !hasAudio || choosing;
    renderRecordControls();
    if (hasAudio) {
      $("audio-name").textContent = s.audio.original_name;
      const mb = s.audio.size ? `${(s.audio.size / 1048576).toFixed(1)} MB` : "";
      const sources = s.audio.tracks ? Object.values(s.audio.tracks).map((t) => t.device).filter(Boolean).join(" + ") : "";
      $("audio-info").textContent = [s.audio.duration ? fmtTime(s.audio.duration) : "", mb, sources].filter(Boolean).join(" · ");
      const player = $("player");
      if (player.dataset.src !== s.audio.url + s.audio.file) {
        player.dataset.src = s.audio.url + s.audio.file;
        player.src = `${s.audio.url}?v=${encodeURIComponent(s.audio.file)}`;
      }
    }
    $("replace-audio").disabled = busy();
  }

  // ── Recording ────────────────────────────────────────────────────────
  function renderRecordControls() {
    const s = state.session;
    const job = s.job || {};
    const recording = s.status === "recording";
    $("record-idle").hidden = recording;
    $("record-live").hidden = !recording;
    $("use-mic").disabled = recording || !state.devices.mics.length;
    $("use-system").disabled = recording || !state.devices.systems.length;
    $("mic-select").disabled = recording || !$("use-mic").checked;
    $("system-select").disabled = recording || !$("use-system").checked;
    const anySource = $("use-mic").checked || $("use-system").checked;
    $("record-btn").disabled = busy() || !state.devices.available || !anySource;
    $("check-levels").disabled = busy() || !state.devices.available || !anySource;
    $("check-levels").textContent = state.monitoring ? "Stop level check" : "Check levels";
    if (!recording && !state.monitoring) resetMeters();
    if (recording) {
      const paused = job.stage === "paused";
      const saving = job.stage === "saving";
      $("rec-state").textContent = saving ? "Saving…" : paused ? "Paused" : "Recording";
      $("record-live").classList.toggle("is-paused", paused || saving);
      $("pause-btn").textContent = paused ? "Resume" : "Pause";
      ["pause-btn", "stop-btn", "stop-only-btn"].forEach((id) => ($(id).disabled = saving));
    }
  }

  // Segmented meters: 24 segments over -60..0 dBFS, amber from -18, red from -6.
  const METER_SEGMENTS = 24;
  const METER_FLOOR_DB = -60;
  const peaks = {};

  function buildMeters() {
    for (const name of ["mic", "system"]) {
      const meter = $(`meter-${name}`);
      meter.innerHTML = Array.from({ length: METER_SEGMENTS }, (_, i) => {
        const db = METER_FLOOR_DB + ((i + 1) * -METER_FLOOR_DB) / METER_SEGMENTS;
        return `<span class="seg${db > -6 ? " hot" : db > -18 ? " warn" : ""}"></span>`;
      }).join("");
      peaks[name] = { index: -1, until: 0 };
    }
  }

  function resetMeters() {
    for (const name of ["mic", "system"]) {
      if (peaks[name]) peaks[name].index = -1;
      $(`meter-${name}`).querySelectorAll(".seg").forEach((seg) => seg.classList.remove("on", "peak"));
    }
  }

  function litSegments(level) {
    if (!level || level <= 0) return 0;
    const db = 20 * Math.log10(level);
    return Math.max(0, Math.min(METER_SEGMENTS, Math.round(((db - METER_FLOOR_DB) / -METER_FLOOR_DB) * METER_SEGMENTS)));
  }

  function setMeter(name, level) {
    const meter = $(`meter-${name}`);
    const lit = litSegments(level);
    const peak = peaks[name];
    const now = performance.now();
    if (lit - 1 >= peak.index) {
      peak.index = lit - 1;
      peak.until = now + 1200; // hold the peak, then let it fall
    } else if (now > peak.until) {
      peak.index = Math.max(lit - 1, peak.index - 1);
    }
    [...meter.children].forEach((seg, i) => {
      seg.classList.toggle("on", i < lit);
      seg.classList.toggle("peak", i === peak.index && i >= lit);
    });
    meter.setAttribute("aria-valuenow", level ? Math.round(20 * Math.log10(level)) : METER_FLOOR_DB);
  }

  function updateMeters(data) {
    for (const name of ["mic", "system"]) setMeter(name, (data.levels || {})[name] || 0);
    if (!data.monitor && !data.ended) $("rec-timer").textContent = fmtTime(data.elapsed);
    if (data.ended && state.monitoring) {
      state.monitoring = false;
      renderRecordControls();
    }
  }

  function fillDeviceSelect(select, devices, preferredName) {
    const preferred = devices.find((d) => d.name === preferredName) || devices.find((d) => d.default) || devices[0];
    select.innerHTML = devices
      .map((d) => `<option value="${d.id}" data-name="${esc(d.name)}" ${preferred && d.id === preferred.id ? "selected" : ""}>${esc(d.name)}${d.default ? " (default)" : ""}</option>`)
      .join("");
  }

  async function loadDevices() {
    let devices = null;
    try { devices = await api("/api/audio/devices"); } catch (error) { devices = { available: false, error: error.message }; }
    state.devices = { mics: [], systems: [], ...devices };
    const { mics, systems } = state.devices;
    fillDeviceSelect($("mic-select"), mics, state.settings.mic_device);
    fillDeviceSelect($("system-select"), systems, state.settings.system_device);
    $("use-mic").checked = state.settings.record_mic !== false && mics.length > 0;
    $("use-system").checked = state.settings.record_system !== false && systems.length > 0;
    const problem = !state.devices.available ? state.devices.error : !mics.length && !systems.length ? "No audio input devices were found." : "";
    $("devices-error").hidden = !problem;
    $("devices-error").textContent = problem || "";
    renderRecordControls();
  }

  function selectedSources() {
    const pick = (use, select) => {
      const option = $(select).selectedOptions[0];
      return $(use).checked && option ? { id: Number(option.value), name: option.dataset.name } : null;
    };
    const mic = pick("use-mic", "mic-select");
    const system = pick("use-system", "system-select");
    return { mic: mic && mic.id, system: system && system.id, mic_name: mic ? mic.name : "", system_name: system ? system.name : "" };
  }

  async function startRecording() {
    const s = state.session;
    if ((s.audio || s.transcript) && !confirm("A new recording replaces this session's audio, transcript and review. Continue?")) return;
    const sources = selectedSources();
    const session = await run(() => api(`/api/sessions/${sid()}/recording/start`, { method: "POST", json: sources }));
    if (!session) return;
    state.monitoring = false;
    state.replacing = false;
    state.settings.mic_device = sources.mic_name || state.settings.mic_device;
    state.settings.system_device = sources.system_name || state.settings.system_device;
    $("rec-timer").textContent = "0:00";
    applySession(session);
  }

  async function recordingAction(action, json = {}) {
    const session = await run(() => api(`/api/sessions/${sid()}/recording/${action}`, { method: "POST", json }));
    if (session) applySession(session);
  }

  async function toggleLevelCheck() {
    if (state.monitoring) {
      await run(() => api(`/api/sessions/${sid()}/monitor`, { method: "POST", json: { stop: true } }));
      state.monitoring = false;
      updateMeters({ levels: {}, monitor: true });
    } else {
      const ok = await run(() => api(`/api/sessions/${sid()}/monitor`, { method: "POST", json: selectedSources() }));
      state.monitoring = Boolean(ok);
    }
    renderRecordControls();
  }

  // ── Transcription settings ───────────────────────────────────────────
  function renderTranscribeCard() {
    const s = state.session;
    const card = $("transcribe-card");
    card.hidden = !s.audio;
    if (card.hidden) return;
    const hasTranscript = Boolean(s.transcript && s.transcript.segments.length);
    $("transcribe-summary-title").textContent = hasTranscript ? "Transcribe again with different settings" : "Transcription settings";
    const phase = `${sid()}|${s.audio.file}|${hasTranscript}`;
    if (card.dataset.phase !== phase) {
      card.dataset.phase = phase;
      $("transcribe-details").open = !hasTranscript && !busy();
    }
    if (busy()) $("transcribe-details").open = false;
    const button = $("start-transcribe");
    button.disabled = busy();
    button.textContent = hasTranscript ? "Transcribe again" : "Transcribe";
    const glossary = $("opt-glossary");
    if (document.activeElement !== glossary) glossary.value = s.glossary || "";
    const defaults = (state.settings.default_glossary || "").split("\n").filter((l) => l.trim()).length;
    $("glossary-hint").textContent =
      (defaults ? `Plus ${defaults} term(s) from your default glossary in Settings. ` : "") +
      "Keep it short (under ~150 words): the terms are hinted to Whisper throughout the recording.";
  }

  function fillModelSelect(select, selected) {
    select.innerHTML = Object.entries(state.models)
      .map(
        ([name, meta]) =>
          `<option value="${name}" ${name === selected ? "selected" : ""}>${name} — ${meta.size}${meta.downloaded ? "" : " · downloads on first use"}</option>`
      )
      .join("");
  }

  function initTranscribeOptions() {
    fillModelSelect($("opt-model"), state.settings.default_model || "turbo");
    $("opt-language").value = state.settings.default_language || "";
    $("opt-accurate").checked = state.settings.accurate_decoding !== false;
  }

  // ── Job card ─────────────────────────────────────────────────────────
  const JOB_TITLES = { record: "Recording", transcribe: "Transcribing", review: "Reviewing", analysis: "Analysing" };

  function renderJob() {
    const job = state.session.job;
    const card = $("job-card");
    card.hidden = !job || (job.kind === "record" && job.stage !== "saving");
    if (card.hidden) return;
    $("job-title").textContent = JOB_TITLES[job.kind] || "Working";
    $("job-message").textContent = job.message || "";
    const indeterminate = job.kind !== "transcribe" && !job.progress;
    const fill = $("job-progress");
    fill.classList.toggle("indeterminate", indeterminate);
    fill.style.width = indeterminate ? "" : `${Math.max(2, job.progress || 0)}%`;
    $("stop-job").hidden = job.kind !== "transcribe";
    $("stop-job").disabled = job.stage === "stopping";
  }

  // ── Transcript lines ─────────────────────────────────────────────────
  function pendingBySegment() {
    const map = new Map();
    for (const item of suggestions()) {
      if (item.status !== "pending") continue;
      if (!map.has(item.segment_id)) map.set(item.segment_id, []);
      map.get(item.segment_id).push(item);
    }
    return map;
  }

  function highlight(text, items) {
    if (!items || !items.length) return esc(text);
    const lower = text.toLowerCase();
    const ranges = [];
    for (const item of items) {
      let at = text.indexOf(item.original);
      if (at < 0) at = lower.indexOf(item.original.toLowerCase());
      if (at < 0) continue;
      if (ranges.some((r) => at < r.end && at + item.original.length > r.start)) continue;
      ranges.push({ start: at, end: at + item.original.length, id: item.id });
    }
    ranges.sort((a, b) => a.start - b.start);
    let html = "";
    let cursor = 0;
    for (const r of ranges) {
      html += esc(text.slice(cursor, r.start));
      html += `<mark data-suggestion="${r.id}">${esc(text.slice(r.start, r.end))}</mark>`;
      cursor = r.end;
    }
    return html + esc(text.slice(cursor));
  }

  function lineHtml(seg, pending) {
    const timed = seg.start !== null && seg.start !== undefined;
    const edited = seg.text !== seg.original_text;
    return `<div class="line ${edited ? "is-edited" : ""}" id="line-${seg.id}" data-id="${seg.id}" data-speaker="${esc(seg.speaker || "")}">
      ${timed ? `<button type="button" class="ts" data-seek="${seg.start}" title="Play from here">${fmtTime(seg.start)}</button>` : `<span class="ts ts-static">${seg.id.replace(/^s0*/, "#")}</span>`}
      ${seg.speaker ? `<span class="speaker speaker-${seg.speaker === "Me" ? "me" : "others"}">${esc(seg.speaker)}</span>` : ""}
      <div class="text" contenteditable="plaintext-only" spellcheck="true">${highlight(seg.text, pending.get(seg.id))}</div>
    </div>`;
  }

  function renderTranscript({ full = false } = {}) {
    const s = state.session;
    const container = $("lines");
    const list = segments();
    $("workspace").hidden = !s.transcript;
    if (!s.transcript) {
      container.innerHTML = "";
      return;
    }
    const t = s.transcript;
    const metaBits = [t.model, t.language, t.device, t.accurate ? "beam search" : "", t.glossary_used ? "glossary" : "", t.stopped ? "stopped early" : ""];
    $("transcript-meta").textContent = `${list.length} lines · ${metaBits.filter(Boolean).join(" · ")}`;
    $("follow-audio").parentElement.hidden = !s.audio;

    if (!list.length) {
      container.innerHTML = `<p class="placeholder">${s.status === "transcribing" ? "Lines will appear here as Whisper works through the audio…" : t.completed_at ? "No speech was found." : "Transcription did not finish. Run it again."}</p>`;
      return;
    }
    const pending = pendingBySegment();
    if (full || container.querySelector(".placeholder")) container.innerHTML = "";
    const existing = new Map([...container.children].map((el) => [el.dataset.id, el]));
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
    let appended = false;
    for (const seg of list) {
      const el = existing.get(seg.id);
      if (!el) {
        container.insertAdjacentHTML("beforeend", lineHtml(seg, pending));
        appended = true;
        continue;
      }
      const textEl = el.querySelector(".text");
      if (document.activeElement === textEl) continue; // never clobber the line being edited
      if ((el.dataset.speaker || "") !== (seg.speaker || "")) {
        el.outerHTML = lineHtml(seg, pending);
        continue;
      }
      const html = highlight(seg.text, pending.get(seg.id));
      if (textEl.innerHTML !== html) textEl.innerHTML = html;
      el.classList.toggle("is-edited", seg.text !== seg.original_text);
    }
    const ids = new Set(list.map((seg) => seg.id));
    for (const [id, el] of existing) if (!ids.has(id)) el.remove();
    if (appended && s.status === "transcribing" && nearBottom) container.scrollTop = container.scrollHeight;
    renderExports();
  }

  function renderExports() {
    const t = state.session.transcript;
    const formats = t && t.source === "import" ? ["txt", "json"] : ["txt", "srt", "vtt", "json"];
    const version = $("export-version").value;
    $("export-buttons").innerHTML = formats
      .map((f) => `<a class="btn btn-ghost btn-sm" href="/api/sessions/${sid()}/export?format=${f}&version=${version}">${f.toUpperCase()}</a>`)
      .join("");
  }

  async function saveLine(textEl) {
    const line = textEl.closest(".line");
    const seg = segments().find((item) => item.id === line.dataset.id);
    const text = textEl.innerText.replace(/\s+/g, " ").trim();
    if (!seg || text === seg.text) {
      if (seg) textEl.innerHTML = highlight(seg.text, pendingBySegment().get(seg.id));
      return;
    }
    const updated = await run(() => api(`/api/sessions/${sid()}/segments/${seg.id}`, { method: "PATCH", json: { text } }));
    if (updated) {
      seg.text = updated.text;
      textEl.innerHTML = highlight(seg.text, pendingBySegment().get(seg.id));
      line.classList.toggle("is-edited", seg.text !== seg.original_text);
      renderReview();
    }
  }

  // ── Review panel ─────────────────────────────────────────────────────
  function renderReview() {
    const s = state.session;
    const review = s.review;
    const list = suggestions();
    const noKey = !state.settings.api_key_configured;
    $("no-key-banner").hidden = !noKey;
    const runButton = $("run-review");
    runButton.disabled = busy() || noKey || !segments().length || s.status === "transcribing";
    runButton.textContent = review ? "Run again" : "Run review";

    const pendingCount = list.filter((i) => i.status === "pending").length;
    const doneCount = list.length - pendingCount;
    $("review-filters").hidden = !list.length;
    $("count-pending").textContent = pendingCount;
    $("count-done").textContent = doneCount;
    $("apply-all").disabled = !pendingCount || busy();
    document.querySelectorAll("#review-filters .chip").forEach((chip) => chip.classList.toggle("is-active", chip.dataset.filter === state.reviewFilter));

    if (review) {
      const when = review.created_at ? new Date(review.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
      $("review-meta").textContent = `${review.model}${review.glossary_used ? " · glossary" : ""}${when ? " · " + when : ""}`;
    } else {
      $("review-meta").textContent = "";
    }

    const container = $("suggestions");
    if (s.status === "reviewing") {
      container.innerHTML = `<p class="placeholder">Checking names, acronyms and terms…</p>`;
      return;
    }
    if (!review) {
      container.innerHTML = noKey
        ? ""
        : `<p class="placeholder">${s.status === "transcribing" ? (state.settings.auto_review ? "Review will start automatically when the transcript is done." : "Run review once the transcript is done.") : "The LLM flags words that look misheard: names, acronyms, products, terms."}</p>`;
      return;
    }
    const visible = list.filter((i) => (state.reviewFilter === "pending" ? i.status === "pending" : i.status !== "pending"));
    if (!visible.length) {
      const extra = review.hidden_dismissed ? ` (${review.hidden_dismissed} previously dismissed hidden)` : "";
      container.innerHTML = `<p class="placeholder">${state.reviewFilter === "pending" ? (list.length ? "All suggestions handled." : "Nothing suspicious found" + extra + ".") : "Nothing handled yet."}</p>`;
      return;
    }
    const segById = new Map(segments().map((seg) => [seg.id, seg]));
    container.innerHTML = visible.map((item) => suggestionHtml(item, segById.get(item.segment_id))).join("");
  }

  function suggestionHtml(item, seg) {
    const time = seg && seg.start !== null ? fmtTime(seg.start) : "";
    const context = seg ? highlight(seg.text, item.status === "pending" ? [item] : []) : "";
    let actions = "";
    if (item.status === "pending") {
      actions = `<input class="replacement" value="${esc(item.suggested)}" aria-label="Replacement" />
        <button type="button" class="btn btn-primary btn-sm" data-act="apply">Apply</button>
        <button type="button" class="btn btn-ghost btn-sm" data-act="dismiss">Dismiss</button>`;
    } else if (item.status === "applied") {
      actions = `<span class="pill pill-ok">Applied: ${esc(item.applied || item.suggested)}</span>
        <button type="button" class="btn btn-ghost btn-sm" data-act="revert">Undo</button>
        <button type="button" class="btn btn-ghost btn-sm" data-act="glossary" title="Add to default glossary">+ Glossary</button>`;
    } else if (item.status === "dismissed") {
      actions = `<span class="pill">Dismissed</span><button type="button" class="btn btn-ghost btn-sm" data-act="reopen">Reopen</button>`;
    } else {
      actions = `<span class="pill pill-warn">Line was edited</span><button type="button" class="btn btn-ghost btn-sm" data-act="dismiss">Dismiss</button>`;
    }
    return `<article class="suggestion is-${item.status}" data-id="${item.id}" data-segment="${item.segment_id}">
      <div class="suggestion-top">
        <span class="orig">${esc(item.original)}</span><span class="arrow">→</span><span class="sugg">${esc(item.suggested)}</span>
        ${time ? `<button type="button" class="ts" data-seek="${seg.start}">${time}</button>` : ""}
      </div>
      <p class="context" data-goto="${item.segment_id}">${context}</p>
      ${item.justification ? `<p class="why">${esc(item.justification)}</p>` : ""}
      <div class="suggestion-actions">${actions}</div>
    </article>`;
  }

  async function suggestionAction(card, act) {
    const id = card.dataset.id;
    if (act === "glossary") {
      const item = suggestions().find((s) => s.id === id);
      const settings = await run(() => api("/api/settings/glossary-term", { method: "POST", json: { term: item.applied || item.suggested } }));
      if (settings) {
        state.settings = settings;
        renderTranscribeCard();
        toast(`Added “${item.applied || item.suggested}” to your default glossary.`);
      }
      return;
    }
    const body = { action: act };
    if (act === "apply") body.replacement = card.querySelector(".replacement").value.trim();
    const session = await run(() => api(`/api/sessions/${sid()}/suggestions/${id}`, { method: "POST", json: body }));
    if (session) applySession(session);
  }

  // ── Analysis ─────────────────────────────────────────────────────────
  function renderAnalysis() {
    const s = state.session;
    const card = $("analysis-card");
    card.hidden = !(s.transcript && s.transcript.segments.length);
    if (card.hidden) return;
    const a = s.analysis;
    $("run-analysis").disabled = busy();
    $("run-analysis").textContent = a ? "Run again" : "Run analysis";
    $("analysis-meta").textContent = a ? `${a.model}${a.mode === "local" ? " (no API key: basic local summary)" : ""}` : "";
    if (s.status === "analyzing") {
      $("analysis-body").innerHTML = `<p class="placeholder">Summarising…</p>`;
      return;
    }
    if (!a) {
      $("analysis-body").innerHTML = `<p class="placeholder">Summary, key points, action items and open questions.</p>`;
      return;
    }
    const list = (title, items) =>
      items && items.length ? `<div><h3>${title}</h3><ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></div>` : "";
    $("analysis-body").innerHTML = `<p class="summary">${esc(a.summary)}</p>
      <div class="analysis-grid">${list("Key points", a.key_points)}${list("Action items", a.action_items)}${list("Open questions", a.open_questions)}</div>`;
  }

  // ── Render all ───────────────────────────────────────────────────────
  function renderAll({ fullTranscript = false } = {}) {
    renderHeader();
    renderSidebar();
    renderInput();
    renderTranscribeCard();
    renderJob();
    renderTranscript({ full: fullTranscript });
    renderReview();
    renderAnalysis();
  }

  function applySession(session) {
    // HTTP responses and SSE snapshots can arrive out of order; keep the newest.
    if (session.id === sid() && (session.rev || 0) < (state.session.rev || 0)) return;
    state.session = session;
    renderAll();
  }

  // ── Live updates (SSE) ───────────────────────────────────────────────
  function connect() {
    const source = new EventSource(`/api/sessions/${sid()}/events`);
    source.addEventListener("session", (e) => applySession(JSON.parse(e.data)));
    source.addEventListener("levels", (e) => updateMeters(JSON.parse(e.data)));
    source.addEventListener("job", (e) => {
      state.session.job = JSON.parse(e.data);
      renderJob();
    });
    source.addEventListener("segments", (e) => {
      const data = JSON.parse(e.data);
      if (!state.session.transcript) return;
      const known = new Set(segments().map((s) => s.id));
      state.session.transcript.segments.push(...data.segments.filter((s) => !known.has(s.id)));
      state.session.job = data.job;
      renderJob();
      renderTranscript();
    });
    source.addEventListener("segment", (e) => {
      const seg = JSON.parse(e.data);
      const target = segments().find((s) => s.id === seg.id);
      if (target) Object.assign(target, seg);
      renderTranscript();
    });
    // EventSource reconnects by itself; the server re-sends a full snapshot on connect.
  }

  // ── Actions ──────────────────────────────────────────────────────────
  async function uploadFile(file) {
    if (!file) return;
    if (state.session.transcript && !confirm("This file replaces the session's audio, transcript and review. Continue?")) return;
    const form = new FormData();
    form.append("file", file);
    toast(`Uploading ${file.name}…`);
    const session = await run(() => api(`/api/sessions/${sid()}/upload`, { method: "POST", body: form }));
    if (!session) return;
    state.replacing = false;
    applySession(session);
    if (session.audio) startTranscription();
    else toast("Transcript imported. You can review it now.");
  }

  async function startTranscription() {
    await saveGlossary();
    const session = await run(() =>
      api(`/api/sessions/${sid()}/transcribe`, {
        method: "POST",
        json: {
          model: $("opt-model").value,
          language: $("opt-language").value.trim(),
          task: $("opt-translate").checked ? "translate" : "transcribe",
          accurate: $("opt-accurate").checked,
        },
      })
    );
    if (session) {
      $("transcribe-details").open = false;
      applySession(session);
      renderTranscript({ full: true });
    }
  }

  async function saveGlossary() {
    const value = $("opt-glossary").value;
    if (value === (state.session.glossary || "")) return;
    const session = await run(() => api(`/api/sessions/${sid()}`, { method: "PATCH", json: { glossary: value } }));
    if (session && (session.rev || 0) >= (state.session.rev || 0)) state.session = session;
  }

  function seek(seconds) {
    const player = $("player");
    if (!state.session.audio || Number.isNaN(seconds)) return;
    player.currentTime = seconds;
    player.play().catch(() => {});
  }

  function gotoLine(segmentId) {
    const el = $(`line-${segmentId}`);
    if (!el) return;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.add("is-flash");
    setTimeout(() => el.classList.remove("is-flash"), 1200);
  }

  // ── Settings dialog ──────────────────────────────────────────────────
  function openSettings() {
    const s = state.settings;
    $("set-api-key").value = "";
    $("key-status").textContent = s.api_key_configured ? "A key is saved." : "No key saved yet.";
    $("set-review-model").value = s.review_model || "";
    $("set-analysis-model").value = s.analysis_model || "";
    $("set-auto-review").checked = Boolean(s.auto_review);
    $("set-auto-analysis").checked = Boolean(s.auto_analysis);
    fillModelSelect($("set-default-model"), s.default_model || "turbo");
    $("set-default-language").value = s.default_language || "";
    $("set-accurate").checked = s.accurate_decoding !== false;
    $("set-glossary").value = s.default_glossary || "";
    $("settings-dialog").showModal();
  }

  async function saveSettings() {
    const payload = {
      openai_api_key: $("set-api-key").value.trim(),
      review_model: $("set-review-model").value.trim(),
      analysis_model: $("set-analysis-model").value.trim(),
      auto_review: $("set-auto-review").checked,
      auto_analysis: $("set-auto-analysis").checked,
      default_model: $("set-default-model").value,
      default_language: $("set-default-language").value.trim(),
      accurate_decoding: $("set-accurate").checked,
      default_glossary: $("set-glossary").value,
    };
    const settings = await run(() => api("/api/settings", { method: "POST", json: payload }));
    if (!settings) return;
    state.settings = settings;
    $("settings-dialog").close();
    initTranscribeOptions();
    renderAll();
    toast("Settings saved.");
  }

  // ── Wiring ───────────────────────────────────────────────────────────
  function wire() {
    $("dropzone-formats").textContent = boot.audio_extensions.map((e) => e.slice(1).toUpperCase()).join(" · ");
    $("file-input").accept = [...boot.audio_extensions, ...boot.text_extensions].join(",");

    const dz = $("dropzone");
    dz.addEventListener("click", () => $("file-input").click());
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file-input").click(); } });
    dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("is-over"); });
    dz.addEventListener("dragleave", () => dz.classList.remove("is-over"));
    dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("is-over"); uploadFile(e.dataTransfer.files[0]); });
    $("file-input").addEventListener("change", (e) => { uploadFile(e.target.files[0]); e.target.value = ""; });
    $("replace-audio").addEventListener("click", () => {
      state.replacing = true;
      renderInput();
    });
    $("cancel-replace").addEventListener("click", () => {
      state.replacing = false;
      if (state.monitoring) toggleLevelCheck();
      renderInput();
    });
    document.querySelectorAll("#input-head [data-mode]").forEach((button) =>
      button.addEventListener("click", () => {
        state.inputMode = button.dataset.mode;
        if (state.inputMode !== "record" && state.monitoring) toggleLevelCheck();
        renderInput();
      })
    );
    ["use-mic", "use-system"].forEach((id) => $(id).addEventListener("change", renderRecordControls));
    $("record-btn").addEventListener("click", startRecording);
    $("check-levels").addEventListener("click", toggleLevelCheck);
    $("pause-btn").addEventListener("click", () => recordingAction(state.session.job && state.session.job.stage === "paused" ? "resume" : "pause"));
    $("stop-btn").addEventListener("click", () => recordingAction("stop"));
    $("stop-only-btn").addEventListener("click", () => recordingAction("stop", { transcribe: false }));

    $("new-session").addEventListener("click", async () => {
      const session = await run(() => api("/api/sessions", { method: "POST", json: {} }));
      if (session) window.location.href = `/sessions/${session.id}`;
    });
    $("delete-session").addEventListener("click", async () => {
      if (!confirm(`Delete “${state.session.name}” and its audio, transcript and review?`)) return;
      const ok = await run(() => api(`/api/sessions/${sid()}`, { method: "DELETE" }));
      if (ok) window.location.href = "/";
    });

    const nameInput = $("session-name");
    const saveName = async () => {
      const name = nameInput.value.trim();
      if (!name || name === state.session.name) { nameInput.value = state.session.name; return; }
      const session = await run(() => api(`/api/sessions/${sid()}`, { method: "PATCH", json: { name } }));
      if (session) applySession(session);
    };
    nameInput.addEventListener("blur", saveName);
    nameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") nameInput.blur(); });

    $("opt-glossary").addEventListener("blur", saveGlossary);
    $("start-transcribe").addEventListener("click", () => {
      if (state.session.transcript && state.session.transcript.segments.length &&
          !confirm("Transcribing again replaces the current transcript, edits and review. Continue?")) return;
      startTranscription();
    });
    $("stop-job").addEventListener("click", () => run(() => api(`/api/sessions/${sid()}/stop`, { method: "POST" })));

    $("run-review").addEventListener("click", async () => {
      const session = await run(() => api(`/api/sessions/${sid()}/review`, { method: "POST" }));
      if (session) { state.reviewFilter = "pending"; applySession(session); }
    });
    $("apply-all").addEventListener("click", async () => {
      const session = await run(() => api(`/api/sessions/${sid()}/suggestions-apply-all`, { method: "POST" }));
      if (session) applySession(session);
    });
    $("review-filters").addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      state.reviewFilter = chip.dataset.filter;
      renderReview();
    });
    $("suggestions").addEventListener("click", (e) => {
      const seekBtn = e.target.closest("[data-seek]");
      if (seekBtn) return seek(parseFloat(seekBtn.dataset.seek));
      const actBtn = e.target.closest("[data-act]");
      if (actBtn) return suggestionAction(actBtn.closest(".suggestion"), actBtn.dataset.act);
      const ctx = e.target.closest("[data-goto]");
      if (ctx) gotoLine(ctx.dataset.goto);
    });
    $("suggestions").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && e.target.classList.contains("replacement")) suggestionAction(e.target.closest(".suggestion"), "apply");
    });

    const lines = $("lines");
    lines.addEventListener("click", (e) => {
      const seekBtn = e.target.closest("[data-seek]");
      if (seekBtn) return seek(parseFloat(seekBtn.dataset.seek));
      const mark = e.target.closest("mark[data-suggestion]");
      if (mark) {
        state.reviewFilter = "pending";
        renderReview();
        const card = document.querySelector(`.suggestion[data-id="${mark.dataset.suggestion}"]`);
        if (card) { card.scrollIntoView({ block: "nearest", behavior: "smooth" }); card.classList.add("is-flash"); setTimeout(() => card.classList.remove("is-flash"), 1200); }
      }
    });
    lines.addEventListener("focusin", (e) => {
      if (!e.target.classList.contains("text")) return;
      const seg = segments().find((s) => s.id === e.target.closest(".line").dataset.id);
      if (seg && e.target.querySelector("mark")) e.target.textContent = seg.text;
    });
    lines.addEventListener("focusout", (e) => { if (e.target.classList.contains("text")) saveLine(e.target); });
    lines.addEventListener("keydown", (e) => {
      if (!e.target.classList.contains("text")) return;
      if (e.key === "Enter") { e.preventDefault(); e.target.blur(); }
      if (e.key === "Escape") {
        const seg = segments().find((s) => s.id === e.target.closest(".line").dataset.id);
        if (seg) e.target.textContent = seg.text;
        e.target.blur();
      }
    });

    $("player").addEventListener("timeupdate", () => {
      const t = $("player").currentTime;
      const seg = segments().find((s) => s.start !== null && t >= s.start && t < s.end);
      const id = seg ? seg.id : null;
      if (id === state.activeLine) return;
      if (state.activeLine) $(`line-${state.activeLine}`)?.classList.remove("is-playing");
      state.activeLine = id;
      if (!id) return;
      const el = $(`line-${id}`);
      el?.classList.add("is-playing");
      if (el && $("follow-audio").checked && !$("player").paused && !el.contains(document.activeElement)) {
        el.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    });

    $("export-version").addEventListener("change", renderExports);
    $("run-analysis").addEventListener("click", async () => {
      const session = await run(() => api(`/api/sessions/${sid()}/analysis`, { method: "POST" }));
      if (session) applySession(session);
    });

    $("open-settings").addEventListener("click", openSettings);
    $("no-key-settings").addEventListener("click", (e) => { e.preventDefault(); openSettings(); });
    $("close-settings").addEventListener("click", () => $("settings-dialog").close());
    $("save-settings").addEventListener("click", saveSettings);
    $("test-key").addEventListener("click", async () => {
      $("key-status").textContent = "Testing…";
      const result = await run(() => api("/api/settings/test-key", { method: "POST", json: { openai_api_key: $("set-api-key").value.trim() } }));
      if (result) $("key-status").textContent = result.message;
    });
  }

  wire();
  initTranscribeOptions();
  renderAll({ fullTranscript: true });
  buildMeters();
  connect();
  loadDevices();
})();
