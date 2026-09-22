// Relative path everywhere this frontend shares an origin with the API —
// local dev, and the copy Render's own StaticFiles serves alongside its
// backend. Only the copy actually split out onto Vercel is truly
// cross-origin, so that's the only case that needs the backend's full URL.
const RENDER_API_ORIGIN = "https://meeting-summarizer-xr53.onrender.com";
const API_ORIGIN = window.location.hostname.endsWith(".vercel.app") ? RENDER_API_ORIGIN : "";
const API_BASE = `${API_ORIGIN}/api/v1/meetings`;
const POLL_INTERVAL_MS = 2000;

const uploadForm = document.getElementById("upload-form");
const audioInput = document.getElementById("audio-input");
const uploadBtn = document.getElementById("upload-btn");
const uploadError = document.getElementById("upload-error");

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

function resetToUpload() {
  clearInterval(pollTimer);
  uploadForm.reset();
  uploadError.hidden = true;
  showOnly(uploadSection);
  loadHistory();
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

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

    const response = await fetch(API_BASE, { method: "POST", body: formData });
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
      const response = await fetch(`${API_BASE}/${meetingId}/status`);
      if (!response.ok) throw new Error("status check failed");
      const body = await response.json();

      if (body.status === "PROCESSING") {
        statusText.textContent = "Processing — transcribing and summarizing…";
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
  const response = await fetch(`${API_BASE}/${meetingId}`);
  const meeting = await response.json();
  renderResult(meeting);
  showOnly(resultSection);
  loadHistory();
}

function renderResult(meeting) {
  document.getElementById("result-title").textContent = meeting.title || meeting.filename;
  document.getElementById("result-filename").textContent = meeting.filename;
  document.getElementById("result-summary").textContent = meeting.summary || "—";

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
      `<input type="checkbox" disabled /> <span>${escapeHtml(item.task)}</span>` +
      `<span class="meta">${escapeHtml(assignee)} · ${escapeHtml(deadline)}</span>`;
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

  document.getElementById("result-transcript").textContent = meeting.transcript || "—";
}

document.getElementById("new-upload-btn").addEventListener("click", resetToUpload);
document.getElementById("retry-btn").addEventListener("click", resetToUpload);

async function loadHistory() {
  try {
    const response = await fetch(API_BASE);
    const meetings = await response.json();
    historyList.innerHTML = "";

    if (meetings.length === 0) {
      historyList.innerHTML = '<li class="muted">No meetings yet.</li>';
      return;
    }

    for (const meeting of meetings) {
      const li = document.createElement("li");
      li.className = "history-item";
      li.innerHTML =
        `<span>${escapeHtml(meeting.title || meeting.filename)}</span>` +
        `<span class="badge badge-${meeting.status.toLowerCase()}">${meeting.status}</span>`;
      li.addEventListener("click", () => openMeeting(meeting));
      historyList.appendChild(li);
    }
  } catch (err) {
    // History is a nice-to-have; fail silently if the server is briefly unreachable.
  }
}

async function openMeeting(meeting) {
  if (meeting.status === "COMPLETED") {
    await loadResult(meeting.id);
  } else if (meeting.status === "FAILED") {
    const response = await fetch(`${API_BASE}/${meeting.id}`);
    const detail = await response.json();
    failureMessage.textContent = detail.error_message || "Processing failed.";
    showOnly(errorSection);
  } else {
    showOnly(statusSection);
    pollStatus(meeting.id);
  }
}

loadHistory();
