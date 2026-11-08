// The API serves this frontend, so every call is same-origin. If you host
// the frontend separately, set this to the API's URL (and ALLOWED_ORIGINS on the server).
const API_ORIGIN = "";
const API_BASE = `${API_ORIGIN}/api/v1/meetings`;
const POLL_INTERVAL_MS = 2000;
const PAGE_SIZE = 20;

// Private-by-default without accounts: a random token made once per browser
// identifies "my" meetings. The server only ever stores a hash of it.
function ownerToken() {
  let token = null;
  try {
    token = localStorage.getItem("owner-token");
  } catch (err) {
    // storage blocked — fall through to a per-page token
  }
  if (!token) {
    const bytes = crypto.getRandomValues(new Uint8Array(24));
    token = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    try {
      localStorage.setItem("owner-token", token);
    } catch (err) {
      // not persisted; meetings from this page load won't be visible after a reload
    }
  }
  return token;
}
const OWNER_TOKEN = ownerToken();

function api(url, options = {}) {
  return fetch(url, { ...options, headers: { ...(options.headers || {}), "X-Owner-Token": OWNER_TOKEN } });
}

const STAGE_TEXT = {
  starting: "Starting…",
  compressing: "Preparing the audio…",
  transcribing: "Transcribing the recording…",
  summarizing: "Writing the summary…",
};
function stageText(stage) {
  if (!stage) return null;
  if (STAGE_TEXT[stage]) return STAGE_TEXT[stage];
  return stage.charAt(0).toUpperCase() + stage.slice(1) + "…"; // e.g. "Summarizing section 2 of 5…"
}

let currentMeetingId = null;
let currentMeeting = null;

const uploadForm = document.getElementById("upload-form");
const audioInput = document.getElementById("audio-input");
const uploadBtn = document.getElementById("upload-btn");
const uploadError = document.getElementById("upload-error");
const dropzone = document.getElementById("dropzone");
const dropzoneText = document.getElementById("dropzone-text");
const DEFAULT_DROPZONE_HTML = dropzoneText.innerHTML;

const uploadSection = document.getElementById("upload-section");
const statusSection = document.getElementById("status-section");
const statusText = document.getElementById("status-text");
const resultSection = document.getElementById("result-section");
const errorSection = document.getElementById("error-section");
const failureMessage = document.getElementById("failure-message");

const themeToggle = document.getElementById("theme-toggle");

const historyList = document.getElementById("history-list");
const historyTab = document.getElementById("history-tab");
const historyPanel = document.getElementById("history-panel");
const historyOverlay = document.getElementById("history-overlay");
const historyCloseBtn = document.getElementById("history-close-btn");

let pollTimer = null;

