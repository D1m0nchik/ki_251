async function init() {
  try {
    currentUser = await apiRequest("/api/user_data");
    const isStarosta = currentUser.role === "starosta";

    document.getElementById("user-role-badge").innerText = isStarosta ? "Староста" : "Студент";
    document.getElementById("subgroup-indicator").innerText = `${currentUser.subgroup} підгрупа ⚙️`;

    if (isStarosta) {
      document.getElementById("starosta-date-filter").style.display = "block";
      document.getElementById("starosta-broadcast-panel").style.display = "block";
      document.getElementById("nav-group-btn").style.display = "block";
    }

    if (!currentUser.full_name || currentUser.full_name.split(" ").length < 2) {
      openProfileModal();
    }

    await loadSchedule();
    await loadAnnouncements(); // Завантажуємо оголошення
    await loadMyStats();
    if (isStarosta) await loadGroupManagement();
  } catch (err) {
    console.error(err);
    document.getElementById("schedule-list").innerHTML = `<p style="color:red;">${err.message}</p>`;
  }
}

// Завантаження збережених оголошень із бази
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
    container.innerHTML = "<p style='color:red;'>Не вдалося завантажити новини.</p>";
  }
}

// Оновлена функція відправки: тепер одразу перевантажує стрічку новин
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

  // Миттєво показуємо нове оголошення в списку
  await loadAnnouncements();
}
