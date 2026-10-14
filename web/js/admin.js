const CAPTURES = 5;
const PROMPTS = [
  "Look straight at the camera.",
  "Turn a little to the left.",
  "Turn a little to the right.",
  "Look slightly up.",
  "Look straight again.",
];

const pages = document.querySelectorAll(".page");
const navBtns = document.querySelectorAll(".nav-btn[data-page]");
const video = document.getElementById("enroll-video");
const canvas = document.getElementById("work-canvas");
const dotsEl = document.getElementById("enroll-dots");
const countEl = document.getElementById("enroll-count");
const statusEl = document.getElementById("enroll-status");
const btnAuto = document.getElementById("btn-auto");
const btnStop = document.getElementById("btn-stop");
const btnEnrollCam = document.getElementById("btn-enable-enroll-cam");
const successEl = document.getElementById("enroll-success");
const shiftSelect = document.getElementById("enroll-shift");
const loginScreen = document.getElementById("login-screen");
const appShell = document.getElementById("app-shell");

let stream = null;
let captures = [];
let autoTimer = null;
let autoBusy = false;
let running = false;
const enrollEditing = document.getElementById("enroll-editing");
let editingId = enrollEditing && enrollEditing.value ? enrollEditing.value : null;
let currentRole = "admin";

function renderDots() {
  if (!dotsEl || !countEl) return;
  dotsEl.innerHTML = "";
  for (let i = 0; i < CAPTURES; i += 1) {
    const s = document.createElement("span");
    if (i < captures.length) s.classList.add("on");
    dotsEl.appendChild(s);
  }
  countEl.textContent = `${captures.length} / ${CAPTURES}`;
}

function applyRole(role) {
  currentRole = role === "super" ? "super" : "admin";
  document.body.classList.toggle("is-super", currentRole === "super");
}

function showApp(ok) {
  document.body.classList.toggle("is-authed", ok);
  loginScreen.classList.toggle("hidden", ok);
  appShell.classList.toggle("hidden", !ok);
  loginScreen.style.display = ok ? "none" : "block";
  loginScreen.style.pointerEvents = ok ? "none" : "";
  loginScreen.style.zIndex = ok ? "-1" : "";
  appShell.style.display = ok ? "block" : "none";
  appShell.style.pointerEvents = ok ? "auto" : "";
  if (ok) loginScreen.setAttribute("hidden", "hidden");
  else loginScreen.removeAttribute("hidden");
}

async function ensureAuth() {
  if (window.__BOOT_TOKEN__) setAuthToken(window.__BOOT_TOKEN__);
  const qsToken = new URLSearchParams(window.location.search).get("t");
  if (qsToken) setAuthToken(qsToken);
  try {
    const me = await api("/auth/me");
    applyRole(me.role);
    if (typeof window.codaiApplyUser === "function") window.codaiApplyUser(me);
    showApp(true);
    return true;
  } catch (err) {
    if (document.body.classList.contains("is-authed")) {
      showApp(true);
      return true;
    }
    showApp(false);
    return false;
  }
}

const logoutBtn = document.getElementById("btn-logout");
if (logoutBtn) logoutBtn.addEventListener("click", () => {
  setAuthToken("");
  stopAuto();
  stopStream(stream);
});

async function loadShiftsIntoSelect() {
  const shifts = await api("/shifts");
  shiftSelect.innerHTML =
    `<option value="">Auto at entrance</option>` +
    shifts.map((s) => `<option value="${s.id}">${s.name}  ${s.start_time}–${s.end_time}</option>`).join("");
  return shifts;
}

async function showPage(key) {
  pages.forEach((p) => {
    const on = p.id === "page-" + key;
    p.classList.toggle("active", on);
    if (on) {
      p.removeAttribute("hidden");
      p.style.display = "block";
      p.style.visibility = "visible";
    } else {
      p.setAttribute("hidden", "hidden");
      p.style.display = "none";
    }
  });
  navBtns.forEach((b) => b.classList.toggle("active", b.dataset.page === key));
  stopAuto();
  stopStream(stream);
  stream = null;
  if (video) video.srcObject = null;

  if (key === "register") {
    if (shiftSelect && shiftSelect.options.length <= 1) {
      try {
        await loadShiftsIntoSelect();
      } catch (err) {
        /* server already filled shifts */
      }
    }
    if (statusEl) {
      statusEl.textContent = "Tap the camera box to start it, then Start auto capture.";
    }
  }
  if (key === "people" && typeof window.codaiOnPage !== "function" && !document.querySelector("#people-body tr")) loadPeople();
  if (key === "shifts" && !document.querySelector("#shift-list .log-row, #shift-list .muted")) loadShiftList();
  if (key === "reports" && !document.querySelector("#report-body tr")) loadReports();
  if (key === "dashboard" && !document.querySelector("#dash-cards .stat-card")) await loadDashboard();
  if (key === "admins") {
    if (currentRole !== "super") {
      showPage("register");
      return;
    }
    loadStaff();
  }
  if (typeof window.codaiOnPage === "function") window.codaiOnPage(key);
}