function effectiveTheme() {
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit) return explicit;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function updateToggleIcon(theme) {
  themeToggle.classList.toggle("is-dark", theme === "dark");
  themeToggle.setAttribute("aria-label", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
}

themeToggle.addEventListener("click", () => {
  const next = effectiveTheme() === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  try {
    localStorage.setItem("theme", next);
  } catch (err) {
    // localStorage unavailable — theme just won't persist past this page load.
  }
  updateToggleIcon(next);
});

updateToggleIcon(effectiveTheme());

function openHistory() {
  historyPanel.hidden = false;
  historyOverlay.hidden = false;
  loadHistory();
}

function closeHistory() {
  historyPanel.hidden = true;
  historyOverlay.hidden = true;
}

historyTab.addEventListener("click", openHistory);
historyCloseBtn.addEventListener("click", closeHistory);
historyOverlay.addEventListener("click", closeHistory);

function showOnly(section) {
  for (const s of [uploadSection, statusSection, resultSection, errorSection]) {
    s.hidden = s !== section;
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

function timeAgo(isoString) {
  const diffMinutes = Math.round((Date.now() - new Date(isoString).getTime()) / 60000);
  if (diffMinutes < 1) return "just now";
  if (diffMinutes < 60) return `${diffMinutes}m ago`;
  const diffHours = Math.round(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours}h ago`;
  const diffDays = Math.round(diffHours / 24);
  return `${diffDays}d ago`;
}

function showSelectedFile(file) {
  dropzoneText.innerHTML = file ? escapeHtml(file.name) : DEFAULT_DROPZONE_HTML;
}

function resetToUpload() {
  clearInterval(pollTimer);
  uploadForm.reset();
  showSelectedFile(null);
  uploadError.hidden = true;
  showOnly(uploadSection);
  loadHistory();
}

audioInput.addEventListener("change", () => {
  showSelectedFile(audioInput.files[0]);
});

for (const eventName of ["dragover", "dragenter"]) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragover");
  });
}

for (const eventName of ["dragleave", "dragend"]) {
  dropzone.addEventListener(eventName, () => {
    dropzone.classList.remove("is-dragover");
  });
}

dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  dropzone.classList.remove("is-dragover");
  const file = event.dataTransfer.files[0];
  if (file) {
    audioInput.files = event.dataTransfer.files;
    showSelectedFile(file);
  }
});

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = audioInput.files[0];
  if (!file) return;

  uploadError.hidden = true;
  uploadBtn.disabled = true;
  uploadBtn.textContent = "Uploading…";

  try {
    const formData = new FormData();
    formData.append("audio", file);

    const response = await api(API_BASE, { method: "POST", body: formData });
    const body = await response.json();

    if (!response.ok) {
      uploadError.textContent = body.detail || "Upload failed.";
      uploadError.hidden = false;
      return;
    }

    showOnly(statusSection);
    pollStatus(body.id);
  } catch (err) {
    uploadError.textContent = "Could not reach the server. Is it running?";
    uploadError.hidden = false;
  } finally {
    uploadBtn.disabled = false;
    uploadBtn.textContent = "Upload & Summarize";
  }
});

function pollStatus(meetingId) {
  clearInterval(pollTimer);
  statusText.textContent = "Queued…";

  pollTimer = setInterval(async () => {
    try {
      const response = await api(`${API_BASE}/${meetingId}/status`);
      if (!response.ok) throw new Error("status check failed");
      const body = await response.json();

      if (body.status === "PROCESSING") {
        statusText.textContent = stageText(body.stage) || "Processing…";
      } else if (body.status === "QUEUED") {
        statusText.textContent = body.stage === "waiting for AI capacity"
          ? "The AI service is busy — your meeting is queued and will continue automatically…"
          : "Queued…";
      } else if (body.status === "COMPLETED") {
        clearInterval(pollTimer);
        await loadResult(meetingId);
      } else if (body.status === "FAILED") {
        clearInterval(pollTimer);
        failureMessage.textContent = body.error_message || "Processing failed.";
        showOnly(errorSection);
      }
    } catch (err) {
      clearInterval(pollTimer);
      failureMessage.textContent = "Lost connection to the server while checking status.";
      showOnly(errorSection);
    }
  }, POLL_INTERVAL_MS);
}

async function loadResult(meetingId) {
  const response = await api(`${API_BASE}/${meetingId}`);
  const meeting = await response.json();
  renderResult(meeting);
  showOnly(resultSection);
  loadHistory();
}

const ICON_PERSON =
  '<svg class="meta-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="3.5" /><path d="M5 20c0-3.5 3-6 7-6s7 2.5 7 6" /></svg>';
const ICON_CALENDAR =
  '<svg class="meta-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="5" width="16" height="15" rx="2" /><path d="M8 3v4M16 3v4M4 10h16" /></svg>';

function renderStats(meeting) {
  const openQuestions = meeting.open_questions || [];
  const counts = [
    { count: meeting.key_decisions.length, label: "decision" },
    { count: meeting.action_items.length, label: "action item" },
    { count: openQuestions.length, label: "open question" },
  ];
  document.getElementById("result-stats").innerHTML = counts
    .map((c) => `<span class="stat-pill"><strong>${c.count}</strong> ${c.label}${c.count === 1 ? "" : "s"}</span>`)
    .join("");
  renderUsage(meeting.usage || {});
}

// What this meeting cost in AI resources, and what the pipeline saved.
function renderUsage(usage) {
  const el = document.getElementById("result-usage");
  const parts = [];
  const audio = usage.audio_seconds || 0;
  const skipped = audio - (usage.audio_sent_seconds ?? audio);
  if (skipped >= 1) {
    parts.push(`Skipped ${fmtDuration(skipped)} of silence (${Math.round((skipped / audio) * 100)}% less audio to transcribe)`);
  }
  if (usage.reused_result) parts.push("Same audio as an earlier upload — reused its notes, no AI used");
  else if (usage.llm_tokens) parts.push(`${usage.llm_tokens.toLocaleString()} AI tokens`);
  if (usage.presummarized_sections) {
    const n = usage.presummarized_sections;
    parts.push(`${n} section${n === 1 ? "" : "s"} summarized during the recording`);
  }
  el.textContent = parts.join(" · ");
  el.hidden = parts.length === 0;
}

function fmtDuration(seconds) {
  const s = Math.round(seconds);
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}

function fmtTime(seconds) {
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const mm = String(m).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function renderTranscript(meeting) {
  const box = document.getElementById("result-transcript");
  box.innerHTML = "";
  if (!meeting.segments || meeting.segments.length === 0) {
    box.textContent = meeting.transcript || "—";
    return;
  }
  meeting.segments.forEach((seg, i) => {
    const row = document.createElement("div");
    row.className = "seg";
    row.dataset.start = seg.start;
    row.innerHTML = `<span class="seg-time">${fmtTime(seg.start)}</span><span>${escapeHtml(seg.text)}</span>`;
    box.appendChild(row);
  });
}

function jumpToTime(seconds) {
  const details = document.querySelector(".transcript-details");
  details.open = true;
  const rows = [...document.querySelectorAll("#result-transcript .seg")];
  const target = rows.filter((r) => Number(r.dataset.start) <= seconds + 0.01).pop() || rows[0];
  if (!target) return;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.add("is-flash");
  setTimeout(() => target.classList.remove("is-flash"), 1600);
}

function renderChapters(meeting) {
  const block = document.getElementById("chapters-block");
  const list = document.getElementById("result-chapters");
  list.innerHTML = "";
  const chapters = meeting.chapters || [];
  block.hidden = chapters.length < 2;
  for (const ch of chapters) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="seg-time">${fmtTime(ch.start)}</span><span class="chapter-title">${escapeHtml(ch.title)}</span>`;
    li.addEventListener("click", () => jumpToTime(ch.start));
    list.appendChild(li);
  }
}

function renderAnswer(box, body, { openOtherMeetings }) {
  box.innerHTML = "";
  const text = document.createElement("div");
  text.textContent = body.answer;
  box.appendChild(text);
  const seen = new Set();
  const chips = document.createElement("div");
  chips.className = "sources";
  for (const src of body.sources || []) {
    const key = `${src.meeting_id}:${Math.floor(src.start)}`;
    if (seen.has(key) || seen.size >= 6) continue;
    seen.add(key);
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "source-chip";
    chip.textContent = openOtherMeetings ? `${src.title || "Meeting"} · ${fmtTime(src.start)}` : fmtTime(src.start);
    chip.addEventListener("click", async () => {
      if (openOtherMeetings) await loadResult(src.meeting_id);
      jumpToTime(src.start);
    });
    chips.appendChild(chip);
  }
  if (chips.children.length) box.appendChild(chips);
  if (body.cached) {
    const note = document.createElement("div");
    note.className = "answer-note";
    note.textContent = "Asked before — answered instantly from cache, no AI tokens used.";
    box.appendChild(note);
  }
}

function renderResult(meeting) {
  currentMeetingId = meeting.id;
  currentMeeting = meeting;
  document.getElementById("ask-answer").hidden = true;
  document.getElementById("ask-input").value = "";
  document.getElementById("result-title").textContent = meeting.title || meeting.filename;
  document.getElementById("result-filename").textContent = meeting.filename;
  document.getElementById("result-summary").textContent = meeting.summary || "—";
  renderStats(meeting);

  const decisionsList = document.getElementById("result-decisions");
  decisionsList.innerHTML = "";
  if (meeting.key_decisions.length === 0) {
    decisionsList.innerHTML = '<li class="muted">No decisions recorded.</li>';
  }
  for (const decision of meeting.key_decisions) {
    const li = document.createElement("li");
    li.textContent = decision;
    decisionsList.appendChild(li);
  }

  const actionsList = document.getElementById("result-actions");
  actionsList.innerHTML = "";
  if (meeting.action_items.length === 0) {
    actionsList.innerHTML = '<li class="muted">No action items recorded.</li>';
  }
  for (const item of meeting.action_items) {
    const li = document.createElement("li");
    const assignee = item.assignee || "Unassigned";
    const deadline = item.deadline || "No deadline";
    li.innerHTML =
      `<input type="checkbox" disabled /> ` +
      `<div class="action-body">` +
      `<span class="action-task">${escapeHtml(item.task)}</span>` +
      `<span class="action-meta">` +
      `<span class="meta-chip">${ICON_PERSON}${escapeHtml(assignee)}</span>` +
      `<span class="meta-chip">${ICON_CALENDAR}${escapeHtml(deadline)}</span>` +
      `</span>` +
      `</div>`;
    actionsList.appendChild(li);
  }

  const questionsList = document.getElementById("result-open-questions");
  questionsList.innerHTML = "";
  const openQuestions = meeting.open_questions || [];
  if (openQuestions.length === 0) {
    questionsList.innerHTML = '<li class="muted">No open questions recorded.</li>';
  }
  for (const question of openQuestions) {
    const li = document.createElement("li");
    li.textContent = question;
    questionsList.appendChild(li);
  }

  renderTranscript(meeting);
  renderChapters(meeting);
}

document.getElementById("new-upload-btn").addEventListener("click", resetToUpload);
document.getElementById("retry-btn").addEventListener("click", resetToUpload);

const historySearch = document.getElementById("history-search");
const historyMore = document.getElementById("history-more");
let historyOffset = 0;
let searchTimer = null;

function renderSnippet(snippet) {
  // The server marks matches with [[ ]]; escape everything else first.
  return escapeHtml(snippet).replace(/\[\[/g, "<mark>").replace(/\]\]/g, "</mark>");
}

async function loadHistory(append = false) {
  try {
    if (!append) historyOffset = 0;
    const q = historySearch.value.trim();
    const params = new URLSearchParams({ limit: PAGE_SIZE, offset: historyOffset });
    if (q) params.set("q", q);
    const response = await api(`${API_BASE}?${params}`);
    const meetings = await response.json();
    if (!append) historyList.innerHTML = "";

    if (meetings.length === 0 && !append) {
      historyList.innerHTML = `<li class="muted">${q ? "No matches." : "No meetings yet."}</li>`;
    }

    for (const meeting of meetings) {
      const li = document.createElement("li");
      li.className = "history-item";
      li.innerHTML =
        `<span class="history-main">` +
        `<span class="history-title">${escapeHtml(meeting.title || meeting.filename)}</span>` +
        `<span class="history-time">${timeAgo(meeting.created_at)}</span>` +
        (meeting.snippet ? `<span class="history-snippet">${renderSnippet(meeting.snippet)}</span>` : "") +
        `</span>` +
        `<span class="badge badge-${meeting.status.toLowerCase()}">${meeting.status}</span>`;
      li.addEventListener("click", () => openMeeting(meeting));
      historyList.appendChild(li);
    }
    historyOffset += meetings.length;
    historyMore.hidden = meetings.length < PAGE_SIZE;
  } catch (err) {
    // History is a nice-to-have; fail silently if the server is briefly unreachable.
  }
}

historySearch.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadHistory(false), 250);
});
historyMore.addEventListener("click", () => loadHistory(true));

