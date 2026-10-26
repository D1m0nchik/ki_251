const tg = window.Telegram.WebApp;
tg.expand();

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
  try {
    currentUser = await apiRequest("/api/user_data");
    const isStarosta = currentUser.role === "starosta";

    document.getElementById("user-role-badge").innerText = isStarosta ? "Староста" : "Студент";
    document.getElementById("subgroup-indicator").innerText = `${currentUser.subgroup} підгрупа ⚙️`;

    if (isStarosta) {
      document.getElementById("starosta-date-filter").style.display = "block";
      document.getElementById("starosta-broadcast-panel").style.display = "block";
      document.getElementById("nav-group-btn").style.display = "block"; // Кнопка «Група» в меню
    }

    // Якщо це новий студент (ім'я містить лише одне слово або нік), пропонуємо заповнити профіль
    if (!currentUser.full_name || currentUser.full_name.split(" ").length < 2) {
      openProfileModal();
    }

    await loadSchedule();
    await loadMyStats();
    if (isStarosta) await loadGroupManagement();
  } catch (err) {
    console.error(err);
    document.getElementById("schedule-list").innerHTML = `<p style="color:red;">${err.message}</p>`;
  }
}

// Налаштування профілю (ПІБ та підгрупа)
function openProfileModal() {
  if (currentUser) {
    document.getElementById("profile-fullname").value = currentUser.full_name || "";
    document.getElementById("profile-subgroup").value = currentUser.subgroup || 1;
  }
  document.getElementById("profile-modal").style.display = "block";
}

function closeProfileModal() {
  document.getElementById("profile-modal").style.display = "none";
}

async function saveProfile() {
  const full_name = document.getElementById("profile-fullname").value.trim();
  const subgroup = parseInt(document.getElementById("profile-subgroup").value);

  if (!full_name || full_name.split(" ").length < 2) {
    alert("Будь ласка, введіть повне Прізвище та Ім'я (наприклад: Шевченко Тарас)");
    return;
  }

  await apiRequest("/api/user/profile", "POST", { full_name, subgroup });
  currentUser.full_name = full_name;
  currentUser.subgroup = subgroup;
  document.getElementById("subgroup-indicator").innerText = `${subgroup} підгрупа ⚙️`;
  closeProfileModal();
  await loadSchedule();
}

// Керування підгрупами старостою
async function loadGroupManagement() {
  const students = await apiRequest("/api/group/students");
  const container = document.getElementById("group-students-list");
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
}

async function changeSubgroup(tgId, newSg) {
  await apiRequest("/api/group/change_subgroup", "POST", {
    student_tg_id: tgId,
    subgroup: parseInt(newSg)
  });
}

// Завантаження розкладу
async function loadSchedule() {
  const schedule = await apiRequest("/api/schedule");
  const container = document.getElementById("schedule-list");
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
      
      let starostaBtn = currentUser.role === "starosta" 
        ? `<button class="btn-journal" onclick="openAttendanceModal(${item.id}, '${item.subject}')">📝 Журнал пари</button>`
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

// Журнал відвідуваності
async function openAttendanceModal(scheduleId, subjectName) {
  currentActiveLessonId = scheduleId;
  attendanceState = {};
  document.getElementById("modal-lesson-title").innerText = `Журнал: ${subjectName}`;
  const studentsListContainer = document.getElementById("modal-students-list");
  studentsListContainer.innerHTML = "<p>Завантаження списку студентів...</p>";
  document.getElementById("attendance-modal").style.display = "block";

  const students = await apiRequest(`/api/attendance/students?schedule_id=${scheduleId}`);
  studentsListContainer.innerHTML = "";

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
    studentsListContainer.appendChild(row);
  });
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
  tg.showPopup({ message: "Журнал пари збережено!" });
}

function closeAttendanceModal() {
  document.getElementById("attendance-modal").style.display = "none";
}

// Статистика
async function loadMyStats() {
  const data = await apiRequest("/api/my_stats");
  const gen = data.general || {};
  const total = gen.total || 0;
  const present = gen.present_count || 0;
  const percent = total > 0 ? Math.round((present / total) * 100) : 100;

  document.getElementById("stat-percent").innerText = `${percent}%`;
  document.getElementById("stat-counts").innerText = 
    `Був: ${present} | Пропусків: ${gen.absent_count || 0} | Запізнень: ${gen.late_count || 0}`;
  document.getElementById("stat-progress-fill").style.width = `${percent}%`;

  const subContainer = document.getElementById("subject-stats-list");
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

// Оголошення
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
}

// Оновлення дат
async function submitCustomDateRange() {
  const sdate = document.getElementById("filter-sdate").value.trim();
  const edate = document.getElementById("filter-edate").value.trim();
  if (!sdate || !edate) {
    alert("Введіть обидві дати");
    return;
  }
  document.getElementById("schedule-list").innerHTML = "<p>Оновлення розкладу з сервера університету...</p>";
  await apiRequest("/api/schedule/update_range", "POST", { sdate, edate });
  await loadSchedule();
}

function switchTab(tabId, btn) {
  document.querySelectorAll(".tab-pane").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(el => el.classList.remove("active"));
  document.getElementById(tabId).classList.add("active");
  btn.classList.add("active");
}

init();