function setEditing(person) {
  editingId = person ? person.employee_id : null;
  document.getElementById("edit-banner").classList.toggle("hidden", !person);
  document.getElementById("btn-save-person").classList.toggle("hidden", !person);
  document.getElementById("btn-cancel-edit").classList.toggle("hidden", !person);
  if (!person) {
    document.getElementById("enroll-name").value = "";
    document.getElementById("enroll-id").value = "";
    document.getElementById("enroll-phone").value = "";
    document.getElementById("enroll-em-name").value = "";
    document.getElementById("enroll-em-phone").value = "";
    ["enroll-email", "enroll-title", "enroll-dept", "enroll-manager"].forEach(function (id) {
      var n = document.getElementById(id);
      if (n) n.value = "";
    });
    var b = document.getElementById("enroll-branch");
    if (b) b.value = "HQ";
    var st = document.getElementById("enroll-status");
    if (st) st.value = "active";
    var jd = document.getElementById("enroll-joined");
    if (jd) jd.value = "";
    shiftSelect.value = "";
    return;
  }
  document.getElementById("enroll-name").value = person.name;
  document.getElementById("enroll-id").value = person.employee_id;
  document.getElementById("enroll-phone").value = person.phone || "";
  document.getElementById("enroll-em-name").value = person.emergency_name || "";
  document.getElementById("enroll-em-phone").value = person.emergency_phone || "";
  const emailEl = document.getElementById("enroll-email");
  if (emailEl) emailEl.value = person.email || "";
  const titleEl = document.getElementById("enroll-title");
  if (titleEl) titleEl.value = person.job_title || "";
  const deptEl = document.getElementById("enroll-dept");
  if (deptEl) deptEl.value = person.department || "";
  const branchEl = document.getElementById("enroll-branch");
  if (branchEl) branchEl.value = person.branch || "HQ";
  const mgrEl = document.getElementById("enroll-manager");
  if (mgrEl) mgrEl.value = person.manager || "";
  const stEl = document.getElementById("enroll-status");
  if (stEl) stEl.value = person.employment_status || "active";
  const joinedEl = document.getElementById("enroll-joined");
  if (joinedEl) joinedEl.value = person.joining_date || "";
  shiftSelect.value = person.shift_id != null ? String(person.shift_id) : "";
}

function stopAuto() {
  running = false;
  autoBusy = false;
  if (autoTimer) {
    clearInterval(autoTimer);
    autoTimer = null;
  }
  btnAuto.disabled = false;
  btnStop.disabled = true;
}

async function captureOnce() {
  if (!running || autoBusy || captures.length >= CAPTURES || !video.videoWidth) return;
  autoBusy = true;
  try {
    const image_b64 = frameToB64(canvas, video, 720, 0.9);
    const previous_b64 = captures.length ? captures[captures.length - 1] : null;
    await api("/captures/validate", {
      method: "POST",
      body: JSON.stringify({ image_b64, previous_b64 }),
    });
    captures.push(image_b64);
    renderDots();
    const next = captures.length;
    if (next < CAPTURES) {
      const line = PROMPTS[next] || "Hold still.";
      statusEl.textContent = line;
      speak(line);
    } else {
      statusEl.textContent = "Saving…";
      speak("All captures taken. Saving.");
      await issuePass();
    }
  } catch (e) {
    statusEl.textContent = e.message;
  } finally {
    autoBusy = false;
  }
}

function extraContact() {
  return {
    phone: document.getElementById("enroll-phone").value.trim(),
    emergency_name: document.getElementById("enroll-em-name").value.trim(),
    emergency_phone: document.getElementById("enroll-em-phone").value.trim(),
  };
}

