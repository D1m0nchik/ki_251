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

let activeSearchRange = null;
let showOriginalSchedule = false;

// Стан інтерактивного календаря
let calCurrentYear = 2026;
let calCurrentMonth = 10; // 1-12
let calMarkedDays = [];
let activeMatrixDateStr = null;

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

function formatMessageWithLinks(text) {
  if (!text) return "";
  const urlPattern = /(\b(https?:\/\/|www\.|t\.me\/)[-A-Z0-9+&@#\/%?=~_|!:,.;]*[-A-Z0-9+&@#\/%=~_|])/gi;
  return text.replace(urlPattern, (url) => {
    let fullUrl = url;
    if (!url.startsWith("http://") && !url.startsWith("https://")) {
      fullUrl = "https://" + url;
    }
    return `<a href="${fullUrl}" target="_blank" rel="noopener noreferrer" style="color: #4bcffa; text-decoration: underline; word-break: break-all;">${url}</a>`;
  });
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

      const deadlineBtn = document.getElementById("starosta-create-deadline-btn");
      if (deadlineBtn) deadlineBtn.style.display = "block";

      const overrideBtn = document.getElementById("starosta-override-btn");
      if (overrideBtn) overrideBtn.style.display = "block";

      const now = new Date();
      calCurrentYear = now.getFullYear();
      calCurrentMonth = now.getMonth() + 1;

      loadGroupManagement().catch(e => console.error("loadGroupManagement error:", e));
      loadGroupStats().catch(e => console.error("loadGroupStats error:", e));
      loadCalendarMonth(calCurrentYear, calCurrentMonth).catch(e => console.error("loadCalendarMonth error:", e));
    }

    if (!currentUser.full_name || currentUser.full_name.split(" ").length < 2) {
      openProfileModal();
    }
  } catch (err) {
    console.error("Init error:", err);
  }

  await refreshAllData();

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
    loadActiveReminders().catch(e => console.error(e));
    loadAnnouncements().catch(e => console.error(e));
    loadMyStats().catch(e => console.error(e));
    if (currentUser && currentUser.role === "starosta") {
      loadGroupStats().catch(e => console.error(e));
    }
  } catch (e) {
    console.error("Помилка фонового оновлення:", e);
  }
}

function onOriginalToggleChange() {
  const toggle = document.getElementById("show-original-toggle");
  showOriginalSchedule = toggle ? toggle.checked : false;
  loadSchedule();
}

async function loadSchedule(isBackground = false) {
  const container = document.getElementById("schedule-list");
  if (!container) return;

  if (!isBackground && allScheduleData.length === 0) {
    container.innerHTML = "<p class='loading'>Завантаження розкладу...</p>";
  }

  let url = `/api/schedule?original=${showOriginalSchedule}`;
  if (activeSearchRange && activeSearchRange.sStr && activeSearchRange.eStr) {
    url = `/api/schedule?sdate=${encodeURIComponent(activeSearchRange.sStr)}&edate=${encodeURIComponent(activeSearchRange.eStr)}&original=${showOriginalSchedule}`;
  }

  try {
    allScheduleData = await apiRequest(url);

    const title = document.getElementById("schedule-view-title");
    if (title) {
      const baseTitle = activeSearchRange ? `Розклад: ${activeSearchRange.sStr} — ${activeSearchRange.eStr}` : "Розклад: на 7 днів вперед";
      title.innerHTML = showOriginalSchedule ? `${baseTitle} <span style="font-size: 12px; color: #8e8ea0; font-weight: 500;">[Оригінал сайту]</span>` : baseTitle;
    }

    if (!allScheduleData || allScheduleData.length === 0) {
      if (!activeSearchRange) {
        await syncFullMonth();
        return;
      }
    }

    renderSchedule(allScheduleData);
  } catch (err) {
    container.innerHTML = `<p style="color: #ff4757; text-align: center;">Не вдалося завантажити розклад: ${err.message}</p>`;
  }
}

function showDefaultScheduleFromToday() {
  activeSearchRange = null;

  const sInput = document.getElementById("filter-sdate");
  const eInput = document.getElementById("filter-edate");
  if (sInput) sInput.value = "";
  if (eInput) eInput.value = "";

  const title = document.getElementById("schedule-view-title");
  if (title) title.innerText = "Розклад: на 7 днів вперед";

  loadSchedule();
}

