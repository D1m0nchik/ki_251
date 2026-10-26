const tg = window.Telegram.WebApp;
tg.expand();

let currentUser = null;

async function apiRequest(endpoint) {
  const headers = {
    "X-Telegram-Init-Data": (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) ? window.Telegram.WebApp.initData : "",
    "Content-Type": "application/json"
  };
  const res = await fetch(endpoint, { headers });
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({}));
    throw new Error(errorData.detail || "Помилка запиту");
  }
  return res.json();
}

async function init() {
  try {
    currentUser = await apiRequest("/api/user_data");
    document.getElementById("user-role-badge").innerText = 
      currentUser.role === "starosta" ? "Староста" : "Студент";
    document.getElementById("subgroup-indicator").innerText = 
      `${currentUser.subgroup} підгрупа`;

    await loadSchedule();
  } catch (err) {
    document.getElementById("schedule-list").innerHTML = 
      "<p style='color:red;'>Не вдалося авторизуватися через Telegram.</p>";
  }
}

async function loadSchedule() {
  const schedule = await apiRequest("/api/schedule");
  const container = document.getElementById("schedule-list");
  container.innerHTML = "";

  if (!schedule || schedule.length === 0) {
    container.innerHTML = "<p>На цей період занять не знайдено.</p>";
    return;
  }

  // 1. Групуємо пари за днями (дата + день тижня)
  const groupedByDay = {};
  schedule.forEach(item => {
    const dayKey = `${item.date_str} ${item.day_of_week}`;
    if (!groupedByDay[dayKey]) {
      groupedByDay[dayKey] = [];
    }
    groupedByDay[dayKey].push(item);
  });

  // 2. Рендеримо дні з окремими заголовками
  for (const [dayTitle, lessons] of Object.entries(groupedByDay)) {
    const daySection = document.createElement("div");
    daySection.className = "day-section";

    // Заголовок дня тижня
    const dayHeader = document.createElement("h3");
    dayHeader.className = "day-header";
    dayHeader.innerText = dayTitle;
    daySection.appendChild(dayHeader);

    // Список пар на цей день
    lessons.forEach(item => {
      const card = document.createElement("div");
      card.className = `card lesson-card ${item.is_replacement ? 'replacement' : ''}`;
      
      let replacementHtml = item.is_replacement 
        ? `<div class="replacement-badge">🔴 ${item.replacement_note}</div>` 
        : "";

      card.innerHTML = `
        ${replacementHtml}
        <h4 style="margin:0 0 6px 0;">${item.lesson_number} пара (${item.time_start} - ${item.time_end})</h4>
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

function switchTab(tabId, btn) {
  document.querySelectorAll(".tab-pane").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(el => el.classList.remove("active"));
  
  document.getElementById(tabId).classList.add("active");
  btn.classList.add("active");
}

init();