function personPayload(includeFace) {
  const name = document.getElementById("enroll-name").value.trim();
  const employee_id = document.getElementById("enroll-id").value.trim();
  const rawShift = shiftSelect.value;
  const shift_id = rawShift ? Number(rawShift) : null;
  const body = { name, employee_id, shift_id, ...extraContact() };
  if (includeFace) body.images_b64 = captures;
  return body;
}

async function issuePass() {
  const body = personPayload(true);
  try {
    if (editingId) {
      await api(`/passes/${encodeURIComponent(editingId)}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: body.name,
          new_employee_id: body.employee_id,
          shift_id: body.shift_id,
          phone: body.phone,
          emergency_name: body.emergency_name,
          emergency_phone: body.emergency_phone,
          images_b64: captures,
        }),
      });
      successEl.textContent = "✓ MEMBER UPDATED";
      speak(`${body.name} is updated.`);
      setEditing(null);
    } else {
      await api("/passes/enroll", {
        method: "POST",
        body: JSON.stringify(body),
      });
      successEl.textContent = "✓ EMPLOYEE REGISTERED";
      speak(`${body.name} is registered.`);
    }
    successEl.classList.remove("hidden");
    captures = [];
    renderDots();
    document.getElementById("enroll-name").value = "";
    document.getElementById("enroll-id").value = "";
    shiftSelect.value = "";
    stopAuto();
    setTimeout(() => successEl.classList.add("hidden"), 2500);
  } catch (e) {
    statusEl.textContent = e.message;
    speak(e.message);
    stopAuto();
  }
}

async function startEnrollCamera() {
  if (!video) return false;
  try {
    if (!stream) stream = await startCamera(video);
    if (btnEnrollCam) btnEnrollCam.classList.add("hidden");
    if (statusEl) statusEl.textContent = "Camera on. Enter name and ID, then start auto capture — or save without Face ID.";
    return true;
  } catch (e) {
    if (btnEnrollCam) btnEnrollCam.classList.remove("hidden");
    if (statusEl) statusEl.textContent = `${e.message} Tap the camera box to allow it.`;
    return false;
  }
}

if (btnEnrollCam) btnEnrollCam.addEventListener("click", async (e) => {
  e.preventDefault();
  await startEnrollCamera();
});

if (btnAuto) btnAuto.addEventListener("click", async () => {
  const name = document.getElementById("enroll-name").value.trim();
  const employee_id = document.getElementById("enroll-id").value.trim();
  if (!name || !employee_id) {
    statusEl.textContent = "Enter name and member ID first.";
    return;
  }
  const camOk = await startEnrollCamera();
  if (!camOk) return;
  captures = [];
  renderDots();
  running = true;
  btnAuto.disabled = true;
  btnStop.disabled = false;
  const line = PROMPTS[0];
  statusEl.textContent = line;
  speak(`Hello ${name}. ${line}`);
  autoTimer = setInterval(captureOnce, 1600);
});

const cancelEditBtn = document.getElementById("btn-cancel-edit");
if (cancelEditBtn) cancelEditBtn.addEventListener("click", () => {
  setEditing(null);
  captures = [];
  renderDots();
});

if (btnStop) btnStop.addEventListener("click", stopAuto);
navBtns.forEach((btn) => {
  btn.addEventListener("click", (e) => {
    if (btn.tagName === "A" && !e.metaKey && !e.ctrlKey) {
      return;
    }
    e.preventDefault();
    showPage(btn.dataset.page);
  });
});

async function loadShiftList() {
  const list = document.getElementById("shift-list");
  const shifts = await api("/shifts");
  list.innerHTML = shifts
    .map(
      (s) => `<article class="log-row">
        <header>
          <span>${s.name}</span>
          <span class="granted-tag">${s.start_time} → ${s.end_time}</span>
        </header>
        <p class="muted">${s.end_time <= s.start_time ? "Overnight — same work date after midnight" : "Same calendar day"}</p>
        <button class="ghost small-del danger" data-del-shift="${s.id}" type="button">Delete</button>
      </article>`
    )
    .join("");
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function reportQuery() {
  const dateEl = document.getElementById("report-date");
  if (!dateEl.value) dateEl.value = new Date().toISOString().slice(0, 10);
  const day = dateEl.value;
  const period = document.getElementById("report-period").value;
  const token = getAuthToken();
  return { day, period, token };
}

async function loadDashboard() {
  const cards = document.getElementById("dash-cards");
  const weekEl = document.getElementById("dash-week");
  const onsite = document.getElementById("dash-onsite");
  if (!cards) return;
  try {
    const data = await api("/console/overview");
    if (typeof window.codaiOnPage === "function") {
      /* console renderer fills extras; keep a live fallback below if needed */
    }
    cards.innerHTML = (data.stats || [])
      .map(
        (s) =>
          `<a class="stat-card" href="${s.href || "#"}"><p class="eyebrow">${s.label}${s.source === "preview" ? " · preview" : ""}</p><strong>${s.value}</strong></a>`
      )
      .join("");
    if (onsite) {
      const rows = data.onsite || [];
      onsite.innerHTML = rows.length
        ? rows
            .map(
              (s) => `<article class="log-row"><header><span>${s.employee_name}</span><span class="granted-tag">${s.shift_name || "—"}</span></header>
              <p class="muted">In since ${fmtTime(s.clock_in)}</p></article>`
            )
            .join("")
        : `<p class="muted">Nobody is clocked in right now.</p>`;
    }
    const week = data.week || [];
    const max = Math.max(1, ...week.map((d) => d.checkins || 0));
    if (weekEl) {
      weekEl.innerHTML = week
        .map((d) => {
          const h = Math.round(((d.checkins || 0) / max) * 100);
          return `<div class="week-col"><div class="week-bar" style="height:${Math.max(h, 4)}%"></div><span>${String(d.date).slice(5)}<br>${d.checkins}</span></div>`;
        })
        .join("");
    }
  } catch (e) {
    cards.innerHTML = `<p class="status">${e.message}</p>`;
  }
}

async function loadReports() {
  const { day, period, token } = reportQuery();
  const qs = `work_date=${day}&period=${period}&token=${encodeURIComponent(token)}`;
  document.getElementById("btn-pdf").href = `${API}/reports/pdf?${qs}`;
  document.getElementById("btn-xlsx").href = `${API}/reports/excel?${qs}`;
  const data = await api(`/reports?work_date=${day}&period=${period}`);
  const summary = data.summary || {};
  const range = document.getElementById("report-range");
  const avgHint =
    data.period === "monthly"
      ? ` · Avg ${summary.avg_formatted || "—"} per employee`
      : "";
  range.innerHTML = `${data.period} · ${data.date_from} to ${data.date_to} · ${summary.closed || 0} closed sessions · ${summary.total_formatted || "0:00"} total${avgHint}`;
  const sumEl = document.getElementById("report-summary");
  if (sumEl) {
    sumEl.innerHTML = [
      ["Sessions", summary.sessions || 0],
      ["Total hours", summary.total_formatted || "0:00"],
      ["Avg / employee", summary.avg_formatted || "—"],
      data.period === "monthly" ? ["People", (summary.employees || []).length] : null,
    ]
      .filter(Boolean)
      .map(([k, v]) => `<article class="stat-card"><p class="eyebrow">${k}</p><strong>${v}</strong></article>`)
      .join("");
  }
  const avgWrap = document.getElementById("report-avg-wrap");
  const avgBody = document.getElementById("report-avg-body");
  if (avgWrap && avgBody) {
    const showAvg = data.period === "monthly";
    avgWrap.classList.toggle("hidden", !showAvg);
    if (showAvg) {
      const people = summary.employees || [];
      avgBody.innerHTML = people.length
        ? people
            .map(
              (p) => `<tr>
                <td>${p.employee_name}</td>
                <td>${p.employee_id}</td>
                <td>${p.days_worked}</td>
                <td>${p.sessions}</td>
                <td>${p.total_formatted}</td>
                <td>${p.avg_formatted}</td>
              </tr>`
            )
            .join("")
        : `<tr><td colspan="6" class="muted">No closed sessions this month.</td></tr>`;
    }
  }
  const body = document.getElementById("report-body");
  if (!data.rows.length) {
    body.innerHTML = `<tr><td colspan="7" class="muted">No sessions in this period.</td></tr>`;
    return;
  }
  body.innerHTML = data.rows
    .map((r) => {
      const cls =
        r.hours_status === "under"
          ? "hrs-under"
          : r.hours_status === "exact"
            ? "hrs-exact"
            : r.hours_status === "over"
              ? "hrs-over"
              : "";
      const lateCls = r.late ? " late-cell" : "";
      return `<tr class="${cls}${lateCls}">
        <td>${r.employee_name}</td>
        <td>${r.employee_id}</td>
        <td>${r.shift_name || "—"}</td>
        <td>${fmtTime(r.clock_in)}</td>
        <td>${r.clock_out ? fmtTime(r.clock_out) : "—"}</td>
        <td>${r.hours_formatted}</td>
        <td>${r.late_label || "On time"}</td>
      </tr>`;
    })
    .join("");
}

async function loadPeople() {
  const body = document.getElementById("people-body");
  const people = await api("/passes");
  if (!people.length) {
    body.innerHTML = `<tr><td colspan="6" class="muted">No employees yet.</td></tr>`;
    return;
  }
  body.innerHTML = people
    .map((p) => `<tr>
        <td>${p.name}</td>
        <td>${p.employee_id}</td>
        <td>${p.phone || "—"}</td>
        <td>${p.shift_name || "Auto at clock-in"}</td>
        <td>${p.has_face ? "Yes" : "No"}</td>
        <td class="people-actions">
          <button class="ghost small-del" type="button" data-edit-person="${p.employee_id}">Edit</button>
          <button class="ghost small-del danger" type="button" data-del-person="${p.employee_id}">Delete</button>
        </td>
      </tr>`)
    .join("");
}

document.getElementById("people-body").addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del-person]");
  const edit = e.target.closest("[data-edit-person]");
  const status = document.getElementById("people-status");
  if (del) {
    const id = del.getAttribute("data-del-person");
    if (!confirm(`Delete member ${id}? This also removes their visit records.`)) return;
    try {
      await api(`/passes/${encodeURIComponent(id)}`, { method: "DELETE" });
      status.textContent = "Member deleted.";
      loadPeople();
    } catch (err) {
      status.textContent = err.message;
    }
    return;
  }
  if (edit) {
    const id = edit.getAttribute("data-edit-person");
    const people = await api("/passes");
    const person = people.find((p) => p.employee_id === id);
    if (!person) return;
    await showPage("register");
    setEditing(person);
    statusEl.textContent = "Save details, or start auto capture to replace the face.";
  }
});


