const tg = window.Telegram.WebApp;
try {
  tg.expand();
  if (tg.disableVerticalSwipes) {
    tg.disableVerticalSwipes();
  }
} catch (e) {}

let currentUser = null;
let currentActiveLessonId = null;
let attendanceState = {};
let allScheduleData = [];
let selectedSubgroupFilter = 0; // 0 - всі, 1 - 1 підгрупа, 2 - 2 підгрупа

// Пріоритетний фільтр користувача: якщо встановлено — показується поверх усього
let activeSearchRange = null; // { startNum: number, endNum: number, sStr: string, eStr: string }

// Автоматична розстановка крапок при введенні
function maskDateInput(input) {
  let v = input.value.replace(/\D/g, "");
  if (v.length > 8) v = v.substring(0, 8);
  
  let formatted = "";
  if (v.length > 4) {
    formatted = `${v.substring(0, 2)}.${v.substring(2, 4)}.${v.substring(4)}`;
  } else if (v.length > 2) {
    formatted = `${v.substring(0, 2)}.${v.substring(2)}`;
  } else {
    formatted = v;
  }
  input.value = formatted;
}

// 1. Перетворення рядка дати з бази у ціле число виду YYYYMMDD (наприклад, 20261028)
function getDateNumber(str) {
  if (!str) return 0;
  const match = str.match(/(\d{1,2})\.(\d{1,2})\.(\d{4})/);
  if (!match) return 0;
  const day = parseInt(match[1], 10);
  const month = parseInt(match[2], 10);
  const year = parseInt(match[3], 10);
  return year * 10000 + month * 100 + day;
}

// 2. Отримання поточного числа YYYYMMDD для сьогоднішнього дня
function getTodayNumber() {
  const now = new Date();
  const year = now.getFullYear();
  const month = now.getMonth() + 1;
  const day = now.getDate();
  return year * 10000 + month * 100 + day;
}

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
    const isStarosta = currentUser && currentUser.role === "starosta";

    const badge = document.getElementById("user-role-badge");
    if (badge) badge.innerText = isStarosta ? "Староста" : "Студент";

    const sgIndicator = document.getElementById("subgroup-indicator");
    if (sgIndicator) sgIndicator.innerText = `${currentUser.subgroup || 1} підгрупа ⚙️`;

    if (isStarosta) {
      const broadcastPanel = document.getElementById("starosta-broadcast-panel");
      if (broadcastPanel) broadcastPanel.style.display = "block";

      const subgroupSwitcher = document.getElementById("starosta-subgroup-switcher");
      if (subgroupSwitcher) subgroupSwitcher.style.display = "flex";

      const navGroupBtn = document.getElementById("nav-group-btn");
      if (navGroupBtn) navGroupBtn.style.display = "flex";

      const navGroupStatsBtn = document.getElementById("nav-group-stats-btn");
      if (navGroupStatsBtn) navGroupStatsBtn.style.display = "flex";

      loadGroupManagement().catch(e => console.error(e));
      loadGroupStats().catch(e => console.error(e));
    }

    if (!currentUser.full_name || currentUser.full_name.split(" ").length < 2) {
      openProfileModal();
    }
  } catch (err) {
    console.error("Init error:", err);
  }

  await refreshAllData();

  // Фонова синхронізація без скидання поточного перегляду
  setInterval(() => {
    refreshAllData(true);
  }, 20000);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshAllData(true);
    }
  });
}

async function refreshAllData(isBackground = false) {
  try {
    await loadSchedule(isBackground);
    loadAnnouncements().catch(e => console.error(e));
    loadMyStats().catch(e => console.error(e));
    if (currentUser && currentUser.role === "starosta") {
      loadGroupStats().catch(e => console.error(e));
    }
  } catch (e) {
    console.error("Помилка фонового оновлення:", e);
  }
}

async function loadSchedule(isBackground = false) {
  const container = document.getElementById("schedule-list");
  if (!container) return;

  if (!isBackground && allScheduleData.length === 0) {
    container.innerHTML = "<p class='loading'>Завантаження розкладу...</p>";
  }

  allScheduleData = await apiRequest("/api/schedule");
  
  if (!allScheduleData || allScheduleData.length === 0) {
    await syncFullMonth();
    return;
  }

  // ПРІОРИТЕТ 1: Якщо користувач шукав діапазон — рендеримо тільки його!
  if (activeSearchRange) {
    renderFilteredSchedule(activeSearchRange.startNum, activeSearchRange.endNum, activeSearchRange.sStr, activeSearchRange.eStr);
  } else {
    // ПРІОРИТЕТ 2: Основний режим за замовчуванням — починаючи з поточного дня
    showDefaultScheduleFromToday();
  }
}