async function openMeeting(meeting) {
  if (meeting.status === "COMPLETED") {
    await loadResult(meeting.id);
  } else if (meeting.status === "RECORDING") {
    failureMessage.textContent = "This recording was never finished, so there's nothing to summarize.";
    showOnly(errorSection);
  } else if (meeting.status === "FAILED") {
    const response = await api(`${API_BASE}/${meeting.id}`);
    const detail = await response.json();
    failureMessage.textContent = detail.error_message || "Processing failed.";
    showOnly(errorSection);
  } else {
    showOnly(statusSection);
    pollStatus(meeting.id);
  }
}

// ---- result actions ----
document.getElementById("export-btn").addEventListener("click", async () => {
  const response = await api(`${API_BASE}/${currentMeetingId}/export`);
  if (!response.ok) return;
  const blob = await response.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${(currentMeeting.title || "meeting").replace(/[^\w-]+/g, "-")}.md`;
  link.click();
  URL.revokeObjectURL(link.href);
});

document.getElementById("copy-btn").addEventListener("click", async (event) => {
  const m = currentMeeting;
  const text = [
    m.title || m.filename,
    "",
    m.summary || "",
    "",
    "Decisions:",
    ...m.key_decisions.map((d) => `- ${d}`),
    "",
    "Action items:",
    ...m.action_items.map((a) => `- ${a.task} (${a.assignee || "unassigned"}, ${a.deadline || "no deadline"})`),
  ].join("\n");
  try {
    await navigator.clipboard.writeText(text);
    event.target.textContent = "Copied ✓";
    setTimeout(() => (event.target.textContent = "Copy summary"), 1500);
  } catch (err) {
    event.target.textContent = "Copy failed";
  }
});

document.getElementById("delete-btn").addEventListener("click", async () => {
  if (!confirm("Delete this meeting and its transcript? This can't be undone.")) return;
  const response = await api(`${API_BASE}/${currentMeetingId}`, { method: "DELETE" });
  if (response.ok) resetToUpload();
});

// ---- ask this meeting ----
const askForm = document.getElementById("ask-form");
askForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("ask-input");
  const answerBox = document.getElementById("ask-answer");
  const button = document.getElementById("ask-btn");
  button.disabled = true;
  answerBox.hidden = false;
  answerBox.textContent = "Thinking…";
  try {
    const response = await api(`${API_BASE}/${currentMeetingId}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: input.value }),
    });
    const body = await response.json();
    if (response.ok) renderAnswer(answerBox, body, { openOtherMeetings: false });
    else answerBox.textContent = body.detail || "Could not get an answer.";
  } catch (err) {
    answerBox.textContent = "Could not reach the server.";
  } finally {
    button.disabled = false;
  }
});

