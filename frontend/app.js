// Relative path everywhere this frontend shares an origin with the API —
// local dev, and the copy Render's own StaticFiles serves alongside its
// backend. Only the copy actually split out onto Vercel is truly
// cross-origin, so that's the only case that needs the backend's full URL.
const RENDER_API_ORIGIN = "https://meeting-summarizer-xr53.onrender.com";
const API_ORIGIN = window.location.hostname.endsWith(".vercel.app") ? RENDER_API_ORIGIN : "";
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
        statusText.textContent = STAGE_TEXT[body.stage] || "Processing…";
      } else if (body.status === "QUEUED") {
        statusText.textContent = "Queued…";
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
  for (const seg of meeting.segments) {
    const row = document.createElement("div");
    row.className = "seg";
    row.innerHTML = `<span class="seg-time">${fmtTime(seg.start)}</span><span>${escapeHtml(seg.text)}</span>`;
    box.appendChild(row);
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
    answerBox.textContent = response.ok ? body.answer : body.detail || "Could not get an answer.";
  } catch (err) {
    answerBox.textContent = "Could not reach the server.";
  } finally {
    button.disabled = false;
  }
});

// ---- record in the browser ----
const recordBtn = document.getElementById("record-btn");
const recordTimer = document.getElementById("record-timer");
let recorder = null;
let recordStarted = 0;
let recordTick = null;

function stopRecordingUi() {
  clearInterval(recordTick);
  recordBtn.classList.remove("is-recording");
  recordBtn.textContent = "● Record a meeting";
  recordTimer.hidden = true;
}

recordBtn.addEventListener("click", async () => {
  if (recorder && recorder.state === "recording") {
    recorder.stop();
    return;
  }
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    uploadError.textContent = "Recording isn't supported in this browser — upload a file instead.";
    uploadError.hidden = false;
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      stopRecordingUi();
      const type = (recorder.mimeType || "audio/webm").split(";")[0];
      const ext = type.includes("ogg") ? "ogg" : type.includes("mp4") ? "mp4" : "webm";
      const file = new File(chunks, `recording-${new Date().toISOString().slice(0, 16).replace(/[:T]/g, "-")}.${ext}`, { type });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      audioInput.files = transfer.files;
      showSelectedFile(file);
    };
    recorder.start(1000);
    recordStarted = Date.now();
    recordBtn.classList.add("is-recording");
    recordBtn.textContent = "■ Stop recording";
    recordTimer.hidden = false;
    recordTick = setInterval(() => {
      recordTimer.textContent = fmtTime((Date.now() - recordStarted) / 1000);
    }, 500);
  } catch (err) {
    uploadError.textContent = "Couldn't access the microphone. Allow microphone access and try again.";
    uploadError.hidden = false;
  }
});

loadHistory();