// Основний режим: чітко з поточної календарної доби (сьогодні)
function showDefaultScheduleFromToday() {
  activeSearchRange = null;

  const sInput = document.getElementById("filter-sdate");
  const eInput = document.getElementById("filter-edate");
  if (sInput) sInput.value = "";
  if (eInput) eInput.value = "";

  const title = document.getElementById("schedule-view-title");
  if (title) title.innerText = "Розклад: від сьогодні на найближчі дні";

  const todayNum = getTodayNumber();

  // Вираховуємо кінець поточного тижня (неділю) у числовому форматі YYYYMMDD
  const now = new Date();
  const currentDay = now.getDay() === 0 ? 7 : now.getDay();
  const daysUntilSunday = 7 - currentDay;
  const sundayDate = new Date(now.getFullYear(), now.getMonth(), now.getDate() + daysUntilSunday);
  const sundayNum = sundayDate.getFullYear() * 10000 + (sundayDate.getMonth() + 1) * 100 + sundayDate.getDate();

  // Фільтруємо: СУВОРО пари, дата яких >= сьогодні (27 число відсікається математично: 20261027 < 20261028)
  let upcomingLessons = allScheduleData.filter(item => {
    const itemNum = getDateNumber(item.date_str);
    return itemNum >= todayNum && itemNum <= sundayNum;
  });

  // Якщо на залишок тижня занять більше немає, беремо найближчі 7 днів починаючи від сьогодні
  if (upcomingLessons.length === 0) {
    const nextWeekDate = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 7);
    const nextWeekNum = nextWeekDate.getFullYear() * 10000 + (nextWeekDate.getMonth() + 1) * 100 + nextWeekDate.getDate();

    upcomingLessons = allScheduleData.filter(item => {
      const itemNum = getDateNumber(item.date_str);
      return itemNum >= todayNum && itemNum <= nextWeekNum;
    });
  }

  // Якщо й далі порожньо — показуємо будь-які майбутні пари (але жодної минулої!)
  if (upcomingLessons.length === 0) {
    upcomingLessons = allScheduleData.filter(item => {
      return getDateNumber(item.date_str) >= todayNum;
    });
  }

  renderSchedule(upcomingLessons);
}

// Запит користувача: стає пріоритетним поверх основного розкладу
async function applyLocalDateFilter() {
  const sdateStr = document.getElementById("filter-sdate").value.trim();
  const edateStr = document.getElementById("filter-edate").value.trim();

  if (!sdateStr && !edateStr) {
    showDefaultScheduleFromToday();
    return;
  }

  const startNum = getDateNumber(sdateStr);
  const endNum = getDateNumber(edateStr);

  if (!startNum || !endNum) {
    alert("Введіть повні дати формату дд.мм.рррр");
    return;
  }

  // Встановлюємо активний пріоритет пошуку
  activeSearchRange = {
    startNum: startNum,
    endNum: endNum,
    sStr: sdateStr,
    eStr: edateStr
  };

  const container = document.getElementById("schedule-list");
  if (container) {
    container.innerHTML = "<p class='loading'>Отримання розкладу за обраний період...</p>";
  }

  try {
    // Звернення до сервера для підвантаження діапазону в БД
    await apiRequest("/api/schedule/fetch_range", "POST", {
      sdate: sdateStr,
      edate: edateStr
    });

    allScheduleData = await apiRequest("/api/schedule");
    renderFilteredSchedule(startNum, endNum, sdateStr, edateStr);
  } catch (err) {
    alert("Помилка завантаження: " + err.message);
    showDefaultScheduleFromToday();
  }
}

// Відображення пріоритетного результату пошуку через числове порівняння
function renderFilteredSchedule(startNum, endNum, sdateStr = "", edateStr = "") {
  const container = document.getElementById("schedule-list");
  if (!container) return;

  const filtered = allScheduleData.filter(item => {
    const itemNum = getDateNumber(item.date_str);
    if (!itemNum) return false;
    return itemNum >= startNum && itemNum <= endNum;
  });

  const title = document.getElementById("schedule-view-title");
  if (title) {
    title.innerText = `Розклад: ${sdateStr || '...'} — ${edateStr || '...'}`;
  }

  if (filtered.length === 0) {
    container.innerHTML = `<p class='text-muted'>За період з ${sdateStr || '...'} по ${edateStr || '...'} занять не знайдено.</p>`;
    return;
  }

  renderSchedule(filtered);
}

