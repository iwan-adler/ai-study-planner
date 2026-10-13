// AI Study Planner UI: plain JavaScript over the JSON API.
// User-provided text is always inserted with textContent (never innerHTML).

const $ = (sel) => document.querySelector(sel);
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const PRIORITY = { 1: "Low", 2: "Medium", 3: "High" };
let subjects = [];

// ---------- helpers ----------

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorText(data, response.status));
  return data;
}

function errorText(data, status) {
  const detail = data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((e) => `${e.loc.slice(1).join(".") || "input"}: ${e.msg.replace(/^Value error, /, "")}`).join("; ");
  }
  return `Request failed (${status})`;
}

let flashTimer;
function flash(text, isError = false) {
  const el = $("#flash");
  el.textContent = text;
  el.className = isError ? "flash error" : "flash";
  el.hidden = false;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => (el.hidden = true), isError ? 6000 : 2500);
}

async function attempt(fn, success) {
  try {
    const result = await fn();
    if (success) flash(success);
    return result;
  } catch (err) {
    flash(err.message, true);
    return undefined;
  }
}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, onClick, className) {
  const b = el("button", text, className);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const fmtDateTime = (iso) =>
  new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const fmtMinutes = (m) => (m >= 60 ? `${Math.floor(m / 60)} h ${m % 60 ? `${m % 60} min` : ""}`.trim() : `${m} min`);

function fillList(list, items, render, emptyText) {
  list.replaceChildren();
  if (!items.length && emptyText) list.append(el("li", emptyText, "empty"));
  items.forEach((item) => list.append(render(item)));
}

// ---------- sessions ----------

function renderSession(s, { showDate = false } = {}) {
  const li = $("#session-tpl").content.firstElementChild.cloneNode(true);
  li.querySelector(".time").textContent =
    `${showDate ? fmtDateTime(s.start_at) : fmtTime(s.start_at)} – ${fmtTime(s.end_at)} · ${fmtMinutes(s.planned_minutes)}`;
  li.querySelector(".title").textContent = s.task_title;
  li.querySelector(".subject").textContent = s.subject_name;
  const chip = li.querySelector(".status");
  chip.textContent = s.status === "partial" ? `partial (${s.actual_minutes} min)` : s.status;
  chip.classList.add(s.status);
  const checkin = li.querySelector(".checkin");
  if (s.status !== "planned") checkin.remove();
  else checkin.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => checkIn(s, b.dataset.status)));
  return li;
}

async function checkIn(session, status) {
  const body = { status };
  if (status === "partial") {
    const answer = prompt(`How many minutes did you study? (1–${session.planned_minutes - 1})`);
    if (answer === null) return;
    body.actual_minutes = Number(answer);
  }
  const ok = await attempt(() => api(`/sessions/${session.id}`, { method: "PATCH", body }), "Saved — plan updated");
  if (ok) refresh();
}

// ---------- dashboard ----------

async function loadDashboard() {
  const d = await api("/dashboard");
  const stats = [
    [`${d.tasks_completed}/${d.tasks_total}`, "tasks completed"],
    [fmtMinutes(d.week.studied_minutes), `studied this week (of ${fmtMinutes(d.week.scheduled_minutes)} scheduled)`],
    [`${d.week.completed_sessions} / ${d.week.partial_sessions} / ${d.week.missed_sessions}`, "sessions done / partial / missed"],
    [String(d.overdue.length), "overdue tasks"],
  ];
  $("#stats").replaceChildren(...stats.map(([value, label]) => {
    const box = el("div", undefined, "stat");
    box.append(el("div", value, "value"), el("div", label, "label"));
    return box;
  }));

  const warnings = $("#warnings");
  warnings.replaceChildren();
  if (d.unscheduled.length) {
    const box = el("div", undefined, "alert");
    box.setAttribute("role", "alert");
    box.append(el("strong", "Not everything fits before its deadline."));
    const ul = el("ul");
    d.unscheduled.forEach((u) => ul.append(el("li", `${u.task_title}: ${u.reason}`)));
    box.append(ul, el("div", "Add availability, lower an estimate or move a deadline.", "small"));
    warnings.append(box);
  }

  fillList($("#today"), d.today, (s) => renderSession(s), "Nothing scheduled today.");
  fillList($("#review"), d.to_review, (s) => renderSession(s, { showDate: true }), "");
  $("#review-card").hidden = !d.to_review.length;

  const taskLine = (t) => {
    const li = el("li");
    li.append(el("strong", t.title), el("div", `${t.subject_name} · due ${fmtDateTime(t.deadline)} · ${fmtMinutes(t.remaining_minutes)} left, ${fmtMinutes(t.scheduled_minutes)} scheduled`, "muted small"));
    return li;
  };
  fillList($("#upcoming"), d.upcoming, taskLine, "No deadlines in the next 7 days.");
  fillList($("#overdue"), d.overdue, taskLine, "");
  $("#overdue-title").hidden = !d.overdue.length;

  fillList($("#subjects-attention"), d.subjects, (s) => {
    const li = el("li");
    const head = el("div");
    head.append(el("strong", s.subject_name), " ");
    if (s.needs_attention) head.append(el("span", "needs attention", "chip overdue"));
    li.append(head, el("div", s.needs_attention ? s.reasons.join(" · ") : `${s.open_tasks} open task(s), ${fmtMinutes(s.remaining_minutes)} left`, "muted small"));
    return li;
  }, "Add subjects in Setup.");
}