async function applyLocalDateFilter() {
  const sdateStr = document.getElementById("filter-sdate").value.trim();
  const edateStr = document.getElementById("filter-edate").value.trim();

  if (!sdateStr && !edateStr) {
    showDefaultScheduleFromToday();
    return;
  }

  if (sdateStr.length < 10 || edateStr.length < 10) {
    alert("Будь ласка, введіть повні дати формату дд.мм.рррр");
    return;
  }

  activeSearchRange = { sStr: sdateStr, eStr: edateStr };

  const container = document.getElementById("schedule-list");
  if (container) {
    container.innerHTML = "<p class='loading'>Отримання розкладу за обраний період...</p>";
  }

  try {
    allScheduleData = await apiRequest("/api/schedule/fetch_range", "POST", { 
      sdate: sdateStr, 
      edate: edateStr 
    });

    const title = document.getElementById("schedule-view-title");
    if (title) {
      const baseTitle = `Розклад: ${sdateStr} — ${edateStr}`;
      title.innerHTML = showOriginalSchedule ? `${baseTitle} <span style="font-size: 12px; color: #8e8ea0; font-weight: 500;">[Оригінал сайту]</span>` : baseTitle;
    }

    renderSchedule(allScheduleData);
  } catch (err) {
    alert("Помилка завантаження: " + err.message);
    showDefaultScheduleFromToday();
  }
}

function switchStarostaViewSubgroup(subgroup, btn) {
  selectedSubgroupFilter = subgroup;
  document.querySelectorAll(".sg-btn").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
  renderSchedule(allScheduleData);
}

async function syncFullMonth() {
  const container = document.getElementById("schedule-list");
  if (container) container.innerHTML = "<p class='loading'>🔄 Оновлення розкладу з сайту університету...</p>";

  try {
    await apiRequest("/api/schedule/sync_month", "POST");
    await loadSchedule();
    if (tg && tg.showPopup) {
      tg.showPopup({ message: "Розклад успішно оновлено!" });
    }
  } catch (err) {
    if (container) {
      container.innerHTML = `<p style='color: #ff4757; text-align: center; padding: 20px;'>
        Не вдалося оновити з сайту: ${err.message}<br>
        <button class="btn-primary" onclick="loadSchedule()" style="margin-top: 10px;">Показати збережений розклад</button>
      </p>`;
    }
    alert("Помилка синхронізації: " + err.message);
  }
}

