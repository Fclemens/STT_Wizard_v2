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
    monitoring: false,
    devices: { available: true, error: null, mics: [], systems: [] },
    stopAt: null,          // pause playback here (speaker sample, "Play selection")
    sampleTurn: {},        // speaker -> which sample to play next
    rangeAnchor: null,     // last line whose speaker label was clicked (Shift-click ranges)
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

  const busy = () => ["recording", "transcribing", "diarizing", "reviewing", "analyzing"].includes(state.session.status);
  const segments = () => (state.session.transcript && state.session.transcript.segments) || [];
  // Suggestions on lines hidden by a trim are kept on the server but not shown.
  const suggestions = () => {
    const all = (state.session.review && state.session.review.suggestions) || [];
    const visible = new Set(segments().map((seg) => seg.id));
    return all.filter((item) => visible.has(item.segment_id));
  };

  // Times typed as "1:02:03.5", "2:15" or "75".
  const parseTime = (text) => {
    const parts = String(text).trim().split(":").map((part) => parseFloat(part.replace(",", ".")));
    if (!parts.length || parts.some((n) => Number.isNaN(n) || n < 0)) return null;
    return parts.reduce((total, n) => total * 60 + n, 0);
  };
  const fmtPrecise = (seconds) => `${fmtTime(seconds)}.${Math.floor((seconds % 1) * 10)}`;
  const fmtTalk = (seconds) => (seconds >= 90 ? `${Math.round(seconds / 60)} min` : `${Math.round(seconds)} s`);

  // ── Sidebar ──────────────────────────────────────────────────────────
  const STATUS_LABEL = { idle: "", recording: "Recording", transcribing: "Transcribing", diarizing: "Speakers", reviewing: "Reviewing", analyzing: "Analysing", ready: "", failed: "Failed" };

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
    const activeStep = { recording: "audio", transcribing: "transcript", diarizing: "transcript", reviewing: "review", analyzing: "analysis" }[s.status];
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
    const choosing = recording || (!hasAudio && !imported);
    $("input-card").hidden = imported;
    $("input-head").hidden = recording || !choosing;
    document.querySelectorAll("#input-head [data-mode]").forEach((b) => b.classList.toggle("is-active", b.dataset.mode === state.inputMode));
    $("record-panel").hidden = !choosing || (state.inputMode !== "record" && !recording);
    $("upload-panel").hidden = !choosing || recording || state.inputMode !== "upload";
    $("audio-bar").hidden = !hasAudio || choosing;
    renderRecordControls();
    if (!hasAudio || choosing) closeTrim();
    if (hasAudio) {
      $("audio-name").textContent = s.audio.original_name;
      const mb = s.audio.size ? `${(s.audio.size / 1048576).toFixed(1)} MB` : "";
      const sources = s.audio.tracks ? Object.values(s.audio.tracks).map((t) => t.device).filter(Boolean).join(" + ") : "";
      const t = s.audio.trim;
      const kept = t ? `<span class="trim-badge" title="Only this part is played, transcribed and exported">✂ ${fmtTime(t.start)}–${fmtTime(t.end)}</span>` : "";
      $("audio-info").innerHTML = [s.audio.duration ? fmtTime(s.audio.duration) : "", mb, esc(sources), kept].filter(Boolean).join(" · ");
      const player = $("player");
      if (player.dataset.src !== s.audio.url + s.audio.file) {
        player.dataset.src = s.audio.url + s.audio.file;
        player.src = `${s.audio.url}?v=${encodeURIComponent(s.audio.file)}`;
        trim.wave = null;
      }
      $("menu-trim").disabled = busy();
    }
    renderTrim();
  }

  async function exportAudio() {
    const button = $("export-audio");
    const labelEl = button.querySelector("span");
    const label = labelEl.textContent;
    button.disabled = true;
    labelEl.textContent = "Preparing…";
    try {
      const response = await fetch(`/api/sessions/${sid()}/audio/export`);
      if (!response.ok) {
        let detail = `Export failed (${response.status})`;
        try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
        throw new Error(detail);
      }
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename\*=utf-8''([^;]+)/i) || disposition.match(/filename="?([^";]+)"?/);
      const name = match ? decodeURIComponent(match[1]) : "recording.mp3";
      const url = URL.createObjectURL(await response.blob());
      const link = Object.assign(document.createElement("a"), { href: url, download: name });
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
      labelEl.textContent = label;
      closeAudioMenu();
    }
  }

  // ── Audio menu (⋮) ───────────────────────────────────────────────────
  function toggleAudioMenu(open = $("audio-menu").hidden) {
    const menu = $("audio-menu");
    menu.hidden = !open;
    $("audio-menu-btn").setAttribute("aria-expanded", String(open));
    // Opens to the left of the button; on narrow screens the button wraps left, so flip it.
    menu.classList.remove("open-right");
    if (open && menu.getBoundingClientRect().left < 8) menu.classList.add("open-right");
  }
  const closeAudioMenu = () => toggleAudioMenu(false);

  function setPlaybackRate(rate) {
    $("player").playbackRate = rate;
    document.querySelectorAll("#menu-speeds button").forEach((b) => b.classList.toggle("is-active", Number(b.dataset.rate) === rate));
  }

  // ── Trim ─────────────────────────────────────────────────────────────
  // While the trim panel is open the whole recording can be played (to find the cut points);
  // otherwise playback stays inside the saved trim.
  const trim = { open: false, draft: null, wave: null, drag: null };
  const savedTrim = () => (state.session.audio && state.session.audio.trim) || null;
  const playRange = () => (trim.open ? null : savedTrim());
  const audioDuration = () =>
    (trim.wave && trim.wave.duration) || (state.session.audio && state.session.audio.duration) || $("player").duration || 0;

  async function openTrim() {
    closeAudioMenu();
    if (busy()) return;
    trim.open = true;
    $("trim-panel").hidden = false;
    if (!trim.wave) {
      $("trim-summary").textContent = "Reading the audio…";
      trim.wave = await run(() => api(`/api/sessions/${sid()}/audio/waveform`));
      if (!trim.wave || !trim.open) return closeTrim();
    }
    const saved = savedTrim();
    trim.draft = saved ? { start: saved.start, end: saved.end } : { start: 0, end: audioDuration() };
    renderTrim();
    drawWave();
    $("trim-panel").scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function closeTrim() {
    if (!trim.open) return;
    trim.open = false;
    trim.draft = null;
    trim.drag = null;
    $("trim-panel").hidden = true;
  }

  function renderTrim() {
    if (!trim.open || !trim.draft) return;
    const duration = audioDuration() || 1;
    const { start, end } = trim.draft;
    const pct = (x) => `${(x / duration) * 100}%`;
    $("trim-shade-left").style.width = pct(start);
    $("trim-shade-right").style.width = pct(duration - end);
    $("trim-handle-start").style.left = pct(start);
    $("trim-handle-end").style.left = pct(end);
    $("trim-handle-start").setAttribute("aria-valuetext", fmtTime(start));
    $("trim-handle-end").setAttribute("aria-valuetext", fmtTime(end));
    if (document.activeElement !== $("trim-start")) $("trim-start").value = fmtPrecise(start);
    if (document.activeElement !== $("trim-end")) $("trim-end").value = fmtPrecise(end);
    const whole = start <= 0.05 && end >= duration - 0.05;
    $("trim-summary").textContent = whole
      ? `Whole recording (${fmtTime(duration)}): drag the handles to cut the start or the end.`
      : `Keeping ${fmtTime(start)} – ${fmtTime(end)}: ${fmtTime(end - start)} of ${fmtTime(duration)}`;
    const saved = savedTrim();
    $("trim-reset").hidden = !saved;
    const unchanged = saved ? Math.abs(saved.start - start) < 0.05 && Math.abs(saved.end - end) < 0.05 : whole;
    $("trim-save").disabled = busy() || unchanged;
    updatePlayhead();
  }

  function drawWave() {
    const canvas = $("trim-canvas");
    if (!trim.open || !trim.wave) return;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.floor(canvas.clientWidth * ratio));
    const height = Math.max(1, Math.floor(canvas.clientHeight * ratio));
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, width, height);
    const peaks = trim.wave.peaks || [];
    if (!peaks.length) return;
    const gradient = ctx.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, "#c4b5fd");
    gradient.addColorStop(0.5, "#8b5cf6");
    gradient.addColorStop(1, "#c4b5fd");
    ctx.fillStyle = gradient;
    const bar = Math.max(1, Math.round(2 * ratio));
    const gap = Math.max(1, Math.round(ratio));
    for (let x = 0; x < width; x += bar + gap) {
      const from = Math.floor((x / width) * peaks.length);
      const to = Math.max(from + 1, Math.floor(((x + bar) / width) * peaks.length));
      let peak = 0;
      for (let i = from; i < to && i < peaks.length; i++) peak = Math.max(peak, peaks[i]);
      const h = Math.max(ratio, peak * (height - 8 * ratio));
      ctx.fillRect(x, (height - h) / 2, bar, h);
    }
  }

  function updatePlayhead() {
    if (!trim.open) return;
    const duration = audioDuration() || 1;
    $("trim-playhead").style.left = `${(Math.min($("player").currentTime, duration) / duration) * 100}%`;
  }

  function setTrimEdge(edge, seconds) {
    const duration = audioDuration();
    const { start, end } = trim.draft;
    if (edge === "start") trim.draft.start = Math.max(0, Math.min(seconds, end - 1));
    else trim.draft.end = Math.min(duration, Math.max(seconds, start + 1));
    renderTrim();
  }

  function timeAt(clientX) {
    const rect = $("trim-wave").getBoundingClientRect();
    return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)) * audioDuration();
  }

  async function saveTrim(clear = false) {
    const duration = audioDuration();
    const { start, end } = clear ? { start: 0, end: duration } : trim.draft;
    const whole = start <= 0.05 && end >= duration - 0.05;
    const json = whole ? { start: null, end: null } : { start, end };
    const session = await run(() => api(`/api/sessions/${sid()}/trim`, { method: "POST", json }));
    if (!session) return;
    closeTrim();
    applySession(session);
    const hidden = (session.transcript && session.transcript.trimmed_count) || 0;
    toast(whole ? "Trim removed: the whole recording is used again." :
      `Keeping ${fmtTime(start)} – ${fmtTime(end)}.${hidden ? ` ${hidden} transcript line(s) outside it are hidden.` : ""}`);
  }

  // Playback: stay inside the saved trim, and stop at the end of a sample or of "Play selection".
  function onPlayerTime() {
    const player = $("player");
    const range = playRange();
    if (state.stopAt !== null && player.currentTime >= state.stopAt) {
      player.pause();
      state.stopAt = null;
      renderSpeakerPlaying();
    } else if (range && player.currentTime >= range.end) {
      player.pause();
      player.currentTime = range.end;
    }
    updatePlayhead();
  }

  function keepInRange() {
    const player = $("player");
    const range = playRange();
    if (!range) return;
    if (player.currentTime < range.start - 0.05) player.currentTime = range.start;
    else if (player.currentTime > range.end + 0.05) player.currentTime = range.end;
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
    $("refresh-devices").hidden = state.inputMode !== "record";
    $("refresh-devices").disabled = busy() || state.monitoring;
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

  async function loadDevices({ refresh = false } = {}) {
    const before = state.devices;
    const chosen = (select) => (select.selectedOptions[0] ? select.selectedOptions[0].dataset.name : "");
    // On refresh keep what the user picked; on first load use the remembered devices.
    const micName = (refresh && chosen($("mic-select"))) || state.settings.mic_device;
    const systemName = (refresh && chosen($("system-select"))) || state.settings.system_device;
    const keepMic = refresh && before.mics.length ? $("use-mic").checked : state.settings.record_mic !== false;
    const keepSystem = refresh && before.systems.length ? $("use-system").checked : state.settings.record_system !== false;

    let devices = null;
    try { devices = await api("/api/audio/devices"); } catch (error) { devices = { available: false, error: error.message }; }
    state.devices = { mics: [], systems: [], ...devices };
    const { mics, systems } = state.devices;
    fillDeviceSelect($("mic-select"), mics, micName);
    fillDeviceSelect($("system-select"), systems, systemName);
    $("use-mic").checked = keepMic && mics.length > 0;
    $("use-system").checked = keepSystem && systems.length > 0;
    if (refresh) {
      const known = new Set([...before.mics, ...before.systems].map((d) => d.name));
      const added = [...mics, ...systems].filter((d) => !known.has(d.name)).map((d) => d.name);
      toast(added.length ? `New: ${added.join(", ")}` : `${mics.length} microphone(s), ${systems.length} output(s) found. No new devices.`);
    }
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
    const kept = s.audio.trim ? ` · only ${fmtTime(s.audio.trim.start)}–${fmtTime(s.audio.trim.end)} (trimmed)` : "";
    $("transcribe-summary-title").textContent = (hasTranscript ? "Transcribe again with different settings" : "Transcription settings") + kept;
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
      "Whisper uses the first ~40–60 terms (it shares space with the running context), so put the most important first. Review always uses the full list.";
  }

  function fillModelSelect(select, selected) {
    select.innerHTML = Object.entries(state.models)
      .map(
        ([name, meta]) =>
          `<option value="${name}" ${name === selected ? "selected" : ""}>${name} — ${meta.size}${meta.downloaded ? "" : " · downloads on first use"}</option>`
      )
      .join("");
  }

  function fillSpeakerCount(select) {
    select.innerHTML = `<option value="0">Auto-detect</option>` +
      Array.from({ length: 8 }, (_, i) => `<option value="${i + 1}">${i + 1} speaker${i ? "s" : ""}</option>`).join("");
  }

  function initTranscribeOptions() {
    fillModelSelect($("opt-model"), state.settings.default_model || "turbo");
    $("opt-language").value = state.settings.default_language || "";
    $("opt-accurate").checked = state.settings.accurate_decoding !== false;
    $("opt-speakers").checked = state.settings.detect_speakers !== false;
    $("opt-speaker-count").disabled = !$("opt-speakers").checked;
  }

  // ── Job card ─────────────────────────────────────────────────────────
  const JOB_TITLES = { record: "Recording", transcribe: "Transcribing", speakers: "Detecting speakers", review: "Reviewing", analysis: "Analysing" };

  function renderJob() {
    const job = state.session.job;
    const card = $("job-card");
    card.hidden = !job || (job.kind === "record" && job.stage !== "saving");
    if (card.hidden) return;
    const speakerStage = ["speakers", "speaker_model"].includes(job.stage);
    $("job-title").textContent = speakerStage ? "Detecting speakers" : JOB_TITLES[job.kind] || "Working";
    $("job-message").textContent = job.message || "";
    const indeterminate = job.kind !== "transcribe" && !job.progress;
    const fill = $("job-progress");
    fill.classList.toggle("indeterminate", indeterminate);
    fill.style.width = indeterminate ? "" : `${Math.max(2, job.progress || 0)}%`;
    $("stop-job").hidden = job.kind !== "transcribe" || speakerStage;
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

  const speakerKey = (seg, speakers) => {
    const spk = seg.speaker ? speakers.get(seg.speaker) : null;
    return spk ? `${spk.key}|${spk.name}|${spk.color}` : "";
  };

  // Speaker labels can be switched off per session (older transcripts: on).
  const labelsOn = () => Boolean(state.session.transcript) && state.session.transcript.speaker_labels !== false;

  function lineHtml(seg, pending, speakers) {
    const timed = seg.start !== null && seg.start !== undefined;
    const edited = seg.text !== seg.original_text;
    const spk = seg.speaker ? speakers.get(seg.speaker) : null;
    const label = !labelsOn() ? ""
      : spk
      ? `<button type="button" class="speaker" data-speaker-btn style="--spk:${spk.color}" title="${esc(spk.name)}: click to change">${esc(spk.name)}</button>`
      : `<button type="button" class="speaker is-none" data-speaker-btn title="Set the speaker">+</button>`;
    return `<div class="line ${edited ? "is-edited" : ""}" id="line-${seg.id}" data-id="${seg.id}" data-speaker="${esc(speakerKey(seg, speakers))}">
      ${timed ? `<button type="button" class="ts" data-seek="${seg.start}" title="Play from here">${fmtTime(seg.start)}</button>` : `<span class="ts ts-static">${seg.id.replace(/^s0*/, "#")}</span>`}
      ${label}
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
    const trimNow = s.audio && s.audio.trim;
    const partial = t.range && (!trimNow || trimNow.start < t.range[0] - 0.5 || trimNow.end > t.range[1] + 0.5)
      ? `transcribed ${fmtTime(t.range[0])}–${fmtTime(t.range[1])} only: transcribe again to include the rest` : "";
    const metaBits = [t.model, t.language, t.device, t.accurate ? "beam search" : "", t.glossary_terms ? `glossary ${t.glossary_terms} terms` : t.glossary_used ? "glossary" : "", t.stopped ? "stopped early" : "",
      t.trimmed_count ? `${t.trimmed_count} lines outside the trim hidden` : "", partial];
    $("transcript-meta").textContent = `${list.length} lines · ${metaBits.filter(Boolean).join(" · ")}`;
    $("follow-audio").parentElement.hidden = !s.audio;

    if (!list.length) {
      container.innerHTML = `<p class="placeholder">${s.status === "transcribing" ? "Lines will appear here as Whisper works through the audio…" : t.completed_at ? "No speech was found." : "Transcription did not finish. Run it again."}</p>`;
      return;
    }
    const pending = pendingBySegment();
    const speakers = speakerIndex();
    // Speaker detection can split lines (new ids in the middle): simplest to redraw everything then.
    const order = list.map((seg) => seg.id).join(",");
    if (container.dataset.order && !order.startsWith(container.dataset.order) && !full) full = true;
    container.dataset.order = order;
    if ((container.dataset.labels || "") !== String(labelsOn())) full = true;
    container.dataset.labels = String(labelsOn());
    if (full || container.querySelector(".placeholder")) container.innerHTML = "";
    const existing = new Map([...container.children].map((el) => [el.dataset.id, el]));
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
    let appended = false;
    for (const seg of list) {
      const el = existing.get(seg.id);
      if (!el) {
        container.insertAdjacentHTML("beforeend", lineHtml(seg, pending, speakers));
        appended = true;
        continue;
      }
      const textEl = el.querySelector(".text");
      if (document.activeElement === textEl) continue; // never clobber the line being edited
      if ((el.dataset.speaker || "") !== speakerKey(seg, speakers)) {
        el.outerHTML = lineHtml(seg, pending, speakers);
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

  // ── Speakers ─────────────────────────────────────────────────────────
  // Colours follow the speaker id ("s3" is always the third colour), so they survive merges.
  const SPEAKER_COLORS = ["#7dd3fc", "#f9a8d4", "#86efac", "#fdba74", "#fde68a", "#fca5a5", "#67e8f9", "#d8b4fe", "#bef264", "#a5b4fc"];

  function speakerColor(key) {
    if (key === "me" || key === "Me") return "var(--accent)";
    const number = /^s(\d+)$/.exec(key);
    return SPEAKER_COLORS[number ? (Number(number[1]) - 1) % SPEAKER_COLORS.length : 0];
  }

  // Speakers in panel order; older sessions only have "Me"/"Others" on the lines.
  function speakerList() {
    const t = state.session.transcript;
    const map = (t && t.speaker_map) || {};
    const keys = Object.keys(map);
    for (const seg of segments()) if (seg.speaker && !keys.includes(seg.speaker)) keys.push(seg.speaker);
    return keys.map((key) => ({
      key,
      name: (map[key] && map[key].name) || key,
      isMe: key === "me" || key === "Me",
      color: speakerColor(key),
    }));
  }
  const speakerIndex = () => new Map(speakerList().map((spk) => [spk.key, spk]));

  function renderSpeakers() {
    const s = state.session;
    const t = s.transcript;
    const card = $("speakers-card");
    card.hidden = !(t && t.completed_at && segments().length);
    if (card.hidden) return closeSpeakerMenu();
    const canDetect = Boolean(s.audio) && t.source !== "import";
    const list = speakerList();
    const on = labelsOn();
    $("speaker-labels").checked = on;
    $("speaker-labels").disabled = busy();
    card.classList.toggle("is-off", !on);
    $("detect-speakers").hidden = $("detect-count").hidden = !canDetect || !on;
    $("add-speaker").hidden = !on;
    if (!on) {
      $("speakers-meta").textContent = "";
      $("speakers-hint").hidden = true;
      $("speaker-list").innerHTML = "";
      return closeSpeakerMenu();
    }
    $("detect-speakers").disabled = busy();
    $("detect-speakers").textContent = list.length ? "Detect again" : "Detect speakers";
    $("add-speaker").disabled = busy();
    $("speakers-hint").hidden = !list.length;
    $("speakers-meta").textContent = list.length ? `${list.length} speaker${list.length > 1 ? "s" : ""}` : "";

    const container = $("speaker-list");
    if (container.contains(document.activeElement) && document.activeElement.classList.contains("speaker-name")) return;
    if (!list.length) {
      const why = t.speaker_map ? "Only one voice was found, so the lines are not labelled." : "Speakers were not detected for this transcript.";
      container.innerHTML = `<p class="muted small speaker-empty">${why} ${canDetect ? "Click <b>Detect speakers</b>, or a" : "A"}dd speakers and assign lines with the <b>+</b> next to each line.</p>`;
      return;
    }
    const talk = new Map();
    const count = new Map();
    let total = 0;
    for (const seg of segments()) {
      const seconds = seg.start !== null && seg.start !== undefined ? Math.max(0, seg.end - seg.start) : 0;
      total += seconds;
      if (!seg.speaker) continue;
      talk.set(seg.speaker, (talk.get(seg.speaker) || 0) + seconds);
      count.set(seg.speaker, (count.get(seg.speaker) || 0) + 1);
    }
    const playIcon = `<svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v11l9-5.5z" fill="currentColor"/></svg>`;
    container.innerHTML = list
      .map((spk) => {
        const seconds = talk.get(spk.key) || 0;
        const lines = count.get(spk.key) || 0;
        const share = total ? Math.round((seconds / total) * 100) : 0;
        const others = list.filter((other) => other.key !== spk.key).map((other) => `<option value="${esc(other.key)}">${esc(other.name)}</option>`).join("");
        return `<div class="speaker-row" data-key="${esc(spk.key)}" style="--spk:${spk.color}">
          <span class="speaker-dot"></span>
          <div class="speaker-name-wrap">
            <input class="speaker-name" value="${esc(spk.name)}" maxlength="60" aria-label="Name of ${esc(spk.name)}" />
            ${spk.isMe ? `<span class="muted small">your microphone</span>` : ""}
          </div>
          <div class="talk">
            <div class="talk-bar"><span style="width:${share}%"></span></div>
            <span class="talk-text">${t.source === "import" ? "" : `${fmtTalk(seconds)} · ${share}% · `}${lines} line${lines === 1 ? "" : "s"}</span>
          </div>
          <div class="speaker-actions">
            ${canDetect && lines ? `<button type="button" class="icon-btn play-btn" data-sample title="Play a sample of this voice (again for another)" aria-label="Play a sample of ${esc(spk.name)}">${playIcon}</button>` : ""}
            ${others ? `<select data-merge aria-label="Merge ${esc(spk.name)} into another speaker"><option value="">Merge into…</option>${others}</select>` : ""}
            <button type="button" class="icon-btn" data-remove title="Remove this speaker${lines ? " (its lines become unlabelled)" : ""}" aria-label="Remove ${esc(spk.name)}">✕</button>
          </div>
        </div>`;
      })
      .join("");
    renderSpeakerPlaying();
  }

  function renderSpeakerPlaying() {
    document.querySelectorAll("#speaker-list .play-btn").forEach((button) =>
      button.classList.toggle("is-playing", state.stopAt !== null && button.closest(".speaker-row").dataset.key === state.sampleKey && !$("player").paused)
    );
  }

  // A few seconds of the speaker's voice; each click plays another of their (up to 5) best lines.
  function playSample(key) {
    const lines = segments().filter((seg) => seg.speaker === key && seg.start !== null && seg.start !== undefined);
    if (!lines.length) return;
    const ranked = [...lines].sort((a, b) => Math.abs(a.end - a.start - 6) - Math.abs(b.end - b.start - 6)).slice(0, 5);
    const turn = state.sampleTurn[key] || 0;
    state.sampleTurn[key] = turn + 1;
    const pick = ranked[turn % ranked.length];
    state.sampleKey = key;
    seek(pick.start, Math.min(pick.end, pick.start + 8));
    gotoLine(pick.id);
    renderSpeakerPlaying();
  }

  async function speakerRequest(path, options) {
    const session = await run(() => api(`/api/sessions/${sid()}${path}`, options));
    if (session) applySession(session);
    return session;
  }

  async function renameSpeaker(input) {
    const key = input.closest(".speaker-row").dataset.key;
    const spk = speakerIndex().get(key);
    const name = input.value.trim();
    if (!spk || !name || name === spk.name) {
      if (spk) input.value = spk.name;
      return;
    }
    await speakerRequest(`/speakers/${encodeURIComponent(key)}`, { method: "PATCH", json: { name } });
  }

  async function addSpeaker(segmentIds = []) {
    const name = prompt("Name of the new speaker:");
    if (name === null) return;
    await speakerRequest("/speakers", { method: "POST", json: { name: name.trim(), segments: segmentIds } });
  }

  async function toggleLabels(input) {
    const on = input.checked;
    const named = speakerList().some((spk) => !/^(Speaker \d+|Others|Me)$/.test(spk.name));
    if (!on && named && !confirm("Remove the speaker labels? The names you gave the speakers are lost.")) {
      input.checked = true;
      return;
    }
    const session = await speakerRequest("/speakers/labels", { method: "POST", json: { on, count: Number($("detect-count").value) || null } });
    if (!session) input.checked = !on;
  }

  async function detectSpeakers() {
    const named = speakerList().some((spk) => !spk.isMe && !/^(Speaker \d+|Others)$/.test(spk.name));
    if (speakerList().length && !confirm(`Detect the speakers again? The current speakers${named ? " and the names you gave them" : ""} are replaced${speakerList().some((spk) => spk.isMe) ? " (Me stays)" : ""}.`)) return;
    await speakerRequest("/speakers/detect", { method: "POST", json: { count: Number($("detect-count").value) || null } });
  }

  // Line label menu: pick a speaker for one line, or for a Shift-clicked range of lines.
  function openSpeakerMenu(button, shift) {
    const id = button.closest(".line").dataset.id;
    const order = segments().map((seg) => seg.id);
    let ids = [id];
    if (shift && state.rangeAnchor && order.includes(state.rangeAnchor)) {
      const [a, b] = [order.indexOf(state.rangeAnchor), order.indexOf(id)].sort((x, y) => x - y);
      ids = order.slice(a, b + 1);
    } else {
      state.rangeAnchor = id;
    }
    closeSpeakerMenu();
    ids.forEach((lineId) => $(`line-${lineId}`)?.classList.add("is-range"));
    button.classList.add("is-selected");

    const chosen = new Set(ids.map((lineId) => (segments().find((seg) => seg.id === lineId) || {}).speaker || ""));
    const menu = $("speaker-menu");
    menu.dataset.ids = ids.join(",");
    menu.innerHTML =
      `<div class="menu-label">${ids.length > 1 ? `${ids.length} lines` : "Speaker of this line"}</div>` +
      speakerList()
        .map((spk) => `<button type="button" role="menuitem" data-pick="${esc(spk.key)}" class="${chosen.size === 1 && chosen.has(spk.key) ? "is-current" : ""}" style="--spk:${spk.color}"><span class="menu-dot"></span>${esc(spk.name)}</button>`)
        .join("") +
      `<div class="menu-sep"></div><button type="button" role="menuitem" data-pick="__new">+ New speaker…</button>` +
      (chosen.size > 1 || !chosen.has("") ? `<button type="button" role="menuitem" data-pick="">No speaker</button>` : "");
    menu.hidden = false;
    const rect = button.getBoundingClientRect();
    const height = menu.offsetHeight;
    menu.style.left = `${Math.min(rect.left, window.innerWidth - menu.offsetWidth - 8)}px`;
    menu.style.top = `${rect.bottom + height + 8 > window.innerHeight ? Math.max(8, rect.top - height - 4) : rect.bottom + 4}px`;
    menu.querySelector("button")?.focus();
  }

  function closeSpeakerMenu() {
    const menu = $("speaker-menu");
    if (menu.hidden) return;
    menu.hidden = true;
    document.querySelectorAll(".line.is-range").forEach((el) => el.classList.remove("is-range"));
    document.querySelectorAll(".speaker.is-selected").forEach((el) => el.classList.remove("is-selected"));
  }

  async function pickSpeaker(key) {
    const ids = $("speaker-menu").dataset.ids.split(",");
    closeSpeakerMenu();
    if (key === "__new") return addSpeaker(ids);
    await speakerRequest("/segments-speaker", { method: "POST", json: { segments: ids, speaker: key || null } });
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
    $("analysis-meta").textContent = a
      ? `${a.model}${a.mode === "local" ? " (no API key: basic local summary)" : ""}${a.outdated ? " · made before the trim: run again" : ""}`
      : "";
    $("copy-analysis").hidden = !a || s.status === "analyzing";
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

  // Plain text with "- " bullets: pastes cleanly into email, Teams, Word.
  function analysisText(a) {
    const section = (title, items) => (items && items.length ? `\n\n${title}\n${items.map((i) => `- ${i}`).join("\n")}` : "");
    return `${state.session.name}\n\nSummary\n${a.summary}` +
      section("Key points", a.key_points) + section("Action items", a.action_items) + section("Open questions", a.open_questions) + "\n";
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Fallback for contexts without the async clipboard API.
      const area = Object.assign(document.createElement("textarea"), { value: text });
      area.style.cssText = "position:fixed;opacity:0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      if (!ok) throw new Error("Could not access the clipboard.");
    }
  }

  async function copyAnalysis() {
    const a = state.session.analysis;
    if (!a) return;
    const label = $("copy-analysis").querySelector("span");
    try {
      await copyText(analysisText(a));
      label.textContent = "Copied";
      setTimeout(() => (label.textContent = "Copy"), 1500);
    } catch (error) {
      toast(error.message, "error");
    }
  }

  // ── Render all ───────────────────────────────────────────────────────
  function renderAll({ fullTranscript = false } = {}) {
    renderHeader();
    renderSidebar();
    renderInput();
    renderTranscribeCard();
    renderJob();
    renderSpeakers();
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
    applySession(session);
    toast(session.audio ? "File added. Check the transcription settings, then click Transcribe." : "Transcript imported. You can review it now.");
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
          speakers: $("opt-speakers").checked,
          speaker_count: Number($("opt-speaker-count").value) || 0,
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

  function seek(seconds, stopAt = null) {
    const player = $("player");
    if (!state.session.audio || Number.isNaN(seconds)) return;
    state.stopAt = stopAt;
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
    $("set-speakers").checked = s.detect_speakers !== false;
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
      detect_speakers: $("set-speakers").checked,
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
    $("export-audio").addEventListener("click", exportAudio);
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
    $("refresh-devices").addEventListener("click", async () => {
      const button = $("refresh-devices");
      button.disabled = true;
      await loadDevices({ refresh: true }); // re-renders the controls, re-enabling the button
    });
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
      const speakerBtn = e.target.closest("[data-speaker-btn]");
      if (speakerBtn) return openSpeakerMenu(speakerBtn, e.shiftKey);
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

    // ⋮ menu
    $("audio-menu-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleAudioMenu(); });
    $("menu-trim").addEventListener("click", openTrim);
    $("menu-speeds").addEventListener("click", (e) => {
      const button = e.target.closest("[data-rate]");
      if (button) setPlaybackRate(Number(button.dataset.rate));
    });
    document.addEventListener("click", (e) => {
      if (!$("audio-menu").hidden && !e.target.closest(".menu-wrap")) closeAudioMenu();
      if (!$("speaker-menu").hidden && !e.target.closest("#speaker-menu") && !e.target.closest("[data-speaker-btn]")) closeSpeakerMenu();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      closeAudioMenu();
      closeSpeakerMenu();
    });

    // Trim
    const wave = $("trim-wave");
    wave.addEventListener("pointerdown", (e) => {
      if (!trim.draft) return;
      const handle = e.target.closest(".trim-handle");
      if (handle) {
        trim.drag = handle.dataset.edge;
        wave.setPointerCapture(e.pointerId);
        e.preventDefault();
      } else {
        const player = $("player");
        player.currentTime = timeAt(e.clientX);
        updatePlayhead();
      }
    });
    wave.addEventListener("pointermove", (e) => { if (trim.drag) setTrimEdge(trim.drag, timeAt(e.clientX)); });
    const endDrag = () => { trim.drag = null; };
    wave.addEventListener("pointerup", endDrag);
    wave.addEventListener("pointercancel", endDrag);
    document.querySelectorAll(".trim-handle").forEach((handle) =>
      handle.addEventListener("keydown", (e) => {
        if (!trim.draft || !["ArrowLeft", "ArrowRight"].includes(e.key)) return;
        e.preventDefault();
        const step = (e.shiftKey ? 10 : 1) * (e.key === "ArrowLeft" ? -1 : 1);
        setTrimEdge(handle.dataset.edge, trim.draft[handle.dataset.edge] + step);
      })
    );
    for (const edge of ["start", "end"]) {
      const input = $(`trim-${edge}`);
      const apply = () => {
        const seconds = parseTime(input.value);
        if (seconds === null) toast("Type a time like 2:15 or 1:02:03.", "error");
        else setTrimEdge(edge, seconds);
        input.value = fmtPrecise(trim.draft[edge]);
      };
      input.addEventListener("change", apply);
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
      $(`trim-${edge}-here`).addEventListener("click", () => setTrimEdge(edge, $("player").currentTime));
    }
    $("trim-play").addEventListener("click", () => trim.draft && seek(trim.draft.start, trim.draft.end));
    $("trim-cancel").addEventListener("click", closeTrim);
    $("trim-save").addEventListener("click", () => saveTrim());
    $("trim-reset").addEventListener("click", () => saveTrim(true));
    window.addEventListener("resize", drawWave);

    // Speakers
    fillSpeakerCount($("detect-count"));
    fillSpeakerCount($("opt-speaker-count"));
    $("opt-speakers").addEventListener("change", () => ($("opt-speaker-count").disabled = !$("opt-speakers").checked));
    $("detect-speakers").addEventListener("click", detectSpeakers);
    $("speaker-labels").addEventListener("change", (e) => toggleLabels(e.target));
    $("add-speaker").addEventListener("click", () => addSpeaker());
    const speakerListEl = $("speaker-list");
    speakerListEl.addEventListener("click", async (e) => {
      const row = e.target.closest(".speaker-row");
      if (!row) return;
      const key = row.dataset.key;
      if (e.target.closest("[data-sample]")) return playSample(key);
      if (e.target.closest("[data-remove]")) {
        const spk = speakerIndex().get(key);
        const lines = segments().filter((seg) => seg.speaker === key).length;
        if (lines && !confirm(`Remove ${spk.name}? Their ${lines} line(s) will have no speaker.`)) return;
        await speakerRequest(`/speakers/${encodeURIComponent(key)}`, { method: "DELETE" });
      }
    });
    speakerListEl.addEventListener("change", async (e) => {
      if (e.target.classList.contains("speaker-name")) return renameSpeaker(e.target);
      if (!e.target.matches("[data-merge]") || !e.target.value) return;
      const index = speakerIndex();
      const from = index.get(e.target.closest(".speaker-row").dataset.key);
      const into = index.get(e.target.value);
      e.target.value = "";
      if (!confirm(`Merge ${from.name} into ${into.name}? All of ${from.name}'s lines become ${into.name}'s.`)) return;
      await speakerRequest(`/speakers/${encodeURIComponent(from.key)}/merge`, { method: "POST", json: { into: into.key } });
    });
    speakerListEl.addEventListener("keydown", (e) => {
      if (!e.target.classList.contains("speaker-name")) return;
      if (e.key === "Enter") e.target.blur();
      if (e.key === "Escape") { e.target.value = (speakerIndex().get(e.target.closest(".speaker-row").dataset.key) || {}).name || ""; e.target.blur(); }
    });
    $("speaker-menu").addEventListener("click", (e) => {
      const button = e.target.closest("[data-pick]");
      if (button) pickSpeaker(button.dataset.pick);
    });

    const player = $("player");
    player.addEventListener("play", () => {
      const range = playRange();
      if (range && (player.currentTime < range.start || player.currentTime >= range.end - 0.1)) player.currentTime = range.start;
      renderSpeakerPlaying();
    });
    player.addEventListener("pause", renderSpeakerPlaying);
    player.addEventListener("seeked", keepInRange);
    player.addEventListener("timeupdate", onPlayerTime);
    // A seek by hand (native scrubber, timestamp) cancels a pending sample stop.
    player.addEventListener("seeking", () => { if (state.stopAt !== null && (player.currentTime < state.stopAt - 30 || player.currentTime > state.stopAt)) state.stopAt = null; });

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
    $("copy-analysis").addEventListener("click", copyAnalysis);
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