// ---------- schedule ----------

async function loadSchedule(plan) {
  plan = plan || (await api("/plan"));
  const byDay = new Map();
  for (let i = 0; i < 7; i++) {
    const day = new Date();
    day.setHours(0, 0, 0, 0);
    day.setDate(day.getDate() + i);
    byDay.set(day.toDateString(), { day, sessions: [] });
  }
  plan.sessions.forEach((s) => byDay.get(new Date(s.start_at).toDateString())?.sessions.push(s));
  const week = $("#week");
  week.replaceChildren();
  for (const { day, sessions } of byDay.values()) {
    const block = el("div", undefined, "day card");
    const total = sessions.filter((s) => s.status !== "missed").reduce((n, s) => n + s.planned_minutes, 0);
    block.append(el("h3", `${day.toLocaleDateString([], { weekday: "long", day: "numeric", month: "short" })} · ${fmtMinutes(total)}`));
    const ul = el("ul", undefined, "list");
    fillList(ul, sessions, (s) => renderSession(s), "Free");
    block.append(ul);
    week.append(block);
  }
  fillList($("#reasons"), plan.reasons, (r) => {
    const li = el("li");
    li.append(el("strong", r.task_title), el("div", r.reason, "muted"));
    return li;
  }, "No open tasks.");
}

// ---------- tasks ----------

function resetTaskForm() {
  const form = $("#task-form");
  form.reset();
  form.id.value = "";
  const deadline = new Date(Date.now() + 3 * 86400000);
  deadline.setHours(23, 0, 0, 0);
  form.deadline.value = toLocalInput(deadline);
  $("#task-form-title").textContent = "Add a task";
  $("#task-cancel").hidden = true;
}

