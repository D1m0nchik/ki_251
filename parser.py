import os
import re
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime
from database import get_db_pool

SCHEDULE_URL = "https://pk.nung.edu.ua/schedule"
GROUP_NAME = "КІ-25-1"

def extract_meet_url(cell_element) -> str:
    """Витягує посилання на онлайн-пару (Meet/Zoom/Teams) з HTML елемента комірки."""
    if not cell_element:
        return ""
    
    # 1. Шукаємо через теги <a>
    for a in cell_element.find_all("a", href=True):
        href = a["href"].strip()
        if any(domain in href for domain in ["meet.google.com", "zoom.us", "teams.microsoft.com"]):
            return href
        if href.startswith("http://") or href.startswith("https://"):
            return href
            
    # 2. Шукаємо регулярним виразом у тексті
    text = cell_element.get_text()
    match = re.search(r'https?://[^\s<>"]+', text)
    if match:
        return match.group(0)
        
    return ""

async def fetch_and_save_schedule(sdate: str, edate: str):
    """
    Завантажує та розбирає розклад з сайту ІФНТУНГ за діапазоном дат sdate та edate.
    Зберігає аудиторії, викладачів та витягує Google Meet посилання.
    """
    params = {
        "group": GROUP_NAME,
        "sdate": sdate,
        "edate": edate
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(SCHEDULE_URL, params=params, timeout=20) as response:
                if response.status != 200:
                    print(f"Помилка завантаження сайту: статус {response.status}")
                    return
                html = await response.text()
        except Exception as e:
            print(f"Помилка HTTP-запиту розкладу: {e}")
            return

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("tr")
    if not rows:
        return

    pool = await get_db_pool()
    current_date_str = ""
    current_dow = ""

    async with pool.acquire() as conn:
        for row in rows:
            # Обробка рядка-заголовка дати
            date_header = row.find(["th", "td"], class_=re.compile(r"date|day|header", re.I))
            if date_header and any(d in date_header.get_text() for d in [".202", ".2025", ".2026"]):
                raw_text = date_header.get_text().strip()
                parts = raw_text.split()
                if len(parts) >= 2:
                    current_date_str = parts[0]
                    current_dow = parts[1]
                else:
                    current_date_str = raw_text
                continue

            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            # Номер та час пари
            lesson_cell = cells[0].get_text().strip()
            num_match = re.search(r"\d+", lesson_cell)
            if not num_match:
                continue
            lesson_number = int(num_match.group(0))

            time_start = ""
            time_end = ""
            time_match = re.findall(r"\d{1,2}:\d{2}", lesson_cell)
            if len(time_match) >= 2:
                time_start, time_end = time_match[0], time_match[1]
            elif len(time_match) == 1:
                time_start = time_match[0]

            content_cell = cells[1]
            cell_text = content_cell.get_text().strip()
            if not cell_text:
                continue

            # Посилання на Meet / Zoom
            meet_url = extract_meet_url(content_cell)

            # Розпізнавання типу та назви
            lesson_type = "Лек"
            if "лаб" in cell_text.lower():
                lesson_type = "Лаб"
            elif "пр" in cell_text.lower():
                lesson_type = "Пр"

            lines = [l.strip() for l in cell_text.split("\n") if l.strip()]
            subject = lines[0] if lines else "Заняття"

            teacher = ""
            room = ""

            for line in lines[1:]:
                if any(w in line.lower() for w in ["ауд", "дистанц"]):
                    room = line
                elif any(w in line.lower() for w in ["доц", "проф", "викл", "ас."]) or len(line.split()) >= 2:
                    teacher = line

            if not room and ("дистанційно" in cell_text.lower() or meet_url):
                room = "Дистанційно"

            # Визначення підгрупи
            target_subgroup = 0
            if "1 підгр" in cell_text.lower() or "підгрупа 1" in cell_text.lower():
                target_subgroup = 1
            elif "2 підгр" in cell_text.lower() or "підгрупа 2" in cell_text.lower():
                target_subgroup = 2

            # Запис у БД
            await conn.execute("""
                INSERT INTO schedule (
                    date_str, day_of_week, lesson_number, time_start, time_end,
                    subject, lesson_type, teacher, room, target_subgroup, meet_url
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (date_str, lesson_number, target_subgroup) 
                DO UPDATE SET 
                    subject = EXCLUDED.subject,
                    lesson_type = EXCLUDED.lesson_type,
                    teacher = EXCLUDED.teacher,
                    room = EXCLUDED.room,
                    meet_url = EXCLUDED.meet_url
            """, current_date_str, current_dow, lesson_number, time_start, time_end,
                 subject, lesson_type, teacher, room, target_subgroup, meet_url)