function renderSchedule(lessonsList) {
  const container = document.getElementById("schedule-list");
  if (!container) return;
  container.innerHTML = "";

  let visibleLessons = lessonsList || [];
  if (currentUser && currentUser.role === "starosta" && selectedSubgroupFilter !== 0) {
    visibleLessons = visibleLessons.filter(l => l.target_subgroup === 0 || l.target_subgroup === selectedSubgroupFilter);
  }

  if (visibleLessons.length === 0) {
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
      const isCustom = item.is_custom_override;
      const customClass = isCustom ? "custom-override" : "";

      let customBadge = isCustom 
        ? `<div class="override-badge">${item.title_note || "Об'єднана пара"}</div>` 
        : "";

      let deleteOverrideBtn = (isCustom && currentUser && currentUser.role === "starosta")
        ? `<button onclick="deleteOverrideItem(${item.id})" style="background:none; border:none; color:#8e8ea0; font-size:14px; cursor:pointer; padding:0 4px;" title="Скасувати заміну">✕</button>`
        : "";

      let replacementHtml = item.is_replacement 
        ? `<div class="replacement-badge" style="background: rgba(255, 71, 87, 0.15); border: 1px solid #ff4757; padding: 6px 10px; border-radius: 6px; margin-bottom: 8px; color: #ff6b81; font-weight: 600; font-size: 12px;">
            🔴 ${item.replacement_note || "Увага! Заміна"}
           </div>` 
        : "";
      
      let starostaBtn = (currentUser && currentUser.role === "starosta")
        ? `<button class="btn-journal" onclick="openAttendanceModal(${item.id}, '${item.subject.replace(/'/g, "\\'")}')">📝 Журнал</button>`
        : "";

      let sgTag = "";
      if (item.target_subgroup === 0) {
        sgTag = `<span style="font-size: 11px; background: rgba(46, 204, 113, 0.15); color: #2ecc71; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">Обидві підгр.</span>`;
      } else if (item.target_subgroup === 1) {
        sgTag = `<span style="font-size: 11px; background: rgba(108, 92, 231, 0.2); color: #a29bfe; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">1 підгр.</span>`;
      } else if (item.target_subgroup === 2) {
        sgTag = `<span style="font-size: 11px; background: rgba(243, 156, 18, 0.2); color: #f39c12; padding: 2px 6px; border-radius: 4px; margin-left: 6px;">2 підгр.</span>`;
      }

      card.className = `card lesson-card ${customClass} ${item.is_replacement ? 'replacement' : ''}`;
      card.innerHTML = `
        ${customBadge}
        ${replacementHtml}
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <h4 style="margin:0 0 6px 0;">${item.lesson_number} пара (${item.time_start} - ${item.time_end}) ${sgTag}</h4>
          <div style="display:flex; gap:6px; align-items:center;">
            ${deleteOverrideBtn}
            ${starostaBtn}
          </div>
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

// =========================================================
// ЛОГІКА КАЛЕНДАРНОГО ЖУРНАЛУ ТА ШАХМАТКИ
// =========================================================

function switchJournalSubTab(subTab) {
  const calView = document.getElementById("journal-view-calendar");
  const statsView = document.getElementById("journal-view-stats");
  const btnCal = document.getElementById("subtab-btn-cal");
  const btnStats = document.getElementById("subtab-btn-stats");

  if (!calView || !statsView) return;

  if (subTab === "cal") {
    calView.style.display = "block";
    statsView.style.display = "none";
    if (btnCal) btnCal.classList.add("active");
    if (btnStats) btnStats.classList.remove("active");
  } else {
    calView.style.display = "none";
    statsView.style.display = "block";
    if (btnCal) btnCal.classList.remove("active");
    if (btnStats) btnStats.classList.add("active");
    loadGroupStats().catch(e => console.error(e));
  }
}

const MONTH_NAMES_UA = [
  "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
  "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"
];

async function loadCalendarMonth(year, month) {
  calCurrentYear = year;
  calCurrentMonth = month;

  const title = document.getElementById("cal-month-title");
  if (title) title.innerText = `${MONTH_NAMES_UA[month - 1]} ${year}`;

  try {
    const meta = await apiRequest(`/api/attendance/calendar_meta?year=${year}&month=${month}`);
    calMarkedDays = meta.marked_days || [];
  } catch (err) {
    calMarkedDays = [];
  }

  renderCalendarGrid();
}

function changeCalendarMonth(delta) {
  calCurrentMonth += delta;
  if (calCurrentMonth > 12) {
    calCurrentMonth = 1;
    calCurrentYear += 1;
  } else if (calCurrentMonth < 1) {
    calCurrentMonth = 12;
    calCurrentYear -= 1;
  }
  loadCalendarMonth(calCurrentYear, calCurrentMonth);
}

function renderCalendarGrid() {
  const grid = document.getElementById("calendar-days-grid");
  if (!grid) return;
  grid.innerHTML = "";

  const firstDay = new Date(calCurrentYear, calCurrentMonth - 1, 1);
  const totalDays = new Date(calCurrentYear, calCurrentMonth, 0).getDate();

  let startOffset = firstDay.getDay() - 1;
  if (startOffset === -1) startOffset = 6;

  for (let i = 0; i < startOffset; i++) {
    const emptyCell = document.createElement("div");
    emptyCell.className = "cal-day-cell empty";
    grid.appendChild(emptyCell);
  }

  const today = new Date();
  const isCurrentMonthNow = (today.getFullYear() === calCurrentYear && (today.getMonth() + 1) === calCurrentMonth);

  for (let d = 1; d <= totalDays; d++) {
    const cell = document.createElement("div");
    cell.className = "cal-day-cell active-day";
    if (isCurrentMonthNow && today.getDate() === d) {
      cell.classList.add("today");
    }

    const hasDot = calMarkedDays.includes(d);
    const dotHtml = hasDot ? `<div class="cal-dot"></div>` : "";

    cell.innerHTML = `<span>${d}</span>${dotHtml}`;
    cell.onclick = () => openDayMatrix(d, calCurrentMonth, calCurrentYear);
    grid.appendChild(cell);
  }
}

async function openDayMatrix(day, month, year) {
  const pad = n => String(n).padStart(2, '0');
  const dateStr = `${pad(day)}.${pad(month)}.${year}`;
  activeMatrixDateStr = dateStr;

  const calBox = document.getElementById("cal-picker-box");
  const matrixBox = document.getElementById("day-matrix-box");
  if (calBox) calBox.style.display = "none";
  if (matrixBox) matrixBox.style.display = "block";

  const dObj = new Date(year, month - 1, day);
  const DAYS_UA = ["Неділя", "Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"];

  const dayTitleElem = document.getElementById("matrix-day-title");
  if (dayTitleElem) dayTitleElem.innerText = `${day} ${MONTH_NAMES_UA[month - 1].toLowerCase()}`;

  const dayDowElem = document.getElementById("matrix-day-dow");
  if (dayDowElem) dayDowElem.innerText = DAYS_UA[dObj.getDay()];

  const container = document.getElementById("day-matrix-table-container");
  if (container) container.innerHTML = "<p class='loading'>Отримання даних на цей день...</p>";

  try {
    const data = await apiRequest(`/api/attendance/day_matrix?date_str=${dateStr}`);
    renderDayMatrixTable(data);
  } catch (err) {
    if (container) container.innerHTML = `<p style="color:#ff4757; padding: 10px;">${err.message}</p>`;
  }
}

function backToCalendarPicker() {
  const matrixBox = document.getElementById("day-matrix-box");
  const calBox = document.getElementById("cal-picker-box");
  if (matrixBox) matrixBox.style.display = "none";
  if (calBox) calBox.style.display = "block";
  loadCalendarMonth(calCurrentYear, calCurrentMonth);
}

function renderDayMatrixTable(data) {
  const container = document.getElementById("day-matrix-table-container");
  if (!container) return;

  const students = data.students || [];
  const lessons = data.lessons || [];
  const matrix = data.matrix || {};

  if (lessons.length === 0) {
    container.innerHTML = "<p style='color: #8e8ea0; padding: 20px; text-align: center;'>На цей день занять у розкладі немає.</p>";
    return;
  }

  let html = `<table class="matrix-table"><thead><tr><th class="st-col">Студент</th>`;

  lessons.forEach(l => {
    const shortSubj = l.subject.length > 15 ? l.subject.substring(0, 13) + '..' : l.subject;
    html += `
      <th>
        <div style="font-size: 11px; color:#a29bfe;">${l.lesson_number} пара</div>
        <div style="font-size: 12px; margin: 2px 0;">${shortSubj}</div>
        <div style="font-size: 10px; color:#8e8ea0;">${l.time_start}</div>
      </th>
    `;
  });

  html += `</tr></thead><tbody>`;

  const statusIcons = {
    "present": "✓",
    "absent": "Н",
    "late": "О",
    "excused": "П"
  };

  const statusClasses = {
    "present": "cell-pres",
    "absent": "cell-abs",
    "late": "cell-late",
    "excused": "cell-exc"
  };

  students.forEach((st, idx) => {
    html += `<tr><td class="st-col">${idx + 1}. ${st.full_name}</td>`;

    lessons.forEach(l => {
      const isOtherSubgroup = (l.target_subgroup !== 0 && l.target_subgroup !== st.subgroup);
      if (isOtherSubgroup) {
        html += `<td><span style="color:#4a4b60;">—</span></td>`;
        return;
      }

      const key = `${l.id}_${st.telegram_id}`;
      const status = matrix[key] || "none";
      const icon = statusIcons[status] || "—";
      const cls = statusClasses[status] || "cell-none";

      html += `
        <td>
          <button class="matrix-cell-btn ${cls}" onclick="onMatrixCellClick(${l.id}, ${st.telegram_id}, this)">
            ${icon}
          </button>
        </td>
      `;
    });

    html += `</tr>`;
  });

  html += `</tbody></table>`;
  container.innerHTML = html;
}

async function onMatrixCellClick(scheduleId, tgId, btn) {
  try {
    const res = await apiRequest("/api/attendance/cell_toggle", "POST", {
      schedule_id: scheduleId,
      telegram_id: tgId
    });

    const statusIcons = { "present": "✓", "absent": "Н", "late": "О", "excused": "П" };
    const statusClasses = { "present": "cell-pres", "absent": "cell-abs", "late": "cell-late", "excused": "cell-exc" };

    btn.className = `matrix-cell-btn ${statusClasses[res.new_status]}`;
    btn.innerText = statusIcons[res.new_status];
  } catch (err) {
    alert("Помилка відмітки: " + err.message);
  }
}

async function downloadMatrixReport() {
  try {
    if (tg && tg.showPopup) {
      tg.showPopup({ message: "Формуємо відомість та надсилаємо у чат із ботом..." });
    }
    
    const res = await apiRequest(`/api/attendance/send_to_telegram?month=${calCurrentMonth}&year=${calCurrentYear}`, "POST");
    
    if (tg && tg.showPopup) {
      tg.showPopup({ message: `✅ Відомість надіслано в чат із ботом!\nФайл: ${res.filename}` });
    } else {
      alert("Файл надіслано в чат бота!");
    }
  } catch (err) {
    alert("Помилка формування відомості: " + err.message);
  }
}

// =========================================================

async function openOverrideModal() {
  const modal = document.getElementById("override-modal");
  const listContainer = document.getElementById("override-source-lessons-list");
  const teacherSelect = document.getElementById("ov-teacher");
  const subjectSelect = document.getElementById("ov-subject");
  if (!modal || !listContainer) return;

  listContainer.innerHTML = "";
  
  allScheduleData.filter(l => !l.is_custom_override).forEach(l => {
    const item = document.createElement("label");
    const roomInfo = l.room ? ` | 🏛 ${l.room}` : "";
    const sgInfo = l.target_subgroup === 0 ? "Всі" : `${l.target_subgroup} підгр.`;

    item.innerHTML = `
      <input type="checkbox" name="replace_lesson" value="${l.id}">
      <span><b>${l.date_str}</b> (${l.lesson_number} пара): ${l.subject} [${sgInfo}]${roomInfo}</span>
    `;
    listContainer.appendChild(item);
  });

  try {
    const meta = await apiRequest("/api/schedule/meta");
    
    if (subjectSelect && meta.subjects) {
      subjectSelect.innerHTML = '<option value="">-- Оберіть дисципліну зі списку --</option>';
      meta.subjects.forEach(s => {
        const opt = document.createElement("option");
        opt.value = s;
        opt.innerText = s;
        subjectSelect.appendChild(opt);
      });
    }

    if (teacherSelect && meta.teachers) {
      teacherSelect.innerHTML = '<option value="">-- Оберіть викладача --</option>';
      meta.teachers.forEach(t => {
        const opt = document.createElement("option");
        opt.value = t;
        opt.innerText = t;
        teacherSelect.appendChild(opt);
      });
    }
  } catch (err) {
    console.error("Не вдалося завантажити довідник пар:", err);
  }

  const d = new Date();
  const pad = n => String(n).padStart(2, '0');
  const dInput = document.getElementById("ov-date");
  if (dInput) dInput.value = `${pad(d.getDate())}.${pad(d.getMonth()+1)}.${d.getFullYear()}`;

  modal.style.display = "flex";
  document.body.style.overflow = "hidden";
}

function closeOverrideModal() {
  const modal = document.getElementById("override-modal");
  if (modal) {
    modal.style.display = "none";
    document.body.style.overflow = "";
  }
}

async function saveOverride() {
  const checkedBoxes = document.querySelectorAll("input[name='replace_lesson']:checked");
  const replaces_ids = Array.from(checkedBoxes).map(cb => parseInt(cb.value));

  const target_date = document.getElementById("ov-date").value.trim();
  const lesson_number = parseInt(document.getElementById("ov-lesson-num").value);
  const subjectSelect = document.getElementById("ov-subject");
  const subject = subjectSelect ? subjectSelect.value.trim() : "";
  const title_note = document.getElementById("ov-note").value.trim();
  let rawRoom = document.getElementById("ov-room").value.trim();
  const teacher = document.getElementById("ov-teacher").value.trim();
  const target_subgroup = parseInt(document.getElementById("ov-subgroup").value);

  if (target_date.length < 10 || !subject) {
    alert("Будь ласка, вкажіть коректну дату (дд.мм.рррр) та оберіть назву дисципліни!");
    return;
  }

  let formattedRoom = "";
  if (rawRoom) {
    rawRoom = rawRoom.replace(/^(ауд\.?|ауд)\s*/i, "").replace(/\.?ауд\.?$/i, "").trim();
    formattedRoom = `${rawRoom}.ауд.`;
  }

  try {
    await apiRequest("/api/schedule/override/create", "POST", {
      title_note,
      replaces_ids,
      target_date,
      lesson_number,
      subject,
      lesson_type: "Лаб",
      teacher,
      room: formattedRoom,
      target_subgroup
    });

    closeOverrideModal();
    await loadSchedule();
    if (tg && tg.showPopup) {
      tg.showPopup({ message: "Пріоритетну пару збережено!" });
    }
  } catch (e) {
    alert("Помилка: " + e.message);
  }
}

async function deleteOverrideItem(overrideId) {
  if (!confirm("Скасувати цю змінену пару та повернути розклад з деканату?")) return;
  try {
    await apiRequest(`/api/schedule/override/delete?override_id=${overrideId}`, "POST");
    await loadSchedule();
  } catch (e) {
    alert("Помилка: " + e.message);
  }
}

async function loadActiveReminders() {
  const banner = document.getElementById("active-deadline-banner");
  if (!banner) return;

  try {
    const rawReminders = await apiRequest("/api/reminders/active");
    if (!rawReminders || rawReminders.length === 0) {
      banner.style.display = "none";
      banner.innerHTML = "";
      return;
    }

    const nowTimestamp = Date.now();
    const reminders = rawReminders.filter(r => {
      if (!r.deadline_at) return true;
      const deadlineTimestamp = new Date(r.deadline_at).getTime();
      return deadlineTimestamp > nowTimestamp;
    });

    if (reminders.length === 0) {
      banner.style.display = "none";
      banner.innerHTML = "";
      return;
    }

    banner.innerHTML = "";
    banner.style.display = "flex";
    banner.style.flexDirection = "column";
    banner.style.gap = "10px";

    const isStarosta = currentUser && currentUser.role === "starosta";

    reminders.forEach(r => {
      const isDone = r.is_completed_by_me;
      const card = document.createElement("div");
      const bgStyle = isDone
        ? "background: rgba(46, 204, 113, 0.08); border: 1px solid rgba(46, 204, 113, 0.25); border-left: 4px solid #2ecc71;"
        : "background: linear-gradient(135deg, rgba(255, 71, 87, 0.12), rgba(255, 107, 129, 0.04)); border: 1px solid rgba(255, 71, 87, 0.35); border-left: 4px solid #ff4757;";
      
      card.style.cssText = `${bgStyle} border-radius: 12px; padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; box-sizing: border-box;`;

      const descHtml = r.description ? `<p style="margin: 0; font-size: 13px; color: #dcdde1; line-height: 1.4;">${formatMessageWithLinks(r.description)}</p>` : "";
      const deleteBtn = isStarosta ? `<button onclick="deleteReminderItem(${r.id})" style="background: none; border: none; color: #8e8ea0; font-size: 14px; cursor: pointer; padding: 0 4px;">✕</button>` : "";

      const completeBtnText = isDone ? "✅ Виконано (зняти)" : "🔘 Я виконав";
      const completeBtnStyle = isDone
        ? "background: rgba(46, 204, 113, 0.2); color: #2ecc71; border: 1px solid #2ecc71;"
        : "background: #202130; color: #fff; border: 1px solid #2f3045;";

      const completedCountHtml = isStarosta 
        ? `<span style="font-size: 11px; color: #8e8ea0; align-self: center;">Здали: <b>${r.completed_count || 0}</b></span>` 
        : "";

      card.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: flex-start;">
          <strong style="color: ${isDone ? '#2ecc71' : '#ff6b81'}; font-size: 15px;">
            ${isDone ? '✅' : '⏳'} ${r.title}
          </strong>
          ${deleteBtn}
        </div>
        ${descHtml}
        <div style="display: flex; justify-content: space-between; font-size: 11px; color: #8e8ea0; margin-top: 2px;">
          <span>Відкрито: ${r.start_str}</span>
          <span style="color: ${isDone ? '#2ecc71' : '#ff4757'}; font-weight: bold;">Дедлайн: ${r.deadline_str}</span>
        </div>
        <div style="display: flex; justify-content: space-between; margin-top: 6px; gap: 8px;">
          <button onclick="toggleReminderComplete(${r.id})" style="${completeBtnStyle} padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 700; cursor: pointer;">
            ${completeBtnText}
          </button>
          ${completedCountHtml}
        </div>
      `;
      banner.appendChild(card);
    });
  } catch (e) {
    console.error("Помилка завантаження нагадувань:", e);
  }
}