function toLocalInput(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

async function loadTasks() {
  const tasks = await api("/tasks");
  const rows = $("#task-rows");
  rows.replaceChildren();
  if (!tasks.length) {
    const tr = el("tr");
    const td = el("td", "No tasks yet. Add one above.", "muted");
    td.colSpan = 5;
    tr.append(td);
    rows.append(tr);
  }
  for (const t of tasks) {
    const tr = el("tr", undefined, t.completed ? "done" : "");
    const name = el("td");
    name.append(el("strong", t.title), el("div", t.subject_name, "muted small"));
    const due = el("td", fmtDateTime(t.deadline));
    if (t.overdue) due.append(" ", el("span", "overdue", "chip overdue"));
    const progress = el("td");
    const pct = Math.min(100, Math.round((t.studied_minutes / t.estimated_minutes) * 100));
    progress.append(el("div", t.completed ? "Completed" : `${fmtMinutes(t.studied_minutes)} of ${fmtMinutes(t.estimated_minutes)} · ${fmtMinutes(t.scheduled_minutes)} scheduled`, "small"));
    const bar = el("div", undefined, "bar");
    const fill = el("span");
    fill.style.width = `${t.completed ? 100 : pct}%`;
    bar.append(fill);
    progress.append(bar);
    const actions = el("td");
    const box = el("div", undefined, "row-actions");
    if (!t.completed) {
      box.append(
        button("Edit", () => editTask(t), "small"),
        button("Complete", async () => {
          if (await attempt(() => api(`/tasks/${t.id}`, { method: "PATCH", body: { completed: true } }), "Task completed")) refresh();
        }, "small"),
        button("Break down", () => showBreakdown(t), "small"),
      );
    }
    box.append(button("Delete", async () => {
      if (!confirm(`Delete "${t.title}" and its sessions?`)) return;
      if ((await attempt(() => api(`/tasks/${t.id}`, { method: "DELETE" }), "Task deleted")) !== undefined) refresh();
    }, "small"));
    actions.append(box);
    tr.append(name, due, progress, el("td", `${PRIORITY[t.priority]} · difficulty ${t.difficulty}/5`, "small"), actions);
    rows.append(tr);
  }
}

function editTask(t) {
  const form = $("#task-form");
  form.id.value = t.id;
  form.subject_id.value = t.subject_id;
  form.title.value = t.title;
  form.notes.value = t.notes;
  form.estimated_minutes.value = t.estimated_minutes;
  form.deadline.value = t.deadline.slice(0, 16);
  form.priority.value = t.priority;
  form.difficulty.value = t.difficulty;
  $("#task-form-title").textContent = "Edit task";
  $("#task-cancel").hidden = false;
  form.scrollIntoView({ behavior: "smooth" });
}

async function submitTask(event) {
  event.preventDefault();
  const form = event.target;
  const body = {
    subject_id: Number(form.subject_id.value),
    title: form.title.value,
    notes: form.notes.value,
    estimated_minutes: Number(form.estimated_minutes.value),
    deadline: form.deadline.value,
    priority: Number(form.priority.value),
    difficulty: Number(form.difficulty.value),
  };
  const id = form.id.value;
  const saved = await attempt(
    () => (id ? api(`/tasks/${id}`, { method: "PATCH", body }) : api("/tasks", { method: "POST", body })),
    id ? "Task updated — plan updated" : "Task added — plan updated",
  );
  if (saved) {
    resetTaskForm();
    refresh();
  }
}

async function showBreakdown(t) {
  const box = $("#breakdown");
  box.hidden = false;
  box.replaceChildren(el("p", "Working…", "muted"));
  const result = await attempt(() => api(`/ai/breakdown/${t.id}`, { method: "POST" }));
  if (!result) {
    box.hidden = true;
    return;
  }
  const head = el("div", undefined, "row-between");
  const title = el("h2", `Break down: ${result.task_title} `);
  title.append(el("span", result.source === "ai" ? "AI suggestion" : "rule-based split", result.source === "ai" ? "chip ai" : "chip"));
  head.append(title, button("Close", () => (box.hidden = true), "small"));
  const ul = el("ul", undefined, "list");
  result.subtasks.forEach((s) => ul.append(el("li", `${s.title} — ${fmtMinutes(s.estimated_minutes)}`)));
  box.replaceChildren(head, ul);
  box.append(el("p", `Total ${fmtMinutes(result.total_minutes)}; ${fmtMinutes(result.free_minutes_before_deadline)} of availability before the deadline.`, "small muted"));
  result.warnings.forEach((w) => box.append(el("p", w, "alert small")));
  box.append(el("p", "Nothing has been saved. Applying replaces the original task with these subtasks.", "small muted"));
  box.append(button("Replace task with these subtasks", async () => {
    const created = await attempt(
      () => api(`/tasks/${t.id}/split`, { method: "POST", body: { subtasks: result.subtasks } }),
      "Task split — plan updated",
    );
    if (created) {
      box.hidden = true;
      refresh();
    }
  }, "primary"));
  box.scrollIntoView({ behavior: "smooth" });
}

// ---------- setup ----------

async function loadSetup() {
  const [profile, windows] = await Promise.all([api("/profile"), api("/availability")]);
  const form = $("#profile-form");
  for (const key of ["name", "goals", "max_session_minutes", "break_minutes", "max_daily_minutes"]) form[key].value = profile[key];

  fillList($("#subject-list"), subjects, (s) => {
    const li = el("li", undefined, "row-between");
    li.append(el("span", s.name), button("Delete", async () => {
      if ((await attempt(() => api(`/subjects/${s.id}`, { method: "DELETE" }), "Subject deleted")) !== undefined) refresh();
    }, "small"));
    return li;
  }, "No subjects yet.");

  const box = $("#windows");
  box.replaceChildren();
  windows.forEach((w) => box.append(windowRow(w)));
  if (!windows.length) box.append(windowRow({ weekday: 0, start_time: "18:00", end_time: "20:00" }));
}

function windowRow(w) {
  const row = el("div", undefined, "window-row");
  const day = el("select");
  day.setAttribute("aria-label", "Day");
  WEEKDAYS.forEach((name, i) => day.append(new Option(name, i)));
  day.value = w.weekday;
  const start = el("input");
  start.type = "time";
  start.value = w.start_time.slice(0, 5);
  start.setAttribute("aria-label", "Start time");
  const end = el("input");
  end.type = "time";
  end.value = w.end_time.slice(0, 5);
  end.setAttribute("aria-label", "End time");
  row.append(day, start, el("span", "to", "muted"), end, button("Remove", () => row.remove(), "small"));
  return row;
}

async function saveWindows() {
  const windows = [...document.querySelectorAll(".window-row")].map((row) => {
    const [day, start, end] = row.querySelectorAll("select, input");
    return { weekday: Number(day.value), start_time: start.value, end_time: end.value };
  });
  if (await attempt(() => api("/availability", { method: "PUT", body: { windows } }), "Availability saved — plan updated")) refresh();
}

// ---------- assistant ----------

async function loadAssistant() {
  const status = await api("/ai/status");
  $("#ai-status").textContent = status.enabled ? `AI model: ${status.model}. ${status.note}` : status.note;
}

async function explain() {
  const out = $("#explanation");
  out.replaceChildren(el("p", "Working…", "muted"));
  const r = await attempt(() => api("/ai/explain", { method: "POST" }));
  if (!r) return out.replaceChildren();
  const badge = el("span", r.source === "ai" ? "AI-generated" : "rule-based", r.source === "ai" ? "chip ai" : "chip");
  const head = el("h3", "Summary ");
  head.append(badge);
  out.replaceChildren(head, el("p", r.summary));
  r.warnings.forEach((w) => out.append(el("p", w, "alert small")));
  if (r.task_notes.length) {
    out.append(el("h3", "Per task"));
    const ul = el("ul", undefined, "list small");
    r.task_notes.forEach((n) => {
      const li = el("li");
      li.append(el("strong", n.task_title), el("div", n.note, "muted"));
      ul.append(li);
    });
    out.append(ul);
  }
  if (r.revision_tips.length) {
    out.append(el("h3", "Revision tips"));
    const ul = el("ul");
    r.revision_tips.forEach((t) => ul.append(el("li", t)));
    out.append(ul);
  }
}

// ---------- wiring ----------

function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    if (t.dataset.tab === name) t.setAttribute("aria-current", "page");
    else t.removeAttribute("aria-current");
  });
  document.querySelectorAll(".panel").forEach((p) => (p.hidden = p.id !== `tab-${name}`));
  try { localStorage.setItem("tab", name); } catch { /* storage unavailable */ }
}

