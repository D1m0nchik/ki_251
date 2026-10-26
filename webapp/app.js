const tg = window.Telegram.WebApp;
try {
  tg.expand();
} catch (e) {}

let currentUser = null;
let currentActiveLessonId = null;
let attendanceState = {};

async function apiRequest(endpoint, method = "GET", body = null) {
  const headers = {
    "X-Telegram-Init-Data": (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) ? window.Telegram.WebApp.initData : "",
    "Content-Type": "application/json"
  };
  const options = { method, headers };
  if (body) options.body = JSON.stringify(body);
  
  const res = await fetch(endpoint, options);
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    throw new Error(errorData.detail || "Помилка сервера");
  }
  return res.json();
}

async function init() {
  // 1. Авторизація користувача
  try {
    currentUser = await apiRequest("/api/user_data");
    const isStarosta = currentUser && currentUser.role === "starosta";

    const badge = document.getElementById("user-role-badge");
    if (badge) badge.innerText = isStarosta ? "Староста" : "Студент";

    const sgIndicator = document.getElementById("subgroup-indicator");
    if (sgIndicator) sgIndicator.innerText = `${currentUser.subgroup || 1} підгрупа ⚙️`;

    if (isStarosta) {
      const dateFilter = document.getElementById("starosta-date-filter");
      if (dateFilter) dateFilter.style.display = "block";

      const broadcastPanel = document.getElementById("starosta-broadcast-panel");
      if (broadcastPanel) broadcastPanel.style.display = "block";

      const navGroupBtn = document.getElementById("nav-group-btn");
      if (navGroupBtn) navGroupBtn.style.display = "block";
    }

    if (!currentUser.full_name || currentUser.full_name.split(" ").length < 2) {
      openProfileModal();
    }
  } catch (err) {
    console.error("User data error:", err);
    const badge = document.getElementById("user-role-badge");
    if (badge) badge.innerText = "Студент";
  }

  // 2. Головне — завантаження розкладу (виконується обов'язково)
  try {
    await loadSchedule();
  } catch (err) {
    console.error("Schedule error:", err);
    const sched = document.getElementById("schedule-list");
    if (sched) sched.innerHTML = `<p style="color:red;">Помилка завантаження розкладу: ${err.message}</p>`;
  }

  // 3. Другорядні модулі (працюють автономно)
  loadAnnouncements().catch(e => console.error(e));
  loadMyStats().catch(e => console.error(e));
  if (currentUser && currentUser.role === "starosta") {
    loadGroupManagement().catch(e => console.error(e));
  }
}

// Завантаження розкладу
async function loadSchedule() {
  const schedule = await apiRequest("/api/schedule");
  const container = document.getElementById("schedule-list");
  if (!container) return;
  container.innerHTML = "";

  if (!schedule || schedule.length === 0) {
    container.innerHTML = "<p>На цей період занять не знайдено.</p>";
    return;
  }

  const groupedByDay = {};
  schedule.forEach(item => {
    const dayKey = `${item.date_str} ${item.day_of_week}`;
    if (!groupedByDay[dayKey]) groupedByDay[dayKey] = [];
    groupedByDay[dayKey].push(item);
  });

  for (const [dayTitle, lessons] of Object.entries(groupedByDay)) {
    const daySection = document.createElement("div");
    daySection.className = "day-section";

    const dayHeader = document.createElement("h3");
    dayHeader.className = "day-header";
    dayHeader.innerText = dayTitle;
    daySection.appendChild(dayHeader);

    lessons.forEach(item => {
      const card = document.createElement("div");
      card.className = `card lesson-card ${item.is_replacement ? 'replacement' : ''}`;
      
      let replacementHtml = item.is_replacement 
        ? `<div class="replacement-badge" style="background: rgba(255, 71, 87, 0.15); border: 1px solid #ff4757; padding: 6px 10px; border-radius: 6px; margin-bottom: 8px; color: #ff6b81; font-weight: 600; font-size: 12px;">
            🔴 ${item.replacement_note || "Увага! Заміна"}
           </div>` 
        : "";
      
      let starostaBtn = (currentUser && currentUser.role === "starosta")
        ? `<button class="btn-journal" onclick="openAttendanceModal(${item.id}, '${item.subject.replace(/'/g, "\\'")}')">📝 Журнал пари</button>`
        : "";

      card.innerHTML = `
        ${replacementHtml}
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h4 style="margin:0 0 6px 0;">${item.lesson_number} пара (${item.time_start} - ${item.time_end})</h4>
          ${starostaBtn}
        </div>
        <p style="margin:0 0 4px 0;"><strong>${item.subject}</strong> (${item.lesson_type})</p>
        <p style="margin:0; font-size: 13px; color: var(--text-muted);">
          🏛 ${item.room} | 👨‍🏫 ${item.teacher}
        </p>
      `;
      daySection.appendChild(card);
    });

    container.appendChild(daySection);
  }
}

