(function () {
  var TITLES = {
    dashboard: "Overview",
    people: "Employees",
    register: "Enroll face",
    attendance: "Attendance",
    shifts: "Shifts & Schedules",
    reports: "Reports & Analytics",
    branches: "Branches & Departments",
    devices: "Devices & Kiosks",
    admins: "Users & Permissions",
    integrations: "API & Integrations",
    notifications: "Notifications",
    settings: "Settings",
    payroll: "Payroll",
  };
  var peopleCache = [];
  var peoplePage = 0;
  var PAGE_SIZE = 12;
  var attCache = { rows: [], audits: [] };
  var deviceStream = null;
  var devicePulse = null;
  var deviceListTimer = null;
  var watchDeviceId = null;
  var previewUrl = "";
  var devicesCache = [];
  var knownDeviceIds = {};
  var devicePollReady = false;

  function el(id) {
    return document.getElementById(id);
  }

  function toast(msg, isErr) {
    var box = el("toast");
    if (!box) return;
    box.textContent = msg;
    box.className = isErr ? "toast err" : "toast";
    box.removeAttribute("hidden");
    box.classList.remove("hidden");
    setTimeout(function () {
      box.classList.add("hidden");
    }, 4200);
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setText(id, value) {
    var node = el(id);
    if (node) node.textContent = value;
  }

  function applyUser(me) {
    var name = (me && me.username) || "Admin";
    var role = (me && me.role) === "leader" ? "Team leader" : "Admin";
    var initial = name.charAt(0).toUpperCase();
    ["sidebar-username", "top-username"].forEach(function (id) {
      setText(id, name);
    });
    ["sidebar-role", "top-role"].forEach(function (id) {
      setText(id, role);
    });
    ["sidebar-avatar", "top-avatar"].forEach(function (id) {
      setText(id, initial);
    });
  }

  function setNavOpen(open) {
    document.body.classList.toggle("nav-open", !!open);
    var back = el("sidebar-backdrop");
    var btn = el("btn-open-nav");
    if (back) {
      if (open) back.removeAttribute("hidden");
      else back.setAttribute("hidden", "hidden");
    }
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function csvDownload(filename, rows) {
    var blob = new Blob([rows.join("\n")], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
  }

  function renderTable(container, columns, rows, empty) {
    if (!container) return;
    if (!rows || !rows.length) {
      container.innerHTML = '<p class="muted">' + esc(empty || "Nothing to show.") + "</p>";
      return;
    }
    var head = columns.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("");
    var body = rows
      .map(function (row) {
        return "<tr>" + row.map(function (cell) { return "<td>" + cell + "</td>"; }).join("") + "</tr>";
      })
      .join("");
    container.innerHTML =
      '<table class="report-table"><thead><tr>' + head + "</tr></thead><tbody>" + body + "</tbody></table>";
  }

  function previewPanel(targetId, module) {
    var box = el(targetId);
    if (!box) return Promise.resolve();
    box.innerHTML = '<p class="muted">Loading…</p>';
    return api("/console/preview/" + module)
      .then(function (data) {
        var note = '<p class="source-banner preview">' + esc(data.note || "Preview only — not saved.") + "</p>";
        var rows = (data.rows || []).map(function (r) {
          return r.map(function (c) { return esc(c); });
        });
        box.innerHTML = note;
        var wrap = document.createElement("div");
        wrap.className = "table-wrap";
        box.appendChild(wrap);
        renderTable(wrap, data.columns || [], rows, "No preview rows.");
        var actions = document.createElement("div");
        actions.className = "actions";
        actions.innerHTML =
          '<button type="button" class="ghost" data-preview-action="' +
          esc(module) +
          '">Create record</button>';
        box.appendChild(actions);
      })
      .catch(function (err) {
        box.innerHTML = '<p class="status">' + esc(err.message) + "</p>";
      });
  }

  function statTone(key) {
    if (key === "present" || key === "working" || key === "employees") return "ok";
    if (key === "late") return "late";
    if (key === "absent" || key === "missing") return "critical";
    return "";
  }

  function setDashLink(state, detail) {
    var pill = el("dash-link");
    if (pill) {
      pill.className = "live-pill " + (state || "");
      pill.textContent = state === "live" ? "Live" : state === "offline" ? "Offline" : "Connecting";
    }
    var meta = el("dash-refresh");
    if (meta && detail) meta.textContent = detail;
  }

  function renderStaff(data) {
    var note = el("dash-month-note");
    var allowance = data.days_off_allowance || 4;
    if (note) {
      note.textContent =
        "Through " +
        (data.staff_through || "today") +
        ". Each employee has " +
        allowance +
        " days off. No clock-in is 1 day, absent is 2 days, and a day off is free until those " +
        allowance +
        " are used.";
    }
    var body = el("dash-staff-body");
    if (!body) return;
    var rows = data.staff || [];
    var cur = data.currency || "EGP";
    body.innerHTML = rows.length
      ? rows
          .map(function (r) {
            return (
              "<tr><td>" +
              esc(r.name) +
              '<br><span class="muted">' +
              esc(r.employee_id) +
              "</span></td><td class=\"num\">" +
              esc(money(r.salary, cur)) +
              "</td><td class=\"num\">" +
              esc(r.days_off_left) +
              " of " +
              esc(r.days_off_allowance || allowance) +
              "</td><td class=\"num\">" +
              esc(r.late_days) +
              "</td><td class=\"num\">" +
              esc(r.absent) +
              "</td><td class=\"num\">" +
              esc(r.no_clock_in) +
              "</td><td class=\"num\">" +
              esc(r.no_clock_out) +
              "</td><td class=\"num\">" +
              esc(r.extra_days || 0) +
              "</td><td class=\"num\">" +
              esc(money(r.deducted, cur)) +
              "</td><td class=\"num\">" +
              esc(money(r.to_pay, cur)) +
              "</td><td>" +
              todayChoice(r) +
              "</td></tr>"
            );
          })
          .join("")
      : '<tr><td colspan="11" class="muted">No active employees.</td></tr>';
  }

  function renderOverview(data) {
    if (!data) return;
    setText("sidebar-company", data.company || "Bunker Attendance");
    setText("sidebar-branch", data.branch || "Headquarters");
    var hour = new Date().getHours();
    var hello = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
    setText("dash-greet", hello);
    setText("top-date", new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" }));
    var cards = el("dash-cards");
    if (cards && data.stats) {
      cards.innerHTML = data.stats
        .map(function (s) {
          var src = s.source === "preview" ? " · preview" : "";
          var tone = statTone(s.key);
          return (
            '<a class="stat-card' +
            (tone ? " tone-" + tone : "") +
            '" href="' +
            esc(s.href || "#") +
            '"><p class="eyebrow">' +
            esc(s.label) +
            src +
            "</p><strong>" +
            esc(s.value) +
            "</strong></a>"
          );
        })
        .join("");
    }
    renderStaff(data);
    var alertBox = el("dash-alert");
    if (alertBox) {
      var byKey = {};
      (data.stats || []).forEach(function (s) { byKey[s.key] = Number(s.value) || 0; });
      var parts = [];
      if (byKey.absent) parts.push(byKey.absent + " absent");
      if (byKey.missing) parts.push(byKey.missing + " missing check-out");
      if (byKey.late) parts.push(byKey.late + " late");
      if (parts.length) {
        alertBox.classList.remove("hidden");
        alertBox.textContent = "Needs attention: " + parts.join(" · ");
      } else {
        alertBox.classList.add("hidden");
        alertBox.textContent = "";
      }
    }
    var week = data.week || [];
    var weekEl = el("dash-week");
    if (weekEl && week.length) {
      var max = 1;
      week.forEach(function (d) {
        if ((d.checkins || 0) > max) max = d.checkins;
      });
      weekEl.innerHTML = week
        .map(function (d) {
          var h = Math.max(4, Math.round(((d.checkins || 0) / max) * 100));
          return (
            '<div class="week-col"><div class="week-bar" style="height:' +
            h +
            '%"></div><span>' +
            esc(String(d.date || "").slice(5)) +
            "<br>" +
            esc(d.checkins) +
            "</span></div>"
          );
        })
        .join("");
    }
    var donut = el("dash-donut");
    if (donut) {
      var dist = data.distribution || [];
      var total = 0;
      dist.forEach(function (d) { total += d.value || 0; });
      donut.innerHTML = dist
        .map(function (d) {
          var pct = total ? Math.round((100 * (d.value || 0)) / total) : 0;
          return (
            '<div class="donut-row"><span>' +
            esc(d.label) +
            "</span><strong>" +
            esc(d.value) +
            " · " +
            pct +
            '%</strong></div><div class="donut-bar tone-' +
            esc(d.tone || "") +
            '"><span style="width:' +
            pct +
            '%"></span></div>'
          );
        })
        .join("");
    }
    var act = el("dash-activity");
    if (act) {
      renderTable(
        act,
        ["Employee", "ID", "Event", "Time", "Status"],
        (data.activity || []).map(function (r) {
          return [esc(r.name), esc(r.employee_id), esc(r.event), esc(r.time), esc(r.status)];
        }),
        "No attendance events today."
      );
    }
    var onsite = el("dash-onsite");
    if (onsite) {
      var rows = data.onsite || [];
      onsite.innerHTML = rows.length
        ? rows
            .map(function (s) {
              return (
                '<article class="log-row"><header><span>' +
                esc(s.employee_name) +
                '</span><span class="granted-tag">' +
                esc(s.shift_name || "On site") +
                "</span></header><p class=\"muted\">In since " +
                esc(String(s.clock_in || "").slice(11, 16) || "—") +
                "</p></article>"
              );
            })
            .join("")
        : '<p class="muted">Nobody is clocked in right now.</p>';
    }
    var br = el("dash-branches");
    if (br) {
      renderTable(
        br,
        ["Branch", "Employees", "Present", "Absent", "%"],
        (data.branches || []).map(function (r) {
          return [esc(r.name), esc(r.employees), esc(r.present), esc(r.absent), esc(r.pct) + "%"];
        }),
        "No branch data."
      );
    }
    var dev = el("dash-devices");
    if (dev) {
      var devices = data.devices || [];
      dev.innerHTML = devices.length
        ? devices
            .map(function (d) {
              return (
                "<article class=\"log-row\"><header><span>" +
                esc(d.name) +
                '</span><span class="granted-tag">' +
                esc(d.status) +
                "</span></header><p class=\"muted\">MAC " +
                esc(d.mac || "—") +
                " · IP " +
                esc(d.ip || "—") +
                " · last seen " +
                esc(d.last_sync) +
                "</p></article>"
              );
            })
            .join("")
        : '<p class="muted">No kiosk online. Open /kiosk on the tablet.</p>';
    }
    var pending = el("dash-pending");
    if (pending) {
      var items = data.pending || [];
      pending.innerHTML = items.length
        ? items
            .map(function (p) {
              return (
                '<article class="log-row"><header><span>' +
                esc(p.title) +
                '</span><span class="granted-tag">' +
                esc(p.source) +
                "</span></header><p class=\"muted\">" +
                esc(p.detail) +
                '</p><a class="ghost small-del" href="' +
                esc(p.href) +
                '">Open</a></article>'
              );
            })
            .join("")
        : '<p class="muted">No pending actions.</p>';
    }
    var count = data.unread_alerts || 0;
    var badge = el("notify-count");
    var navBadge = el("nav-alert-count");
    if (badge) badge.textContent = String(count);
    if (navBadge) {
      navBadge.textContent = String(count);
      if (count) navBadge.removeAttribute("hidden");
      else navBadge.setAttribute("hidden", "hidden");
    }
  }

  var dashTimer = null;

  function loadOverview() {
    return api("/console/overview")
      .then(function (data) {
        renderOverview(data);
        var stamp = new Date().toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        setDashLink("live", "Auto-refresh 15s · updated " + stamp);
      })
      .catch(function (err) {
        var cards = el("dash-cards");
        setDashLink("offline", "Offline · last view kept · " + (err.message || "retrying"));
        if (cards && !cards.children.length) cards.innerHTML = '<p class="status">' + esc(err.message) + "</p>";
      });
  }

  function startDashWatch() {
    loadOverview();
    if (dashTimer) clearInterval(dashTimer);
    dashTimer = setInterval(loadOverview, 15000);
  }

  function stopDashWatch() {
    if (dashTimer) clearInterval(dashTimer);
    dashTimer = null;
  }

  function filteredPeople() {
    var q = ((el("people-search") && el("people-search").value) || "").toLowerCase();
    var branch = (el("people-branch") && el("people-branch").value) || "";
    var dept = (el("people-dept") && el("people-dept").value) || "";
    var shift = (el("people-shift") && el("people-shift").value) || "";
    var face = (el("people-face") && el("people-face").value) || "";
    var status = (el("people-status-filter") && el("people-status-filter").value) || "";
    return peopleCache.filter(function (p) {
      var hay = ((p.name || "") + " " + (p.employee_id || "") + " " + (p.phone || "") + " " + (p.job_title || "")).toLowerCase();
      if (q && hay.indexOf(q) === -1) return false;
      if (branch && (p.branch || "HQ") !== branch) return false;
      if (dept && (p.department || "") !== dept) return false;
      if (shift && String(p.shift_id || "") !== shift) return false;
      if (face === "yes" && !p.has_face) return false;
      if (face === "no" && p.has_face) return false;
      if (status && (p.employment_status || "active") !== status) return false;
      return true;
    });
  }

  function fillFilter(id, values, keep) {
    var sel = el(id);
    if (!sel) return;
    var current = sel.value;
    var opts = ['<option value="">' + keep + "</option>"];
    values.forEach(function (v) {
      if (!v) return;
      opts.push("<option value=\"" + esc(v) + "\">" + esc(v) + "</option>");
    });
    sel.innerHTML = opts.join("");
    sel.value = current;
  }

  function renderPeople() {
    var body = el("people-body");
    if (!body) return;
    var rows = filteredPeople();
    var pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    if (peoplePage >= pages) peoplePage = pages - 1;
    var slice = rows.slice(peoplePage * PAGE_SIZE, peoplePage * PAGE_SIZE + PAGE_SIZE);
    if (!slice.length) {
      body.innerHTML = '<tr><td colspan="9" class="muted">No employees match these filters.</td></tr>';
    } else {
      body.innerHTML = slice
        .map(function (p) {
          var face = p.has_face ? "Enrolled" : "Not enrolled";
          var salary = p.monthly_salary ? String(p.monthly_salary) : "—";
          var eid = esc(p.employee_id);
          return (
            "<tr>" +
            "<td>" + eid + "</td>" +
            "<td>" + esc(p.name) + "</td>" +
            "<td>" + esc(p.job_title || "—") + "</td>" +
            "<td>" + (p.work_type === "part" ? "Part time" : p.work_type === "leader" ? "Team leader" : "Full time") + "</td>" +
            "<td>" + esc(p.phone || "—") + "</td>" +
            "<td>" + esc(salary) + "</td>" +
            "<td>" + face + "</td>" +
            "<td>" + esc(p.employment_status || "active") + "</td>" +
            '<td class="people-actions">' +
            '<button class="ghost small-del" type="button" data-edit-person="' + eid + '">Edit</button>' +
            '<button class="ghost small-del" type="button" data-emp-att="' + eid + '">Attendance</button>' +
            '<button class="ghost small-del" type="button" data-emp-rep="' + eid + '">Report</button>' +
            '<a class="ghost small-del" href="/admin?page=register">Enroll face</a>' +
            '<button class="ghost small-del danger" type="button" data-deactivate-person="' + eid + '">Deactivate</button>' +
            "</td></tr>"
          );
        })
        .join("");
    }
    var pager = el("people-pager");
    if (pager) {
      pager.innerHTML =
        '<button type="button" class="ghost" id="people-prev">Previous</button>' +
        "<span class=\"muted\">Page " + (peoplePage + 1) + " of " + pages + " · " + rows.length + " people</span>" +
        '<button type="button" class="ghost" id="people-next">Next</button>';
    }
  }

  function loadPeopleConsole() {
    return api("/passes").then(function (people) {
      peopleCache = people || [];
      var branches = [];
      var depts = [];
      peopleCache.forEach(function (p) {
        if (p.branch && branches.indexOf(p.branch) === -1) branches.push(p.branch);
        if (p.department && depts.indexOf(p.department) === -1) depts.push(p.department);
      });
      fillFilter("people-branch", branches, "All");
      fillFilter("people-dept", depts, "All");
      api("/shifts")
        .then(function (shifts) {
          var sel = el("people-shift");
          if (!sel) return;
          sel.innerHTML =
            '<option value="">All</option>' +
            (shifts || [])
              .map(function (s) {
                return '<option value="' + s.id + '">' + esc(s.name) + "</option>";
              })
              .join("");
        })
        .catch(function () {});
      renderPeople();
    });
  }

  function renderAttendance(data) {
    attCache = data || { rows: [], audits: [] };
    var status = (el("att-status") && el("att-status").value) || "";
    var rows = (attCache.rows || []).filter(function (r) {
      return !status || r.status === status;
    });
    var body = el("att-body");
    if (body) {
      body.innerHTML = rows.length
        ? rows
            .map(function (r) {
              return (
                "<tr>" +
                "<td>" + esc(r.employee_name) + "</td>" +
                "<td>" + esc(r.employee_id) + "</td>" +
                "<td>" + esc(r.date) + "</td>" +
                "<td>" + esc(r.clock_in) + "</td>" +
                "<td>" + esc(r.clock_out) + "</td>" +
                "<td>" + esc(r.hours) + "</td>" +
                "<td>" + esc(r.late) + "</td>" +
                "<td>" + esc(r.status) + "</td>" +
                "<td>" + esc(r.device) + "</td>" +
                "<td>" +
                decisionCell(r) +
                "</td></tr>"
              );
            })
            .join("")
        : '<tr><td colspan="10" class="muted">No attendance records for this filter.</td></tr>';
    }
  }

  function loadAttendance() {
    var day = (el("att-date") && el("att-date").value) || new Date().toISOString().slice(0, 10);
    if (el("att-date") && !el("att-date").value) el("att-date").value = day;
    var name = (el("att-name") && el("att-name").value) || "";
    return api("/console/attendance?work_date=" + encodeURIComponent(day) + "&name=" + encodeURIComponent(name))
      .then(renderAttendance)
      .catch(function (err) {
        var body = el("att-body");
        if (body) body.innerHTML = '<tr><td colspan="10" class="status">' + esc(err.message) + "</td></tr>";
      });
  }

  var decisionKind = "absent";

  function todayChoice(r) {
    var actions = decisionCell({ status: r.today, employee_id: r.employee_id, date: r.today_date });
    return esc(r.today || "") + (actions ? "<br>" + actions : "");
  }

  function applyDecisionClick(e, afterward) {
    var markA = e.target.closest && e.target.closest("[data-mark-absent]");
    var markD = e.target.closest && e.target.closest("[data-mark-dayoff]");
    var clearA = e.target.closest && e.target.closest("[data-clear-absent]");
    var clearD = e.target.closest && e.target.closest("[data-clear-dayoff]");
    var btn = markA || markD || clearA || clearD;
    if (!btn) return;
    var employeeId = btn.getAttribute("data-mark-absent") || btn.getAttribute("data-mark-dayoff") || btn.getAttribute("data-clear-absent") || btn.getAttribute("data-clear-dayoff") || "";
    var day = btn.getAttribute("data-absent-date") || "";
    var done = markA || markD
      ? postDecision(markD ? "day_off" : "absent", employeeId, day)
      : api(
          (clearD ? "/attendance/day-off" : "/attendance/absent") +
            "?employee_id=" +
            encodeURIComponent(employeeId) +
            "&work_date=" +
            encodeURIComponent(day),
          { method: "DELETE" }
        );
    done
      .then(function () {
        toast(markD ? "Day off saved." : markA ? "Absence saved." : clearD ? "Day off cleared." : "Absence cleared.");
        afterward();
      })
      .catch(function (err) { toast(err.message || "Could not update the day.", true); });
  }

  function decisionCell(r) {
    var id = esc(r.employee_id);
    var day = esc(r.date);
    if (r.status === "Absent") {
      return '<button type="button" class="ghost small-del" data-clear-absent="' + id + '" data-absent-date="' + day + '">Clear</button>';
    }
    if (r.status === "Day off") {
      return '<button type="button" class="ghost small-del" data-clear-dayoff="' + id + '" data-absent-date="' + day + '">Clear</button>';
    }
    if (r.status === "No clock-in") {
      return (
        '<button type="button" class="ghost small-del" data-mark-absent="' +
        id +
        '" data-absent-date="' +
        day +
        '">Absent</button> <button type="button" class="ghost small-del" data-mark-dayoff="' +
        id +
        '" data-absent-date="' +
        day +
        '">Day off</button>'
      );
    }
    return "";
  }

  function postDecision(kind, employeeId, day) {
    var path = kind === "day_off" ? "/attendance/day-off" : "/attendance/absent";
    return api(path, {
      method: "POST",
      body: JSON.stringify({ employee_id: employeeId, work_date: day }),
    });
  }

  function fillAbsentRoster() {
    var day = (el("absent-date") && el("absent-date").value) || (el("att-date") && el("att-date").value) || "";
    var path = "/attendance/roster" + (day ? "?work_date=" + encodeURIComponent(day) : "");
    return api(path).then(function (rows) {
      var sel = el("absent-employee");
      if (!sel) return;
      var current = sel.value;
      sel.innerHTML =
        '<option value="">Choose an employee</option>' +
        (rows || [])
          .map(function (p) {
            var left = p.days_off_left == null ? "" : " · " + p.days_off_left + " of " + (p.days_off_allowance || 4) + " days off left";
            return '<option value="' + esc(p.employee_id) + '">' + esc(p.name) + " · " + esc(p.employee_id) + esc(left) + "</option>";
          })
          .join("");
      if (current) sel.value = current;
    });
  }

  function loadNotifications() {
    return api("/console/notifications")
      .then(function (data) {
        var list = el("notify-list");
        var filter = (el("notify-filter") && el("notify-filter").value) || "";
        var items = (data.items || []).filter(function (n) {
          return !filter || n.type === filter;
        });
        if (list) {
          list.innerHTML = items.length
            ? items
                .map(function (n) {
                  return (
                    '<article class="log-row"><header><span>' +
                    esc(n.title) +
                    '</span><span class="granted-tag">' +
                    esc(n.source) +
                    "</span></header><p class=\"muted\">" +
                    esc(n.message) +
                    " · " +
                    esc(n.time) +
                    '</p><a class="ghost small-del" href="' +
                    esc(n.href) +
                    '">Open</a></article>'
                  );
                })
                .join("")
            : '<p class="muted">No alerts for this filter.</p>';
        }
        var badge = el("notify-count");
        if (badge) badge.textContent = String(data.unread || 0);
      })
      .catch(function (err) {
        var list = el("notify-list");
        if (list) list.innerHTML = '<p class="status">' + esc(err.message) + "</p>";
      });
  }

  function loadSettings() {
    return api("/console/settings")
      .then(function (data) {
        var v = data.values || {};
        if (el("set-company")) el("set-company").value = v.company_name || "";
        if (el("set-tz")) el("set-tz").value = v.timezone || "";
        if (el("set-days")) el("set-days").value = v.working_days || "";
        if (el("set-contact")) el("set-contact").value = v.contact || "";
        if (typeof syncNightControls === "function") syncNightControls();
        setText("sidebar-company", v.company_name || "Bunker Attendance");
        loadPayrollSettings();
      })
      .catch(function (err) {
        var st = el("settings-status");
        if (st) st.textContent = err.message;
      });
  }

  function loadShiftsCalendar() {
    var cal = el("shift-calendar");
    if (!cal) return;
    var days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    api("/shifts")
      .then(function (shifts) {
        cal.innerHTML = days
          .map(function (day) {
            var names = (shifts || [])
              .map(function (s) {
                return esc(s.name) + " " + esc(s.start_time) + "–" + esc(s.end_time);
              })
              .join("<br>");
            return "<div><strong>" + day + "</strong><p class=\"muted\">" + (names || "Rest / unassigned") + "</p></div>";
          })
          .join("");
      })
      .catch(function () {
        cal.innerHTML = '<p class="muted">Could not load shift templates.</p>';
      });
  }

  function stopDeviceCam() {
    if (devicePulse) {
      clearInterval(devicePulse);
      devicePulse = null;
    }
    if (deviceListTimer) {
      clearInterval(deviceListTimer);
      deviceListTimer = null;
    }
    if (deviceStream) {
      stopStream(deviceStream);
      deviceStream = null;
    }
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
      previewUrl = "";
    }
  }

  function selectDevice(d) {
    if (!d) return;
    watchDeviceId = d.id;
    if (el("device-id")) el("device-id").value = d.id;
    if (el("device-name")) el("device-name").value = d.name || "";
    if (el("device-mac")) el("device-mac").value = d.mac_address || "";
    if (el("device-ip")) el("device-ip").value = d.last_ip || "";
    if (el("device-location")) el("device-location").value = d.location || "";
    var restore = el("btn-device-restore");
    if (restore) restore.classList.toggle("hidden", d.status !== "revoked");
    var macNote = d.mac_resolved ? d.mac_address : (d.mac_address || "resolving…");
    setText(
      "device-watch-meta",
      (d.name || "Kiosk") + " · IP " + (d.last_ip || "—") + " · MAC " + macNote
    );
    if (typeof setDeviceMac === "function" && d.mac_resolved) setDeviceMac(d.mac_address);
  }

  function renderNewDeviceBanner(list) {
    var banner = el("device-new");
    if (!banner) return;
    var fresh = [];
    (list || []).forEach(function (d) {
      if (d.is_new) fresh.push(d);
    });
    if (!fresh.length) {
      banner.classList.add("hidden");
      banner.textContent = "";
      return;
    }
    banner.classList.remove("hidden");
    banner.textContent =
      "New kiosk opened: " +
      fresh
        .map(function (d) {
          return (d.name || "Kiosk") + " · " + (d.last_ip || d.mac_address || "unknown");
        })
        .join(" · ") +
      ". Edit the name or remove it.";
  }

  function removeDevice(id, name) {
    if (!id) {
      toast("Select a kiosk first.", true);
      return;
    }
    if (!confirm("Remove " + (name || "this kiosk") + "? If that tablet opens /kiosk again, it will show up as a new device.")) return;
    api("/console/devices/" + id, { method: "DELETE" })
      .then(function () {
        toast("Kiosk removed.");
        if (String(watchDeviceId) === String(id)) {
          watchDeviceId = null;
          if (el("device-id")) el("device-id").value = "";
          if (el("device-name")) el("device-name").value = "";
          if (el("device-mac")) el("device-mac").value = "";
          if (el("device-ip")) el("device-ip").value = "";
          if (el("device-location")) el("device-location").value = "";
          setText("device-watch-meta", "Waiting for a kiosk at /kiosk…");
        }
        loadDevices();
      })
      .catch(function (err) {
        toast(err.message, true);
      });
  }

  function refreshTabletPreview() {
    if (!watchDeviceId) return;
    var token = typeof getAuthToken === "function" ? getAuthToken() : "";
    fetch("/api/v1/console/devices/" + watchDeviceId + "/preview?t=" + encodeURIComponent(token), {
      credentials: "include",
    })
      .then(function (res) {
        if (!res.ok) throw new Error("waiting");
        return res.blob();
      })
      .then(function (blob) {
        var img = el("device-live");
        if (!img) return;
        var url = URL.createObjectURL(blob);
        img.src = url;
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = url;
        setText("device-cam-status", "Live from the tablet.");
      })
      .catch(function () {
        setText("device-cam-status", "Waiting for the tablet camera. Open /kiosk and tap to start camera.");
      });
  }

  function renderDevices(list) {
    var body = el("devices-body");
    if (!body) return;
    if (!list || !list.length) {
      body.innerHTML = '<tr><td colspan="7" class="muted">No kiosk yet. On the tablet open /kiosk and tap to start the camera.</td></tr>';
      return;
    }
    if (!watchDeviceId) {
      var first = null;
      list.forEach(function (d) {
        if (!first && d.status !== "revoked") first = d;
      });
      if (first) selectDevice(first);
    } else {
      var form = el("device-form");
      var typing = form && document.activeElement && form.contains(document.activeElement);
      if (!typing) {
        list.forEach(function (d) {
          if (d.id === watchDeviceId) selectDevice(d);
        });
      }
    }
    body.innerHTML = list
      .map(function (d) {
        var watching = watchDeviceId === d.id ? " · watching" : "";
        var macLabel = d.mac_resolved ? d.mac_address : "Resolving…";
        var cam = d.has_preview ? "Live" : d.last_camera === "live" ? "On" : "Idle";
        var mark = d.is_new ? '<span class="badge">New</span> ' : "";
        var extra =
          d.status === "revoked"
            ? '<button class="ghost small-del" type="button" data-restore-device="' +
              d.id +
              '">Restore</button>'
            : '<button class="ghost small-del danger" type="button" data-revoke-device="' +
              d.id +
              '">Revoke</button>';
        return (
          "<tr>" +
          "<td>" + mark + esc(d.name) + watching + "</td>" +
          "<td>" + esc(d.last_ip || "—") + "</td>" +
          "<td>" + esc(macLabel) + "</td>" +
          "<td>" + esc(cam) + "</td>" +
          "<td>" + esc(d.live_status) + "</td>" +
          "<td>" + esc(String(d.last_seen || "—").replace("T", " ").slice(0, 16)) + "</td>" +
          '<td class="people-actions device-actions">' +
          '<button class="ghost small-del" type="button" data-edit-device="' +
          d.id +
          '">Edit</button>' +
          '<button class="ghost small-del" type="button" data-watch-device="' +
          d.id +
          '">Watch</button>' +
          extra +
          '<button class="ghost small-del danger" type="button" data-remove-device="' +
          d.id +
          '">Remove</button>' +
          "</td></tr>"
        );
      })
      .join("");
  }

  function loadDevices() {
    return api("/console/devices")
      .then(function (data) {
        devicesCache = data.devices || [];
        if (devicePollReady) {
          devicesCache.forEach(function (d) {
            if (d.is_new && !knownDeviceIds[d.id]) {
              toast("New kiosk opened: " + (d.name || "tablet") + ".");
            }
          });
        }
        devicesCache.forEach(function (d) {
          knownDeviceIds[d.id] = true;
        });
        devicePollReady = true;
        renderNewDeviceBanner(devicesCache);
        renderDevices(devicesCache);
      })
      .catch(function (err) {
        var body = el("devices-body");
        if (body) body.innerHTML = '<tr><td colspan="7" class="status">' + esc(err.message) + "</td></tr>";
      });
  }

  function startDeviceWatch() {
    loadDevices();
    if (deviceListTimer) clearInterval(deviceListTimer);
    deviceListTimer = setInterval(loadDevices, 3000);
    if (devicePulse) clearInterval(devicePulse);
    devicePulse = setInterval(refreshTabletPreview, 700);
    refreshTabletPreview();
  }

  window.codaiOnPage = function (key) {
    setText("top-crumbs", "Bunker Attendance Manager / " + (TITLES[key] || key));
    setNavOpen(false);
    if (key !== "devices") stopDeviceCam();
    if (key === "dashboard") startDashWatch();
    else stopDashWatch();
    if (key === "people") loadPeopleConsole();
    if (key === "attendance") loadAttendance();
    if (key === "shifts") loadShiftsCalendar();
    if (key === "branches") previewPanel("branches-panel", "branches");
    if (key === "devices") startDeviceWatch();
    if (key === "integrations") previewPanel("integrations-panel", "integrations");
    if (key === "notifications") loadNotifications();
    if (key === "settings") loadSettings();
    if (key === "payroll") loadPayrollPage();
    if (key === "reports") loadNotifications();
  };

  window.codaiApplyUser = applyUser;

  var openNav = el("btn-open-nav");
  var closeNav = el("btn-close-nav");
  var toggle = el("btn-sidebar-toggle");
  var back = el("sidebar-backdrop");
  if (openNav) {
    openNav.addEventListener("click", function () {
      setNavOpen(!document.body.classList.contains("nav-open"));
    });
  }
  if (closeNav) closeNav.addEventListener("click", function () { setNavOpen(false); });
  if (toggle) {
    toggle.addEventListener("click", function () {
      if (window.matchMedia && window.matchMedia("(max-width: 900px)").matches) {
        setNavOpen(true);
      } else {
        document.body.classList.toggle("sidebar-collapsed");
      }
    });
  }
  if (back) back.addEventListener("click", function () { setNavOpen(false); });
  document.addEventListener("keydown", function (evt) {
    if (evt.key === "Escape") setNavOpen(false);
  });

  var helpBtn = el("btn-help");
  var helpDlg = el("help-dialog");
  if (helpBtn && helpDlg && helpDlg.showModal) {
    helpBtn.addEventListener("click", function () { helpDlg.showModal(); });
  } else if (helpBtn) {
    helpBtn.addEventListener("click", function () {
      toast("Enroll faces here. Employees clock in on /kiosk.");
    });
  }
  var notifyBtn = el("btn-notify");
  if (notifyBtn) {
    notifyBtn.addEventListener("click", function () {
      if (typeof showPage === "function") showPage("notifications");
      else window.location.href = "/admin?page=notifications";
    });
  }

  var search = el("global-search");
  if (search) {
    search.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      var peopleSearch = el("people-search");
      if (peopleSearch) peopleSearch.value = search.value;
      if (typeof showPage === "function") showPage("people");
      else window.location.href = "/admin?page=people";
    });
  }

  ["people-search", "people-branch", "people-dept", "people-shift", "people-face", "people-status-filter"].forEach(function (id) {
    var node = el(id);
    if (node) node.addEventListener("input", function () { peoplePage = 0; renderPeople(); });
    if (node) node.addEventListener("change", function () { peoplePage = 0; renderPeople(); });
  });

  document.addEventListener("click", function (e) {
    var prev = e.target.closest && e.target.closest("#people-prev");
    var next = e.target.closest && e.target.closest("#people-next");
    if (prev) {
      peoplePage = Math.max(0, peoplePage - 1);
      renderPeople();
    }
    if (next) {
      peoplePage += 1;
      renderPeople();
    }
    var previewBtn = e.target.closest && e.target.closest("[data-preview-action]");
    if (previewBtn) {
      var module = previewBtn.getAttribute("data-preview-action");
      fetch("/api/v1/console/preview/" + module + "/action", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + (getAuthToken() || "") },
      })
        .then(function (res) { return res.json(); })
        .then(function (data) {
          toast(data.detail || "Not saved — preview only.", true);
        })
        .catch(function () {
          toast("Not saved — preview only.", true);
        });
    }
    var empAtt = e.target.closest && e.target.closest("[data-emp-att]");
    if (empAtt) {
      openExportDialog("monthly", empAtt.getAttribute("data-emp-att"));
      return;
    }
    var empRep = e.target.closest && e.target.closest("[data-emp-rep]");
    if (empRep) {
      downloadEmployeeReport(empRep.getAttribute("data-emp-rep"));
      return;
    }
    var deact = e.target.closest && e.target.closest("[data-deactivate-person]");
    if (deact) {
      var did = deact.getAttribute("data-deactivate-person");
      if (!confirm("Deactivate employee " + did + "? They stay in the directory with inactive status.")) return;
      api("/console/employees/" + encodeURIComponent(did) + "/status", {
        method: "POST",
        body: JSON.stringify({ status: "inactive" }),
      })
        .then(function () {
          toast("Employee marked inactive.");
          loadPeopleConsole();
        })
        .catch(function (err) { toast(err.message, true); });
      return;
    }
    var editDev = e.target.closest && e.target.closest("[data-edit-device]");
    if (editDev) {
      var eid = Number(editDev.getAttribute("data-edit-device"));
      devicesCache.forEach(function (d) {
        if (d.id === eid) selectDevice(d);
      });
      var form = el("device-form");
      if (form && form.scrollIntoView) form.scrollIntoView({ block: "start" });
      var nameInput = el("device-name");
      if (nameInput) nameInput.focus();
      return;
    }
    var watchBtn = e.target.closest && e.target.closest("[data-watch-device]");
    if (watchBtn) {
      var wid = Number(watchBtn.getAttribute("data-watch-device"));
      devicesCache.forEach(function (d) {
        if (d.id === wid) selectDevice(d);
      });
      refreshTabletPreview();
      loadDevices();
      return;
    }
    var removeDev = e.target.closest && e.target.closest("[data-remove-device]");
    if (removeDev) {
      var rid = removeDev.getAttribute("data-remove-device");
      var rname = "";
      devicesCache.forEach(function (d) {
        if (String(d.id) === String(rid)) rname = d.name || "";
      });
      removeDevice(rid, rname);
      return;
    }
    var restoreDev = e.target.closest && e.target.closest("[data-restore-device]");
    if (restoreDev) {
      api("/console/devices/" + restoreDev.getAttribute("data-restore-device"), {
        method: "PATCH",
        body: JSON.stringify({ status: "active" }),
      })
        .then(function () {
          toast("Kiosk restored.");
          loadDevices();
        })
        .catch(function (err) { toast(err.message, true); });
      return;
    }
    var revokeDev = e.target.closest && e.target.closest("[data-revoke-device]");
    if (revokeDev) {
      if (!confirm("Revoke this tablet? It stays listed, but opening /kiosk will not turn it back on until you Restore or Edit it.")) return;
      api("/console/devices/" + revokeDev.getAttribute("data-revoke-device"), {
        method: "PATCH",
        body: JSON.stringify({ status: "revoked" }),
      })
        .then(function () {
          toast("Device revoked.");
          loadDevices();
        })
        .catch(function (err) { toast(err.message, true); });
      return;
    }
    var reportBtn = e.target.closest && e.target.closest("[data-report]");
    if (reportBtn && el("report-period")) {
      var kind = reportBtn.getAttribute("data-report");
      el("report-period").value = kind === "monthly" ? "monthly" : "daily";
      if (typeof loadReports === "function") loadReports();
    }
  });

  var attLoad = el("btn-att-load");
  if (attLoad) attLoad.addEventListener("click", loadAttendance);
  var attStatus = el("att-status");
  if (attStatus) attStatus.addEventListener("change", function () { renderAttendance(attCache); });
  var staffBody = el("dash-staff-body");
  if (staffBody) {
    staffBody.addEventListener("click", function (e) {
      applyDecisionClick(e, function () { loadOverview(); });
    });
  }
  var attBody = el("att-body");
  if (attBody) {
    attBody.addEventListener("click", function (e) {
      applyDecisionClick(e, function () { loadAttendance(); });
    });
  }

  var exportMeta = null;
  var exportBusy = false;
  var exportCountTimer = null;
  var exportColKeys = [];
  var exportColDefaults = [];

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function isoLocal(d) {
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  function monthRange(d) {
    return {
      start: isoLocal(new Date(d.getFullYear(), d.getMonth(), 1)),
      end: isoLocal(new Date(d.getFullYear(), d.getMonth() + 1, 0)),
    };
  }

  function fillSelect(node, items, valueKey, labelKey, blank) {
    if (!node) return;
    var current = node.value;
    node.innerHTML = "";
    var all = document.createElement("option");
    all.value = "";
    all.textContent = blank;
    node.appendChild(all);
    (items || []).forEach(function (item) {
      var opt = document.createElement("option");
      if (typeof item === "string") {
        opt.value = item;
        opt.textContent = item;
      } else {
        opt.value = item[valueKey];
        opt.textContent = item[labelKey] + " (" + item[valueKey] + ")";
      }
      node.appendChild(opt);
    });
    if (current) node.value = current;
  }

  function renderExportCols() {
    var box = el("export-cols");
    var report = el("export-report") && el("export-report").value;
    if (!box || !exportMeta) return;
    var isPeople = report === "employees";
    exportColKeys = isPeople ? exportMeta.directory_columns : exportMeta.detail_columns;
    exportColDefaults = isPeople ? exportMeta.directory_default : exportMeta.detail_default;
    box.innerHTML = "";
    exportColKeys.forEach(function (key) {
      var lab = document.createElement("label");
      lab.className = "export-check";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = key;
      cb.checked = exportColDefaults.indexOf(key) !== -1;
      lab.appendChild(cb);
      lab.appendChild(document.createTextNode(" " + key.replace(/_/g, " ")));
      box.appendChild(lab);
    });
  }

  function selectedExportCols() {
    var out = [];
    var box = el("export-cols");
    if (!box) return out;
    var boxes = box.querySelectorAll("input[type=checkbox]");
    for (var i = 0; i < boxes.length; i += 1) {
      if (boxes[i].checked) out.push(boxes[i].value);
    }
    return out;
  }

  function exportQuery() {
    var report = (el("export-report") && el("export-report").value) || "monthly";
    var fmt = (el("export-format") && el("export-format").value) || "xlsx";
    var start = (el("export-start") && el("export-start").value) || "";
    var end = (el("export-end") && el("export-end").value) || "";
    var branch = (el("export-branch") && el("export-branch").value) || "";
    var dept = (el("export-dept") && el("export-dept").value) || "";
    var emp = (el("export-employee") && el("export-employee").value) || "";
    var status = (el("export-status") && el("export-status").value) || "";
    var inactive = el("export-inactive") && el("export-inactive").checked ? "1" : "";
    var cols = selectedExportCols().join(",");
    return (
      "report=" + encodeURIComponent(report) +
      "&format=" + encodeURIComponent(fmt) +
      "&start_date=" + encodeURIComponent(start) +
      "&end_date=" + encodeURIComponent(end) +
      "&branch=" + encodeURIComponent(branch) +
      "&department=" + encodeURIComponent(dept) +
      "&employee_id=" + encodeURIComponent(emp) +
      "&status=" + encodeURIComponent(status) +
      "&columns=" + encodeURIComponent(cols) +
      "&include_inactive=" + encodeURIComponent(inactive)
    );
  }

  function showExportError(msg) {
    var node = el("export-error");
    if (!node) return;
    if (msg) {
      node.hidden = false;
      node.textContent = msg;
    } else {
      node.hidden = true;
      node.textContent = "";
    }
  }

  function validateExportForm() {
    var start = el("export-start") && el("export-start").value;
    var end = el("export-end") && el("export-end").value;
    if (!start || !end) return "Choose a start date and an end date.";
    if (end < start) return "End date must be on or after the start date.";
    var a = new Date(start + "T00:00:00");
    var b = new Date(end + "T00:00:00");
    var days = Math.round((b - a) / 86400000) + 1;
    if (days > 366) return "Date range cannot exceed 366 days.";
    if (!selectedExportCols().length) return "Select at least one column.";
    return "";
  }

  function refreshExportCount() {
    var err = validateExportForm();
    var countEl = el("export-count");
    if (err) {
      if (countEl) countEl.textContent = err;
      return;
    }
    if (countEl) countEl.textContent = "Estimating records…";
    api("/exports/count?" + exportQuery())
      .then(function (data) {
        if (!countEl) return;
        var note = data.hours_note ? " " + data.hours_note : "";
        countEl.textContent = (data.count || 0) + " records match these filters." + note;
      })
      .catch(function (err) {
        if (countEl) countEl.textContent = err.message || "Could not estimate records.";
      });
  }

  function scheduleExportCount() {
    if (exportCountTimer) clearTimeout(exportCountTimer);
    exportCountTimer = setTimeout(refreshExportCount, 280);
  }

  function applyExportDefaults(kind) {
    var report = kind || "monthly";
    var period = el("report-period") && el("report-period").value;
    if (!kind && period === "daily") report = "daily";
    if (el("export-report")) el("export-report").value = report;
    var today = isoLocal(new Date());
    var month = monthRange(new Date());
    if (report === "daily") {
      var attDate = el("att-date") && el("att-date").value;
      if (el("export-start")) el("export-start").value = attDate || today;
      if (el("export-end")) el("export-end").value = attDate || today;
    } else {
      if (el("export-start")) el("export-start").value = month.start;
      if (el("export-end")) el("export-end").value = month.end;
    }
    if (el("export-branch") && el("people-branch") && el("people-branch").value) {
      el("export-branch").value = el("people-branch").value;
    }
    if (el("export-dept") && el("people-dept") && el("people-dept").value) {
      el("export-dept").value = el("people-dept").value;
    }
    var attStatus = el("att-status") && el("att-status").value;
    if (el("export-status") && attStatus) {
      el("export-status").value = attStatus === "Missing Check-out" ? "Incomplete" : attStatus;
    }
    if (el("export-inactive") && el("people-status-filter")) {
      el("export-inactive").checked = el("people-status-filter").value === "inactive";
    }
    renderExportCols();
  }

  function downloadEmployeeReport(employeeId) {
    var token = typeof getAuthToken === "function" ? getAuthToken() : "";
    var headers = {};
    if (token) headers.Authorization = "Bearer " + token;
    var day = isoLocal(new Date());
    var qs =
      "period=monthly&work_date=" +
      encodeURIComponent(day) +
      "&employee_id=" +
      encodeURIComponent(employeeId || "");
    fetch(API + "/reports/excel?" + qs, { credentials: "include", headers: headers })
      .then(function (res) {
        if (!res.ok) throw new Error(res.statusText || "Report download failed.");
        var disp = res.headers.get("Content-Disposition") || "";
        var match = /filename=\"?([^\";]+)\"?/.exec(disp);
        var name = match ? match[1] : "BunkerAttendance-employee-report.xlsx";
        return res.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = url;
          a.download = name;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
          toast("Employee report download started.");
        });
      })
      .catch(function (err) {
        toast(err.message || "Report download failed.", true);
      });
  }

  function openExportDialog(kind, employeeId) {
    var dlg = el("export-dialog");
    if (!dlg) {
      toast("Export dialog is missing.", true);
      return;
    }
    showExportError("");
    var ready = exportMeta
      ? Promise.resolve(exportMeta)
      : api("/exports/meta").then(function (meta) {
          exportMeta = meta;
          fillSelect(el("export-branch"), meta.branches || [], "", "", "All branches");
          fillSelect(el("export-dept"), meta.departments || [], "", "", "All departments");
          fillSelect(el("export-employee"), meta.employees || [], "employee_id", "name", "All employees");
          return meta;
        });
    ready
      .then(function () {
        applyExportDefaults(kind);
        if (employeeId && el("export-employee")) el("export-employee").value = employeeId;
        scheduleExportCount();
        if (dlg.showModal) dlg.showModal();
        else dlg.setAttribute("open", "open");
      })
      .catch(function (err) {
        toast(err.message || "Could not load export options.", true);
      });
  }

  function closeExportDialog() {
    var dlg = el("export-dialog");
    if (!dlg) return;
    if (dlg.close) dlg.close();
    else dlg.removeAttribute("open");
  }

  function runExportDownload() {
    if (exportBusy) return;
    var err = validateExportForm();
    if (err) {
      showExportError(err);
      return;
    }
    showExportError("");
    exportBusy = true;
    var btn = el("export-submit");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Generating…";
    }
    var token = typeof getAuthToken === "function" ? getAuthToken() : "";
    var headers = {};
    if (token) headers.Authorization = "Bearer " + token;
    fetch(API + "/exports?" + exportQuery(), { credentials: "include", headers: headers })
      .then(function (res) {
        var ctype = res.headers.get("content-type") || "";
        if (!res.ok) {
          if (ctype.indexOf("json") !== -1) {
            return res.json().then(function (data) {
              var detail = data && data.detail;
              throw new Error(typeof detail === "string" ? detail : res.statusText);
            });
          }
          throw new Error(res.statusText || "Export failed.");
        }
        var disp = res.headers.get("Content-Disposition") || "";
        var match = /filename=\"?([^\";]+)\"?/.exec(disp);
        var name = match ? match[1] : "BunkerAttendance_export";
        return res.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          var a = document.createElement("a");
          a.href = url;
          a.download = name;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
          toast("Download started.");
          closeExportDialog();
        });
      })
      .catch(function (error) {
        showExportError(error.message || "Export failed. You can retry.");
        toast(error.message || "Export failed.", true);
      })
      .then(function () {
        exportBusy = false;
        if (btn) {
          btn.disabled = false;
          btn.textContent = "Download";
        }
      });
  }

  ["dash-export", "btn-people-export", "btn-att-export", "btn-reports-export"].forEach(function (id) {
    var btn = el(id);
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      openExportDialog(btn.getAttribute("data-export") || "");
    });
  });
  var exportForm = el("export-form");
  if (exportForm) {
    exportForm.addEventListener("submit", function (e) {
      e.preventDefault();
      runExportDownload();
    });
  }
  var exportCancel = el("export-cancel");
  if (exportCancel) exportCancel.addEventListener("click", closeExportDialog);
  var exportReport = el("export-report");
  if (exportReport) {
    exportReport.addEventListener("change", function () {
      renderExportCols();
      scheduleExportCount();
    });
  }
  ["export-format", "export-start", "export-end", "export-branch", "export-dept", "export-employee", "export-status", "export-inactive"].forEach(function (id) {
    var node = el(id);
    if (node) node.addEventListener("change", scheduleExportCount);
  });
  var colsAll = el("export-cols-all");
  if (colsAll) {
    colsAll.addEventListener("click", function () {
      var box = el("export-cols");
      if (!box) return;
      var boxes = box.querySelectorAll("input[type=checkbox]");
      for (var i = 0; i < boxes.length; i += 1) boxes[i].checked = true;
      scheduleExportCount();
    });
  }
  var colsReset = el("export-cols-reset");
  if (colsReset) {
    colsReset.addEventListener("click", function () {
      renderExportCols();
      scheduleExportCount();
    });
  }
  var exportCols = el("export-cols");
  if (exportCols) {
    exportCols.addEventListener("change", scheduleExportCount);
  }

  var importer = el("people-import");
  if (importer) {
    importer.addEventListener("change", function () {
      var file = importer.files && importer.files[0];
      if (!file) return;
      var reader = new FileReader();
      reader.onload = function () {
        var lines = String(reader.result || "").split(/\r?\n/);
        var started = 0;
        lines.forEach(function (line, idx) {
          if (!idx || !line.trim()) return;
          var parts = line.split(",");
          if (parts.length < 2) return;
          started += 1;
          api("/console/employees", {
            method: "POST",
            body: JSON.stringify({
              employee_id: parts[0].trim(),
              name: parts[1].trim(),
              email: (parts[2] || "").trim(),
              department: (parts[3] || "").trim(),
              branch: (parts[4] || "").trim() || "HQ",
            }),
          }).catch(function (err) {
            toast(err.message, true);
          });
        });
        toast(started ? "Import started for " + started + " rows." : "No data rows found.");
        setTimeout(loadPeopleConsole, 800);
      };
      reader.readAsText(file);
    });
  }

  var settingsForm = el("settings-form");
  if (settingsForm) {
    settingsForm.addEventListener("submit", function (e) {
      e.preventDefault();
      api("/console/settings", {
        method: "PUT",
        body: JSON.stringify({
          company_name: el("set-company").value,
          timezone: el("set-tz").value,
          working_days: el("set-days").value,
          contact: el("set-contact").value,
        }),
      })
        .then(function () {
          toast("Settings saved.");
          setText("settings-status", "Saved.");
          loadOverview();
        })
        .catch(function (err) {
          toast(err.message, true);
          setText("settings-status", err.message);
        });
    });
  }

  var notifyFilter = el("notify-filter");
  if (notifyFilter) notifyFilter.addEventListener("change", loadNotifications);

  var deviceForm = el("device-form");
  if (deviceForm) {
    deviceForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var id = el("device-id") && el("device-id").value;
      if (!id) {
        toast("Open the kiosk on the tablet first.", true);
        return;
      }
      api("/console/devices/" + id, {
        method: "PATCH",
        body: JSON.stringify({
          name: el("device-name").value,
          location: el("device-location").value || "",
          status: "active",
        }),
      })
        .then(function () {
          toast("Kiosk saved.");
          setText("device-form-status", "Saved.");
          loadDevices();
        })
        .catch(function (err) {
          toast(err.message, true);
          setText("device-form-status", err.message);
        });
    });
  }
  var removeBtn = el("btn-device-remove");
  if (removeBtn) {
    removeBtn.addEventListener("click", function () {
      var id = el("device-id") && el("device-id").value;
      var name = el("device-name") && el("device-name").value;
      removeDevice(id, name);
    });
  }
  var restoreBtn = el("btn-device-restore");
  if (restoreBtn) {
    restoreBtn.addEventListener("click", function () {
      var id = el("device-id") && el("device-id").value;
      if (!id) {
        toast("Select a kiosk first.", true);
        return;
      }
      api("/console/devices/" + id, {
        method: "PATCH",
        body: JSON.stringify({ status: "active" }),
      })
        .then(function () {
          toast("Kiosk restored.");
          setText("device-form-status", "Restored.");
          loadDevices();
        })
        .catch(function (err) {
          toast(err.message, true);
        });
    });
  }

  api("/auth/me")
    .then(applyUser)
    .catch(function () {});

  var payRun = null;
  var payCurrency = "EGP";

  function monthBounds() {
    var now = new Date();
    var start = new Date(now.getFullYear(), now.getMonth(), 1);
    var end = new Date(now.getFullYear(), now.getMonth() + 1, 0);
    function iso(d) {
      var mm = String(d.getMonth() + 1);
      var dd = String(d.getDate());
      if (mm.length < 2) mm = "0" + mm;
      if (dd.length < 2) dd = "0" + dd;
      return d.getFullYear() + "-" + mm + "-" + dd;
    }
    return { start: iso(start), end: iso(end) };
  }

  function money(v, cur) {
    var n = Number(v);
    var c = cur || payCurrency || "EGP";
    if (!isFinite(n)) return String(v == null ? "0" : v) + " " + c;
    return n.toFixed(2) + " " + c;
  }

  function setVal(id, value) {
    var node = el(id);
    if (node) node.value = value == null ? "" : String(value);
  }

  function loadPayrollSettings() {
    return api("/payroll/settings")
      .then(function (data) {
        var v = (data && data.values) || {};
        payCurrency = v.currency || "EGP";
        setVal("pay-currency", v.currency);
        setVal("pay-divisor", v.divisor);
        setVal("pay-hours", v.hours_per_day);
        setVal("pay-round", v.rounding);
        setVal("pay-prec", v.rounding_precision);
        setVal("pay-effective", v.effective_date);
        setVal("pay-leave", v.paid_leave_days);
        setVal("pay-unused", v.unused_leave_compensation);
        setVal("pay-payout", v.unused_payout);
        setVal("pay-carry", v.leave_carry_forward);
        setVal("pay-carry-max", v.max_carry_forward);
        setVal("pay-holidays", v.public_holidays);
      })
      .catch(function (err) {
        setText("payroll-settings-status", err.message || "Could not load payroll rules.");
      });
  }

  function renderPayroll(data) {
    payRun = data;
    payCurrency = (data && data.currency) || payCurrency;
    var cards = el("pay-cards");
    if (cards) {
      cards.innerHTML =
        '<article class="stat-card"><span>Total payroll</span><strong>' + esc(money(data.total_payroll)) + "</strong></article>" +
        '<article class="stat-card"><span>Employees</span><strong>' + esc(String(data.employee_count || 0)) + "</strong></article>" +
        '<article class="stat-card"><span>Additions</span><strong>' + esc(money(data.total_additions)) + "</strong></article>" +
        '<article class="stat-card"><span>Deductions</span><strong>' + esc(money(data.total_deductions)) + "</strong></article>" +
        '<article class="stat-card"><span>Needs review</span><strong>' + esc(String(data.outstanding_review || 0)) + "</strong></article>" +
        '<article class="stat-card"><span>Status</span><strong>' + esc(data.status || "preview") + "</strong></article>";
    }
    setText("pay-status", (data.status || "preview") + (data.period_id ? " · run #" + data.period_id : ""));
    var q = ((el("pay-search") && el("pay-search").value) || "").toLowerCase();
    var rows = (data.employees || []).filter(function (r) {
      if (!q) return true;
      return String(r.name || "").toLowerCase().indexOf(q) !== -1 || String(r.employee_id || "").toLowerCase().indexOf(q) !== -1;
    });
    var table = el("pay-table");
    if (!table) return;
    if (!rows.length) {
      table.innerHTML = '<p class="muted">No employees in this period.</p>';
      return;
    }
    var body = rows
      .map(function (r) {
        var flag = r.flags && r.flags.length ? ' <span class="badge badge-expired">check records</span>' : "";
        return (
          "<tr data-pay-eid=\"" +
          esc(r.employee_id) +
          "\"><td>" +
          esc(r.name) +
          flag +
          "</td><td>" +
          esc(r.employee_id) +
          "</td><td>" +
          esc(money(r.base_salary)) +
          "</td><td>" +
          esc(r.unused_leave_days) +
          "</td><td>" +
          esc(String(r.chargeable_late_minutes || 0)) +
          "</td><td>" +
          esc(r.absence_days) +
          "</td><td>" +
          esc(money(r.additions_total)) +
          "</td><td>" +
          esc(money(r.deductions_total)) +
          "</td><td>" +
          esc(money(r.net_pay)) +
          "</td></tr>"
        );
      })
      .join("");
    table.innerHTML =
      '<table class="report-table"><thead><tr><th>Name</th><th>ID</th><th>Base</th><th>Leave bal.</th><th>Late min</th><th>Absence</th><th>Additions</th><th>Deductions</th><th>Net</th></tr></thead><tbody>' +
      body +
      "</tbody></table>";
  }

  function showPayDetail(eid) {
    if (!payRun || !payRun.employees) return;
    var row = null;
    payRun.employees.forEach(function (r) {
      if (r.employee_id === eid) row = r;
    });
    if (!row) return;
    var empty = el("pay-detail-empty");
    var box = el("pay-detail");
    var form = el("pay-profile-form");
    var leaveForm = el("pay-leave-form");
    if (empty) empty.classList.add("hidden");
    if (form) form.classList.remove("hidden");
    if (leaveForm) leaveForm.classList.remove("hidden");
    if (box) {
      box.classList.remove("hidden");
      var lines = (row.lines || [])
        .map(function (ln) {
          return "<li>" + esc(ln.label) + ": " + esc(money(ln.amount)) + "</li>";
        })
        .join("");
      var notes = (row.explanation || []).map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("");
      var inherit = row.inherited || {};
      box.innerHTML =
        "<p><strong>" +
        esc(row.name) +
        "</strong> · " +
        esc(row.employee_id) +
        "</p><ul>" +
        lines +
        "</ul><p class=\"muted\">Inherited: salary " +
        (inherit.monthly_salary ? "company default" : "override") +
        ", leave " +
        (inherit.leave_allowance ? "company default" : "override") +
        ".</p>" +
        (notes ? "<p class=\"muted\">Notes</p><ul>" + notes + "</ul>" : "");
    }
    setVal("pay-eid", eid);
    api("/payroll/profiles/" + encodeURIComponent(eid))
      .then(function (p) {
        setVal("pay-emp-salary", p.monthly_salary);
        setVal("pay-emp-leave", p.leave_allowance_override || "");
        setVal("pay-emp-unused", p.unused_leave_eligible == null ? "inherit" : p.unused_leave_eligible ? "1" : "0");
        setVal("pay-emp-effective", p.effective_date);
      })
      .catch(function () {});
  }

  function loadPayrollPage() {
    var bounds = monthBounds();
    if (el("pay-start") && !el("pay-start").value) el("pay-start").value = bounds.start;
    if (el("pay-end") && !el("pay-end").value) el("pay-end").value = bounds.end;
    var start = el("pay-start") && el("pay-start").value;
    var end = el("pay-end") && el("pay-end").value;
    if (!start || !end) return;
    var box = el("pay-table");
    if (box) box.innerHTML = '<p class="muted">Loading…</p>';
    api("/payroll/preview?start_date=" + encodeURIComponent(start) + "&end_date=" + encodeURIComponent(end))
      .then(renderPayroll)
      .catch(function (err) {
        if (box) box.innerHTML = '<p class="status">' + esc(err.message) + "</p>";
        toast(err.message || "Payroll preview failed.", true);
      });
  }

  function payrollRange() {
    return {
      start_date: (el("pay-start") && el("pay-start").value) || "",
      end_date: (el("pay-end") && el("pay-end").value) || "",
    };
  }

  var paySettingsForm = el("payroll-settings-form");
  if (paySettingsForm) {
    paySettingsForm.addEventListener("submit", function (e) {
      e.preventDefault();
      api("/payroll/settings", {
        method: "PUT",
        body: JSON.stringify({
          currency: el("pay-currency") && el("pay-currency").value,
          divisor: el("pay-divisor") && el("pay-divisor").value,
          hours_per_day: el("pay-hours") && el("pay-hours").value,
          rounding: el("pay-round") && el("pay-round").value,
          rounding_precision: el("pay-prec") && el("pay-prec").value,
          effective_date: el("pay-effective") && el("pay-effective").value,
          paid_leave_days: el("pay-leave") && el("pay-leave").value,
          unused_leave_compensation: el("pay-unused") && el("pay-unused").value,
          unused_payout: el("pay-payout") && el("pay-payout").value,
          leave_carry_forward: el("pay-carry") && el("pay-carry").value,
          max_carry_forward: el("pay-carry-max") && el("pay-carry-max").value,
          public_holidays: el("pay-holidays") && el("pay-holidays").value,
        }),
      })
        .then(function () {
          toast("Payroll rules saved.");
          setText("payroll-settings-status", "Saved.");
        })
        .catch(function (err) {
          toast(err.message, true);
          setText("payroll-settings-status", err.message);
        });
    });
  }

  if (el("pay-preview")) {
    el("pay-preview").addEventListener("click", loadPayrollPage);
  }
  if (el("pay-calc")) {
    el("pay-calc").addEventListener("click", function () {
      var range = payrollRange();
      api("/payroll/calculate", { method: "POST", body: JSON.stringify(range) })
        .then(function (data) {
          renderPayroll(data);
          toast("Payroll calculated.");
        })
        .catch(function (err) {
          toast(err.message || "Calculate failed.", true);
        });
    });
  }
  function postPayStatus(status) {
    if (!payRun || !payRun.period_id) {
      toast("Calculate payroll first.", true);
      return;
    }
    api("/payroll/periods/" + payRun.period_id + "/status", { method: "POST", body: JSON.stringify({ status: status }) })
      .then(function (data) {
        renderPayroll(data);
        toast("Payroll is now " + status + ".");
      })
      .catch(function (err) {
        toast(err.message, true);
      });
  }
  if (el("pay-submit")) el("pay-submit").addEventListener("click", function () { postPayStatus("pending_approval"); });
  if (el("pay-approve")) el("pay-approve").addEventListener("click", function () { postPayStatus("approved"); });
  if (el("pay-finalize")) el("pay-finalize").addEventListener("click", function () { postPayStatus("finalized"); });
  function downloadPay(fmt) {
    if (!payRun || !payRun.period_id) {
      toast("Calculate payroll first.", true);
      return;
    }
    fetch(API + "/payroll/periods/" + payRun.period_id + "/export?format=" + encodeURIComponent(fmt), { credentials: "include" })
      .then(function (res) {
        if (!res.ok) throw new Error("Export failed.");
        return res.blob().then(function (blob) {
          var a = document.createElement("a");
          a.href = URL.createObjectURL(blob);
          a.download = "payroll." + fmt;
          a.click();
        });
      })
      .catch(function (err) {
        toast(err.message, true);
      });
  }
  if (el("pay-csv")) el("pay-csv").addEventListener("click", function () { downloadPay("csv"); });
  if (el("pay-xlsx")) el("pay-xlsx").addEventListener("click", function () { downloadPay("xlsx"); });
  if (el("pay-search")) el("pay-search").addEventListener("input", function () { if (payRun) renderPayroll(payRun); });
  if (el("pay-table")) {
    el("pay-table").addEventListener("click", function (ev) {
      var tr = ev.target && ev.target.closest ? ev.target.closest("tr[data-pay-eid]") : null;
      if (tr) showPayDetail(tr.getAttribute("data-pay-eid"));
    });
  }
  var payProfileForm = el("pay-profile-form");
  if (payProfileForm) {
    payProfileForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var eid = el("pay-eid") && el("pay-eid").value;
      if (!eid) return;
      var unused = el("pay-emp-unused") && el("pay-emp-unused").value;
      var body = {
        monthly_salary: el("pay-emp-salary") && el("pay-emp-salary").value,
        leave_allowance_override: el("pay-emp-leave") && el("pay-emp-leave").value,
        effective_date: el("pay-emp-effective") && el("pay-emp-effective").value,
      };
      if (unused === "1") body.unused_leave_eligible = true;
      if (unused === "0") body.unused_leave_eligible = false;
      api("/payroll/profiles/" + encodeURIComponent(eid), { method: "PUT", body: JSON.stringify(body) })
        .then(function () {
          toast("Employee pay profile saved.");
          loadPayrollPage();
        })
        .catch(function (err) {
          toast(err.message, true);
        });
    });
  }
  var payLeaveForm = el("pay-leave-form");
  if (payLeaveForm) {
    payLeaveForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var eid = el("pay-eid") && el("pay-eid").value;
      if (!eid) return;
      api("/payroll/leave", {
        method: "POST",
        body: JSON.stringify({
          employee_id: eid,
          work_date: el("pay-leave-date") && el("pay-leave-date").value,
          kind: el("pay-leave-kind") && el("pay-leave-kind").value,
        }),
      })
        .then(function () {
          toast("Leave recorded.");
          loadPayrollPage();
        })
        .catch(function (err) {
          toast(err.message, true);
        });
    });
  }

  loadOverview();
})();