async function refresh() {
  try {
    subjects = await api("/subjects");
    const select = $("#task-form").subject_id;
    const current = select.value;
    select.replaceChildren(...subjects.map((s) => new Option(s.name, s.id)));
    if (current) select.value = current;
    await Promise.all([loadDashboard(), loadSchedule(), loadTasks(), loadSetup(), loadAssistant()]);
  } catch (err) {
    flash(`Could not load data: ${err.message}`, true);
  }
}

document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
$("#task-form").addEventListener("submit", submitTask);
$("#task-cancel").addEventListener("click", resetTaskForm);
$("#replan-btn").addEventListener("click", async () => {
  const plan = await attempt(() => api("/plan", { method: "POST" }), "Plan regenerated");
  if (plan) refresh();
});
$("#profile-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    name: f.name.value, goals: f.goals.value, max_session_minutes: Number(f.max_session_minutes.value),
    break_minutes: Number(f.break_minutes.value), max_daily_minutes: Number(f.max_daily_minutes.value),
  };
  if (await attempt(() => api("/profile", { method: "PUT", body }), "Preferences saved — plan updated")) refresh();
});
$("#subject-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (await attempt(() => api("/subjects", { method: "POST", body: { name: e.target.name.value } }), "Subject added")) {
    e.target.reset();
    refresh();
  }
});
$("#add-window").addEventListener("click", () => $("#windows").append(windowRow({ weekday: 0, start_time: "18:00", end_time: "20:00" })));
$("#save-windows").addEventListener("click", saveWindows);
$("#explain-btn").addEventListener("click", explain);

// A link like /#schedule opens that tab; otherwise reopen the last tab used.
let initial = location.hash.slice(1) || "dashboard";
if (!location.hash) {
  try { initial = localStorage.getItem("tab") || initial; } catch { /* ignore */ }
}
showTab(document.getElementById(`tab-${initial}`) ? initial : "dashboard");
resetTaskForm();
refresh();