// Перемикач підгруп (не збиває активний пошук)
function switchStarostaViewSubgroup(subgroup, btn) {
  selectedSubgroupFilter = subgroup;
  document.querySelectorAll(".sg-btn").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  
  if (activeSearchRange) {
    renderFilteredSchedule(activeSearchRange.startNum, activeSearchRange.endNum, activeSearchRange.sStr, activeSearchRange.eStr);
  } else {
    showDefaultScheduleFromToday();
  }
}

async function syncFullMonth() {
  const container = document.getElementById("schedule-list");
  if (container) container.innerHTML = "<p class='loading'>🔄 Кешування розкладу на місяць вперед...</p>";

  try {
    await apiRequest("/api/schedule/sync_month", "POST");
    allScheduleData = await apiRequest("/api/schedule");
    
    if (activeSearchRange) {
      renderFilteredSchedule(activeSearchRange.startNum, activeSearchRange.endNum, activeSearchRange.sStr, activeSearchRange.eStr);
    } else {
      showDefaultScheduleFromToday();
    }

    if (tg && tg.showPopup) {
      tg.showPopup({ message: "Розклад успішно закешовано на місяць наперед!" });
    }
  } catch (err) {
    alert("Помилка завантаження: " + err.message);
  }
}

function renderSchedule(lessonsList) {
  const container = document.getElementById("schedule-list");
  if (!container) return;
  container.innerHTML = "";

  let visibleLessons = lessonsList;
  if (currentUser && currentUser.role === "starosta" && selectedSubgroupFilter !== 0) {
    visibleLessons = lessonsList.filter(l => l.target_subgroup === 0 || l.target_subgroup === selectedSubgroupFilter);
  }

  if (!visibleLessons || visibleLessons.length === 0) {
    container.innerHTML = "<p class='text-muted'>Занять на обрані дні не знайдено.</p>";
    return;
  }

  const groupedByDay = {};
  visibleLessons.forEach(item => {
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
        ? `<button class="btn-journal" onclick="openAttendanceModal(${item.id}, '${item.subject.replace(/'/g, "\\'")}')">📝 Журнал</button>`
        : "";

      let sgTag = "";
      if (currentUser && currentUser.role === "starosta") {
        if (item.target_subgroup === 1) sgTag = `<span style="font-size: 11px; background: rgba(108, 92, 231, 0.2); color: #a29bfe; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">1 підгр.</span>`;
        else if (item.target_subgroup === 2) sgTag = `<span style="font-size: 11px; background: rgba(243, 156, 18, 0.2); color: #f39c12; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">2 підгр.</span>`;
      }

      card.innerHTML = `
        ${replacementHtml}
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h4 style="margin:0 0 6px 0;">${item.lesson_number} пара (${item.time_start} - ${item.time_end}) ${sgTag}</h4>
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

// Статистика студента
async function loadMyStats() {
  try {
    const data = await apiRequest("/api/my_stats");
    const gen = data.general || {};
    const total = gen.total || 0;
    const present = gen.present_count || 0;
    const absent = gen.absent_count || 0;
    const late = gen.late_count || 0;
    const excused = gen.excused_count || 0;

    const percent = total > 0 ? Math.round((present / total) * 100) : 100;

    document.getElementById("stat-percent").innerText = `${percent}%`;
    document.getElementById("stat-pres").innerText = present;
    document.getElementById("stat-abs").innerText = absent;
    document.getElementById("stat-late").innerText = late;
    document.getElementById("stat-exc").innerText = excused;

    const ring = document.getElementById("stat-multicolor-ring");
    if (ring) {
      if (total === 0) {
        ring.style.background = "#2a2a38";
      } else {
        const presDeg = (present / total) * 360;
        const absDeg = presDeg + (absent / total) * 360;
        const lateDeg = absDeg + (late / total) * 360;

        ring.style.background = `conic-gradient(
          #2ecc71 0deg ${presDeg}deg,
          #ff4757 ${presDeg}deg ${absDeg}deg,
          #ffa502 ${absDeg}deg ${lateDeg}deg,
          #1e90ff ${lateDeg}deg 360deg
        )`;
      }
    }

    const subContainer = document.getElementById("subject-stats-list");
    if (!subContainer) return;
    subContainer.innerHTML = "";

    if (!data.subjects || data.subjects.length === 0) {
      subContainer.innerHTML = "<p class='text-muted'>Історія пар за вашою участю поки порожня.</p>";
      return;
    }

    data.subjects.forEach(sub => {
      const subTotal = sub.total_subject_lessons || 1;
      const subAbsent = sub.subject_absent || 0;
      const subPresent = subTotal - subAbsent;
      const subPercent = Math.round((subPresent / subTotal) * 100);
      const barColor = subPercent >= 75 ? '#2ecc71' : (subPercent >= 50 ? '#ffa502' : '#ff4757');

      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
          <strong>${sub.subject}</strong>
          <span style="font-weight:bold; color:${barColor};">${subPercent}%</span>
        </div>
        <div class="progress-bar">
          <div class="fill" style="width: ${subPercent}%; background: ${barColor};"></div>
        </div>
        <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--text-muted); margin-top:6px;">
          <span>Відвідано: ${subPresent} з ${subTotal} пар</span>
          <span>Пропусків (Н): ${subAbsent}</span>
        </div>
      `;
      subContainer.appendChild(card);
    });
  } catch (e) {
    console.error("Помилка статистики:", e);
  }
}