async function toggleReminderComplete(reminderId) {
  try {
    const res = await apiRequest(`/api/reminders/complete?reminder_id=${reminderId}`, "POST");
    await loadActiveReminders();
    if (tg && tg.showPopup) {
      const msg = res.completed 
        ? "Супер! Ви відмітили завдання як виконане. Нагадування вимкнено!" 
        : "Позначку знято. Нагадування відновлено.";
      tg.showPopup({ message: msg });
    }
  } catch (e) {
    alert("Помилка: " + e.message);
  }
}

function openReminderModal() {
  const modal = document.getElementById("reminder-modal");
  if (!modal) return;
  modal.style.display = "flex";

  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const nowStr = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
  
  const startInp = document.getElementById("rem-start");
  if (startInp) startInp.value = nowStr;
}

function closeReminderModal() {
  const modal = document.getElementById("reminder-modal");
  if (modal) modal.style.display = "none";
}

async function saveReminder() {
  const title = document.getElementById("rem-title").value.trim();
  const desc = document.getElementById("rem-desc").value.trim();
  const startAt = document.getElementById("rem-start").value;
  const deadlineAt = document.getElementById("rem-deadline").value;

  if (!title || !deadlineAt) {
    alert("Будь ласка, вкажіть назву завдання та дату дедлайну!");
    return;
  }

  try {
    await apiRequest("/api/reminders/create", "POST", {
      title,
      description: desc,
      start_at: startAt,
      deadline_at: deadlineAt
    });

    closeReminderModal();
    document.getElementById("rem-title").value = "";
    document.getElementById("rem-desc").value = "";
    document.getElementById("rem-deadline").value = "";

    await loadActiveReminders();
    if (tg && tg.showPopup) {
      tg.showPopup({ message: "Дедлайн успішно створено та надіслано в групу!" });
    } else {
      alert("Дедлайн створено!");
    }
  } catch (err) {
    alert("Помилка: " + err.message);
  }
}