async function loadStaff() {
  const data = await api("/staff");
  document.getElementById("admin-quota").textContent =
    `${data.admins_used} of ${data.admins_max} admin seats used`;
  const body = document.getElementById("staff-body");
  body.innerHTML = data.users
    .map((u) => {
      const role = u.role === "super" ? "Super admin" : "Admin";
      const actions =
        u.role === "super"
          ? `<span class="muted">You</span>`
          : `<button class="ghost small-del" type="button" data-reset-staff="${u.id}" data-name="${u.username}">Reset password</button>
             <button class="ghost small-del danger" type="button" data-del-staff="${u.id}">Remove</button>`;
      return `<tr>
        <td>${u.username}</td>
        <td>${role}</td>
        <td class="people-actions">${actions}</td>
      </tr>`;
    })
    .join("");
}


document.getElementById("staff-body").addEventListener("click", async (e) => {
  const status = document.getElementById("staff-status");
  const del = e.target.closest("[data-del-staff]");
  const reset = e.target.closest("[data-reset-staff]");
  if (del) {
    if (!confirm("Remove this admin?")) return;
    try {
      await api(`/staff/${del.getAttribute("data-del-staff")}`, { method: "DELETE" });
      status.textContent = "Admin removed.";
      loadStaff();
    } catch (err) {
      status.textContent = err.message;
    }
    return;
  }
  if (reset) {
    const next = prompt(`New password for ${reset.getAttribute("data-name")}`);
    if (!next) return;
    try {
      await api(`/staff/${reset.getAttribute("data-reset-staff")}`, {
        method: "PATCH",
        body: JSON.stringify({ password: next }),
      });
      status.textContent = "Password updated.";
    } catch (err) {
      status.textContent = err.message;
    }
  }
});


var bootPage = "dashboard";
try {
  var pageMatch = (window.location.search || "").match(/[?&]page=([^&]+)/);
  if (pageMatch) bootPage = decodeURIComponent(pageMatch[1]);
} catch (err) {
  bootPage = "dashboard";
}
ensureAuth()
  .then(function (ok) {
    if (ok) return showPage(bootPage);
  })
  .catch(function () {});
try {
  renderDots();
} catch (err) {
  /* enroll widgets optional on first paint */
}