// Журнал відвідуваності
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
  if (currentUser && currentUser.role === "starosta") {
    loadGroupStats().catch(e => console.error(e));
  }
}

function closeAttendanceModal() {
  const modal = document.getElementById("attendance-modal");
  if (modal) modal.style.display = "none";
}

// Звіт групи для старости
async function loadGroupStats() {
  const container = document.getElementById("group-stats-table-container");
  if (!container) return;

  try {
    const data = await apiRequest("/api/group/stats");
    container.innerHTML = "";

    if (!data || data.length === 0) {
      container.innerHTML = "<p class='text-muted'>Дані ще не накопичено.</p>";
      return;
    }

    data.forEach(st => {
      const card = document.createElement("div");
      card.className = "card group-student-stat-card";
      const badgeColor = st.percent >= 80 ? '#2ecc71' : (st.percent >= 50 ? '#ffa502' : '#ff4757');

      card.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <div>
            <strong>${st.full_name}</strong>
            <div style="font-size:12px; color:var(--text-muted);">${st.subgroup} підгрупа</div>
          </div>
          <div style="font-weight:bold; font-size:15px; color:${badgeColor}; background:rgba(255,255,255,0.05); padding:4px 8px; border-radius:6px;">
            ${st.percent}%
          </div>
        </div>
        <div style="font-size:12px; margin-top:8px; display:flex; gap:10px; color:var(--text-muted);">
          <span>✅ Був: <b>${st.present_count}</b></span>
          <span>❌ Н: <b>${st.absent_count}</b></span>
          <span>⏳ Запізн.: <b>${st.late_count}</b></span>
        </div>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    container.innerHTML = "<p style='color:red;'>Не вдалося завантажити статистику групи.</p>";
  }
}

async function downloadAttendanceReport() {
  const initData = (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) ? window.Telegram.WebApp.initData : "";
  try {
    const res = await fetch("/api/attendance/export", {
      headers: { "X-Telegram-Init-Data": initData }
    });
    if (!res.ok) throw new Error("Помилка формування файлу");

    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `Журнал_КІ-25-1_${new Date().toLocaleDateString('uk-UA')}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  } catch (e) {
    alert("Не вдалося завантажити файл: " + e.message);
  }
}

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

async function sendBroadcastMessage() {
  const title = document.getElementById("broadcast-title").value.trim();
  const message = document.getElementById("broadcast-body").value.trim();
  if (!title || !message) {
    alert("Будь ласка, заповніть тему та текст");
    return;
  }

  const res = await apiRequest("/api/broadcast", "POST", { title, message });
  alert(`Оголошення надіслано ${res.sent_to} студентам та збережено!`);
  
  document.getElementById("broadcast-title").value = "";
  document.getElementById("broadcast-body").value = "";
  await loadAnnouncements();
}

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
    console.error("Помилка завантаження студентів:", e);
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
    alert("Будь ласка, введіть повне Прізвище та Ім'я");
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

function switchTab(tabId, btn) {
  document.querySelectorAll(".tab-pane").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(el => el.classList.remove("active"));
  
  const target = document.getElementById(tabId);
  if (target) target.classList.add("active");
  if (btn) btn.classList.add("active");
}

init();