async function deleteReminderItem(id) {
  if (!confirm("Видалити це нагадування?")) return;
  await apiRequest(`/api/reminders/delete?reminder_id=${id}`, "POST");
  await loadActiveReminders();
}

async function loadGroupManagement() {
  try {
    const students = await apiRequest("/api/group/students");
    const container = document.getElementById("group-students-list");
    if (!container) return;
    container.innerHTML = "";

    students.forEach(s => {
      const card = document.createElement("div");
      card.style.cssText = "background: #161722; border: 1px solid #232435; border-radius: 12px; padding: 14px 16px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center;";

      card.innerHTML = `
        <div style="display: flex; flex-direction: column; gap: 4px;">
          <strong style="font-size: 15px; font-weight: 700; color: #ffffff; letter-spacing: -0.2px;">${s.full_name}</strong>
          <span style="font-size: 12px; color: #8e8ea0;">${s.role === 'starosta' ? '👑 Староста' : 'Студент'}</span>
        </div>
        <div>
          <select onchange="changeSubgroup(${s.telegram_id}, this.value)" style="background: #202130; border: 1px solid #2f3045; color: #ffffff; padding: 8px 12px; border-radius: 8px; font-size: 13px; outline: none; cursor: pointer;">
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

async function loadGroupStats() {
  const container = document.getElementById("group-stats-table-container");
  if (!container) return;

  try {
    const data = await apiRequest("/api/group/stats");
    container.innerHTML = "";

    if (!data || data.length === 0) {
      container.innerHTML = "<p style='color: #8e8ea0; font-size: 13px;'>Дані ще не накопичено.</p>";
      return;
    }

    let totalRiskCount = 0;
    const totalStudents = data.length;

    let maxLessons = 0;
    data.forEach(st => {
      const checked = (st.present_count || 0) + (st.absent_count || 0) + (st.late_count || 0) + (st.excused_count || 0);
      if (checked > maxLessons) maxLessons = checked;
      if ((st.absent_count || 0) >= 3) {
        totalRiskCount++;
      }
    });

    if (maxLessons === 0) maxLessons = 1;

    const riskElem = document.getElementById("metric-risk-count");
    if (riskElem) riskElem.innerText = totalRiskCount;

    const stElem = document.getElementById("metric-students-count");
    if (stElem) stElem.innerText = totalStudents;

    const lesElem = document.getElementById("metric-lessons-total");
    if (lesElem) lesElem.innerText = maxLessons;

    data.sort((a, b) => (b.absent_count || 0) - (a.absent_count || 0));

    data.forEach(st => {
      const pres = (st.present_count || 0) + (st.late_count || 0);
      const abs = st.absent_count || 0;
      const exc = st.excused_count || 0;
      const totalStLessons = pres + abs + exc;
      const baseline = totalStLessons > 0 ? totalStLessons : maxLessons;

      const isRisk = abs >= 3;

      const presWidth = ((pres / baseline) * 100).toFixed(1);
      const absWidth = ((abs / baseline) * 100).toFixed(1);
      const excWidth = ((exc / baseline) * 100).toFixed(1);

      let badgesHtml = "";
      if (isRisk) {
        badgesHtml += `<span style="background: rgba(255, 71, 87, 0.18); color: #ff6b81; font-size: 11px; font-weight: 700; padding: 4px 8px; border-radius: 6px; white-space: nowrap;">на межі · ${abs} проп.</span>`;
      } else if (abs > 0) {
        badgesHtml += `<span style="background: rgba(142, 142, 160, 0.15); color: #8e8ea0; font-size: 11px; font-weight: 600; padding: 4px 8px; border-radius: 6px; white-space: nowrap;">${abs} проп.</span>`;
      }

      if (exc > 0) {
        badgesHtml += `<span style="background: rgba(52, 152, 219, 0.18); color: #3498db; font-size: 11px; font-weight: 700; padding: 4px 8px; border-radius: 6px; white-space: nowrap;">${exc} поваж.</span>`;
      }

      const card = document.createElement("div");
      const borderLeft = isRisk ? "border-left: 3px solid #ff4757;" : "border-left: 1px solid #232435;";
      card.style.cssText = `background: #161722; border: 1px solid #232435; ${borderLeft} border-radius: 14px; padding: 14px 18px; margin-bottom: 10px; display: flex; flex-direction: column; gap: 8px; box-sizing: border-box;`;

      card.innerHTML = `
        <div style="display: flex; justify-content: space-between; align-items: center;">
          <span style="font-size: 15px; font-weight: 700; color: #ffffff; letter-spacing: -0.2px;">${st.full_name}</span>
          <div style="display: flex; gap: 6px; align-items: center;">
            ${badgesHtml}
          </div>
        </div>

        <div style="width: 100%; height: 7px; background: #232435; border-radius: 4px; display: flex; overflow: hidden; margin-top: 4px;">
          <div style="width: ${presWidth}%; background: #2ecc71; height: 100%;"></div>
          <div style="width: ${absWidth}%; background: #ff4757; height: 100%;"></div>
          <div style="width: ${excWidth}%; background: #3498db; height: 100%;"></div>
        </div>

        <div style="display: flex; justify-content: flex-end; font-size: 12px; color: #8e8ea0; margin-top: 2px;">
          <span>був ${pres} із ${baseline}</span>
        </div>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    container.innerHTML = "<p style='color: #ff4757; font-size: 13px;'>Не вдалося завантажити статистику групи.</p>";
  }
}

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

    const statPercent = document.getElementById("stat-percent");
    if (statPercent) statPercent.innerText = `${percent}%`;

    const statPres = document.getElementById("stat-pres");
    if (statPres) statPres.innerText = present;

    const statAbs = document.getElementById("stat-abs");
    if (statAbs) statAbs.innerText = absent;

    const statLate = document.getElementById("stat-late");
    if (statLate) statLate.innerText = late;

    const statExc = document.getElementById("stat-exc");
    if (statExc) statExc.innerText = excused;

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

async function openAttendanceModal(scheduleId, subjectName) {
  currentActiveLessonId = scheduleId;
  attendanceState = {};
  const title = document.getElementById("modal-lesson-title");
  if (title) title.innerText = `Журнал: ${subjectName}`;
  const list = document.getElementById("modal-students-list");
  if (list) list.innerHTML = "<p class='loading'>Завантаження студентів...</p>";
  
  const modal = document.getElementById("attendance-modal");
  if (modal) {
    modal.style.display = "flex";
    document.body.style.overflow = "hidden";
  }

  try {
    const students = await apiRequest(`/api/attendance/students?schedule_id=${scheduleId}`);
    if (list) list.innerHTML = "";

    students.forEach(s => {
      const currentStatus = s.status || "absent";
      attendanceState[s.telegram_id] = currentStatus;

      const row = document.createElement("div");
      row.className = "student-row";
      row.style.cssText = "display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #232435; gap: 10px;";
      
      row.innerHTML = `
        <span style="font-size: 14px; font-weight: 600; color: #fff; flex: 1; word-break: break-word; line-height: 1.3;">
          ${s.full_name} <span style="font-size: 12px; color: var(--text-muted); white-space: nowrap;">(${s.subgroup} підгр.)</span>
        </span>
        <select onchange="updateAttendanceStatus(${s.telegram_id}, this.value)" style="width: 130px; min-width: 130px; max-width: 130px; flex-shrink: 0; background: #202130; border: 1px solid #2f3045; color: #fff; padding: 8px 10px; border-radius: 8px; font-size: 13px; outline: none; text-align: left;">
          <option value="absent" ${currentStatus === 'absent' ? 'selected' : ''}>❌ Пропуск</option>
          <option value="present" ${currentStatus === 'present' ? 'selected' : ''}>✅ Був</option>
          <option value="late" ${currentStatus === 'late' ? 'selected' : ''}>⏳ Запізнився</option>
          <option value="excused" ${currentStatus === 'excused' ? 'selected' : ''}>📋 Поважна</option>
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
  if (modal) {
    modal.style.display = "none";
    document.body.style.overflow = "";
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
    setTimeout(() => {
      a.remove();
      window.URL.revokeObjectURL(url);
    }, 500);
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
      const messageWithLinks = formatMessageWithLinks(item.message);

      card.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom: 6px;">
          <h4 style="margin:0; color: #ff6b81;">📢 ${item.title}</h4>
          <span style="font-size:11px; color:var(--text-muted);">${item.date_time || ''}</span>
        </div>
        <p style="margin:0; font-size:14px; line-height:1.4; white-space: pre-wrap;">${messageWithLinks}</p>
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

function openProfileModal() {
  const modal = document.getElementById("profile-modal");
  if (!modal) return;
  if (currentUser) {
    const nameInput = document.getElementById("profile-fullname");
    if (nameInput) nameInput.value = currentUser.full_name || "";
    const sgSelect = document.getElementById("profile-subgroup");
    if (sgSelect) sgSelect.value = currentUser.subgroup || 1;
  }
  modal.style.display = "flex";
  document.body.style.overflow = "hidden";
}

function closeProfileModal() {
  const modal = document.getElementById("profile-modal");
  if (modal) {
    modal.style.display = "none";
    document.body.style.overflow = "";
  }
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