// ---- ask across all meetings ----
document.getElementById("ask-all-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.getElementById("ask-all-input");
  const box = document.getElementById("ask-all-answer");
  const button = document.getElementById("ask-all-btn");
  button.disabled = true;
  box.hidden = false;
  box.textContent = "Searching your meetings…";
  try {
    const response = await api(`${API_ORIGIN}/api/v1/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: input.value }),
    });
    const body = await response.json();
    if (response.ok) renderAnswer(box, body, { openOtherMeetings: true });
    else box.textContent = body.detail || "Could not get an answer.";
  } catch (err) {
    box.textContent = "Could not reach the server.";
  } finally {
    button.disabled = false;
  }
});

// ---- live recording ----
// The recorder is restarted every PIECE_SECONDS so each piece is a complete,
// independently decodable file. Each piece is uploaded (and transcribed by
// the server) while recording continues, so stopping only leaves the summary.
const PIECE_SECONDS = 45;
const RECORDINGS_API = `${API_ORIGIN}/api/v1/recordings`;
const recordBtn = document.getElementById("record-btn");
const recordTimer = document.getElementById("record-timer");
let live = null;

async function uploadPiece(recordingId, seq, offset, blob, ext) {
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const form = new FormData();
      form.append("seq", seq);
      form.append("offset", offset.toFixed(2));
      form.append("audio", new File([blob], `piece-${seq}.${ext}`, { type: blob.type }));
      const response = await api(`${RECORDINGS_API}/${recordingId}/chunks`, { method: "POST", body: form });
      if (response.ok) return (await response.json()).transcribed;
      if (response.status < 500 && response.status !== 429) return false; // won't succeed on retry
    } catch (err) {
      // network blip — retry below
    }
    await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
  }
  return false;
}

function updateLiveLabel() {
  const elapsed = (Date.now() - live.startedAt) / 1000;
  recordTimer.textContent = `${fmtTime(elapsed)} · ${live.done} piece${live.done === 1 ? "" : "s"} transcribed`;
}

function startPiece() {
  const recorder = new MediaRecorder(live.stream);
  const parts = [];
  const seq = live.seq++;
  const offset = (Date.now() - live.startedAt) / 1000;
  recorder.ondataavailable = (e) => e.data.size && parts.push(e.data);
  recorder.onstop = () => {
    const type = (recorder.mimeType || "audio/webm").split(";")[0];
    const ext = type.includes("ogg") ? "ogg" : type.includes("mp4") ? "mp4" : "webm";
    const blob = new Blob(parts, { type });
    if (blob.size > 0) {
      live.pending.push(
        uploadPiece(live.id, seq, offset, blob, ext).then((ok) => {
          if (ok) live.done++;
        }),
      );
    }
  };
  recorder.start();
  live.recorder = recorder;
}

async function startLiveRecording() {
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    uploadError.textContent = "Recording isn't supported in this browser — upload a file instead.";
    uploadError.hidden = false;
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    uploadError.textContent = "Couldn't access the microphone. Allow microphone access and try again.";
    uploadError.hidden = false;
    return;
  }
  const response = await api(RECORDINGS_API, { method: "POST" });
  if (!response.ok) {
    stream.getTracks().forEach((t) => t.stop());
    uploadError.textContent = (await response.json()).detail || "Couldn't start a recording.";
    uploadError.hidden = false;
    return;
  }
  live = { id: (await response.json()).id, stream, seq: 0, done: 0, pending: [], startedAt: Date.now() };
  uploadError.hidden = true;
  startPiece();
  live.rotate = setInterval(() => {
    live.recorder.stop();
    startPiece();
  }, PIECE_SECONDS * 1000);
  live.tick = setInterval(updateLiveLabel, 500);
  recordBtn.classList.add("is-recording");
  recordBtn.textContent = "■ Stop recording";
  recordTimer.hidden = false;
  updateLiveLabel();
}

async function stopLiveRecording() {
  const session = live;
  clearInterval(session.rotate);
  clearInterval(session.tick);
  recordBtn.disabled = true;
  recordBtn.textContent = "Finishing…";
  await new Promise((resolve) => {
    session.recorder.addEventListener("stop", resolve, { once: true });
    session.recorder.stop();
  });
  session.stream.getTracks().forEach((t) => t.stop());
  await Promise.all(session.pending);
  live = null;
  recordBtn.disabled = false;
  recordBtn.classList.remove("is-recording");
  recordBtn.textContent = "● Record a meeting";
  recordTimer.hidden = true;

  const response = await api(`${RECORDINGS_API}/${session.id}/finish`, { method: "POST" });
  const body = await response.json();
  if (!response.ok) {
    uploadError.textContent = body.detail || "Couldn't finish the recording.";
    uploadError.hidden = false;
    return;
  }
  showOnly(statusSection);
  pollStatus(session.id);
}

recordBtn.addEventListener("click", () => (live ? stopLiveRecording() : startLiveRecording()));

loadHistory();