// Завантаження оголошень
async function loadAnnouncements() {
  const container = document.getElementById("announcements-list");
  if (!container) return;

  try {
    const list = await apiRequest("/api/announcements");
    container.innerHTML = "";

    if (!list || list.length === 0) {
      container.innerHTML = "<p class='text-muted'>Поки немає жодних оголошень.</p>";
      return;
    }

    list.forEach(item => {
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom: 6px;">
          <h4 style="margin:0; color: #ff6b81;">📢 ${item.title}</h4>
          <span style="font-size:11px; color:var(--text-muted);">${item.date_time || ''}</span>
        </div>
        <p style="margin:0; font-size:14px; line-height:1.4; white-space: pre-wrap;">${item.message}</p>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    container.innerHTML = "<p class='text-muted'>Поки немає жодних оголошень.</p>";
  }
}

// Персональна статистика
async function loadMyStats() {
  try {
    const data = await apiRequest("/api/my_stats");
    const gen = data.general || {};
    const total = gen.total || 0;
    const present = gen.present_count || 0;
    const percent = total > 0 ? Math.round((present / total) * 100) : 100;

    const statPercent = document.getElementById("stat-percent");
    if (statPercent) statPercent.innerText = `${percent}%`;

    const statCounts = document.getElementById("stat-counts");
    if (statCounts) {
      statCounts.innerText = `Був: ${present} | Пропусків: ${gen.absent_count || 0} | Запізнень: ${gen.late_count || 0}`;
    }

    const statFill = document.getElementById("stat-progress-fill");
    if (statFill) statFill.style.width = `${percent}%`;

    const subContainer = document.getElementById("subject-stats-list");
    if (subContainer) {
      subContainer.innerHTML = "";
      (data.subjects || []).forEach(sub => {
        const card = document.createElement("div");
        card.className = "card";
        card.innerHTML = `
          <p style="margin:0;"><strong>${sub.subject}</strong></p>
          <span style="font-size:12px; color:var(--text-muted);">Пропусків: ${sub.subject_absent} з ${sub.total_subject_lessons} пар</span>
        `;
        subContainer.appendChild(card);
      });
    }
  } catch (e) {
    console.error("Stats load error:", e);
  }
}

// Керування списком студентів для старости
async function loadGroupManagement() {
  try {
    const students = await apiRequest("/api/group/students");
    const container = document.getElementById("group-students-list");
    if (!container) return;
    container.innerHTML = "";

    students.forEach(s => {
      const card = document.createElement("div");
      card.className = "card student-row";
      card.innerHTML = `
        <div>
          <strong>${s.full_name}</strong>
          <div style="font-size:12px; color:var(--text-muted);">${s.role === 'starosta' ? '👑 Староста' : 'Студент'}</div>
        </div>
        <div>
          <select onchange="changeSubgroup(${s.telegram_id}, this.value)" class="status-select">
            <option value="1" ${s.subgroup === 1 ? 'selected' : ''}>1 підгрупа</option>
            <option value="2" ${s.subgroup === 2 ? 'selected' : ''}>2 підгрупа</option>
          </select>
        </div>
      `;
      container.appendChild(card);
    });
  } catch (e) {
    console.error("Group management error:", e);
  }
}

async function changeSubgroup(tgId, newSg) {
  await apiRequest("/api/group/change_subgroup", "POST", {
    student_tg_id: tgId,
    subgroup: parseInt(newSg)
  });
}

function openProfileModal() {
  const modal = document.getElementById("profile-modal");
  if (!modal) return;
  if (currentUser) {
    const nameInput = document.getElementById("profile-fullname");
    if (nameInput) nameInput.value = currentUser.full_name || "";
    const sgSelect = document.getElementById("profile-subgroup");
    if (sgSelect) sgSelect.value = currentUser.subgroup || 1;
  }
  modal.style.display = "block";
}

function closeProfileModal() {
  const modal = document.getElementById("profile-modal");
  if (modal) modal.style.display = "none";
}

async function saveProfile() {
  const nameInput = document.getElementById("profile-fullname");
  const full_name = nameInput ? nameInput.value.trim() : "";
  const sgSelect = document.getElementById("profile-subgroup");
  const subgroup = sgSelect ? parseInt(sgSelect.value) : 1;

  if (!full_name || full_name.split(" ").length < 2) {
    alert("Будь ласка, введіть Прізвище та Ім'я");
    return;
  }

  await apiRequest("/api/user/profile", "POST", { full_name, subgroup });
  if (currentUser) {
    currentUser.full_name = full_name;
    currentUser.subgroup = subgroup;
  }
  const ind = document.getElementById("subgroup-indicator");
  if (ind) ind.innerText = `${subgroup} підгрупа ⚙️`;
  closeProfileModal();
  await loadSchedule();
}

async function openAttendanceModal(scheduleId, subjectName) {
  currentActiveLessonId = scheduleId;
  attendanceState = {};
  const title = document.getElementById("modal-lesson-title");
  if (title) title.innerText = `Журнал: ${subjectName}`;
  const list = document.getElementById("modal-students-list");
  if (list) list.innerHTML = "<p>Завантаження студентів...</p>";
  const modal = document.getElementById("attendance-modal");
  if (modal) modal.style.display = "block";

  try {
    const students = await apiRequest(`/api/attendance/students?schedule_id=${scheduleId}`);
    if (list) list.innerHTML = "";

    students.forEach(s => {
      attendanceState[s.telegram_id] = s.status || "present";
      const row = document.createElement("div");
      row.className = "student-row";
      row.innerHTML = `
        <span>${s.full_name} (${s.subgroup} підгр.)</span>
        <select onchange="updateAttendanceStatus(${s.telegram_id}, this.value)" class="status-select">
          <option value="present" ${s.status === 'present' ? 'selected' : ''}>✅ Був</option>
          <option value="absent" ${s.status === 'absent' ? 'selected' : ''}>❌ Пропуск</option>
          <option value="late" ${s.status === 'late' ? 'selected' : ''}>⏳ Запізнився</option>
          <option value="excused" ${s.status === 'excused' ? 'selected' : ''}>📋 Поважна</option>
        </select>
      `;
      list.appendChild(row);
    });
  } catch (err) {
    if (list) list.innerHTML = `<p style="color:red;">${err.message}</p>`;
  }
}

function updateAttendanceStatus(tgId, status) {
  attendanceState[tgId] = status;
}

async function saveAttendance() {
  await apiRequest("/api/attendance/save", "POST", {
    schedule_id: currentActiveLessonId,
    records: attendanceState
  });
  closeAttendanceModal();
  if (tg && tg.showPopup) {
    tg.showPopup({ message: "Журнал пари збережено!" });
  } else {
    alert("Журнал збережено!");
  }
}

function closeAttendanceModal() {
  const modal = document.getElementById("attendance-modal");
  if (modal) modal.style.display = "none";
}

async function sendBroadcastMessage() {
  const title = document.getElementById("broadcast-title").value.trim();
  const message = document.getElementById("broadcast-body").value.trim();
  if (!title || !message) {
    alert("Будь ласка, заповніть тему та текст");
    return;
  }
  const res = await apiRequest("/api/broadcast", "POST", { title, message });
  alert(`Оголошення надіслано ${res.sent_to} студентам!`);
  document.getElementById("broadcast-title").value = "";
  document.getElementById("broadcast-body").value = "";
  await loadAnnouncements();
}

async function submitCustomDateRange() {
  const sdate = document.getElementById("filter-sdate").value.trim();
  const edate = document.getElementById("filter-edate").value.trim();
  if (!sdate || !edate) {
    alert("Введіть обидві дати");
    return;
  }
  const sched = document.getElementById("schedule-list");
  if (sched) sched.innerHTML = "<p>Оновлення розкладу з сервера університету...</p>";
  await apiRequest("/api/schedule/update_range", "POST", { sdate, edate });
  await loadSchedule();
}

function switchTab(tabId, btn) {
  document.querySelectorAll(".tab-pane").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(el => el.classList.remove("active"));
  
  const target = document.getElementById(tabId);
  if (target) target.classList.add("active");
  if (btn) btn.classList.add("active");
}

init();
