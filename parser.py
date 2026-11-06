import re
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from database import get_db_pool

GROUP_ID = 2658  # ID групи КІ-25-1 в системі ІФНТУНГ
BASE_URL = "https://online.nung.edu.ua/schedule/view"

# Розклад дзвінків ІФНТУНГ
LESSON_TIMES = {
    1: ("08:00", "09:20"),
    2: ("09:30", "10:50"),
    3: ("11:00", "12:20"),
    4: ("12:50", "14:10"),
    5: ("14:20", "15:40"),
    6: ("15:50", "17:10"),
    7: ("17:20", "18:40")
}

def extract_meet_url(text: str) -> str:
    """Витягує посилання на відеозустріч (Google Meet, Zoom, Teams). Якщо немає — бере перше HTTP-посилання."""
    if not text:
        return ""
    
    # 1. Шукаємо прямі посилання на платформи відеозв'язку
    meet_match = re.search(r'(https?://(?:meet\.google\.com/[a-z0-9\-]+|zoom\.us/j/[0-9\?&=]+|teams\.microsoft\.com/[^\s<>"]+))', text, re.I)
    if meet_match:
        return meet_match.group(1).strip()
    
    # 2. Якщо специфічного відеозв'язку немає, беремо будь-яке перше валідне посилання (наприклад, Moodle)
    any_link = re.search(r'(https?://[^\s<>"]+)', text, re.I)
    if any_link:
        return any_link.group(1).strip()
        
    return ""

def detect_subgroup(text: str) -> int:
    """Визначає цільову підгрупу (1, 2 або 0 для всієї групи)."""
    text_lower = text.lower()
    if "підгр. 1" in text_lower or "1 підгр" in text_lower or "(1 підгрупа)" in text_lower or "підгр.1" in text_lower:
        return 1
    if "підгр. 2" in text_lower or "2 підгр" in text_lower or "(2 підгрупа)" in text_lower or "підгр.2" in text_lower:
        return 2
    return 0

def detect_elective(text: str) -> int:
    """Визначає номер вибіркової дисципліни (якщо назва містить зірочку чи помітку)."""
    text_lower = text.lower()
    if "вибіркова 1" in text_lower or "вибіркова №1" in text_lower:
        return 1
    if "вибіркова 2" in text_lower or "вибіркова №2" in text_lower:
        return 2
    if text.strip().startswith("*") or "вибіркова" in text_lower:
        return 1
    return 0

async def fetch_and_save_schedule(sdate: str, edate: str):
    """
    Завантажує офіційний розклад з сайту ІФНТУНГ за діапазоном дат
    і зберігає в таблицю schedule з оновленням посилань та кабінетів.
    """
    params = {
        "group": GROUP_ID,
        "sdate": sdate,
        "edate": edate
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        try:
            async with session.get(BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=25)) as response:
                if response.status != 200:
                    print(f"Помилка отримання розкладу: HTTP {response.status}")
                    return
                html = await response.text()
        except Exception as e:
            print("Помилка з'єднання з сервером розкладу ІФНТУНГ:", e)
            return

    soup = BeautifulSoup(html, "html.parser")
    pool = await get_db_pool()

    # Парсинг структури сторінки розкладу
    # Перебираємо всі блоки днів або рядки таблиці
    day_blocks = soup.find_all(["div", "table"], class_=re.compile(r'(day|schedule|card|block)', re.I))
    
    # Якщо структура таблична
    rows = soup.find_all("tr")
    current_date = ""
    current_dow = ""

    parsed_lessons = []

    # Регулярний вираз для пошуку дат формату ДД.ММ.РРРР
    date_pattern = re.compile(r'(\d{2}\.\d{2}\.\d{4})')
    days_ua_pattern = re.compile(r'(понеділок|вівторок|середа|четвер|п\'ятниця|субота|неділя)', re.I)

    # Загальний обхід елементів розкладу
    elements = soup.find_all(["div", "tr", "p", "td"])
    for el in elements:
        text = el.get_text(" ", strip=True)
        date_find = date_pattern.search(text)
        if date_find:
            current_date = date_find.group(1)
            dow_find = days_ua_pattern.search(text)
            if dow_find:
                current_dow = dow_find.group(1).capitalize()

    # Сканування занять у таблиці/картках
    lesson_items = soup.find_all(["div", "tr"], class_=re.compile(r'(lesson|pair|item|row)', re.I))
    if not lesson_items:
        lesson_items = soup.find_all("tr")

    for item in lesson_items:
        text = item.get_text(" ", strip=True)
        if not text:
            continue

        # Перевірка наявності номера пари
        pair_match = re.search(r'([1-7])\s*(?:пара|п\.|\))', text, re.I)
        if not pair_match:
            continue

        lesson_num = int(pair_match.group(1))
        t_start, t_end = LESSON_TIMES.get(lesson_num, ("08:00", "09:20"))

        # Перевірка дати в межах поточного або батьківського блоку
        item_date = current_date
        item_dow = current_dow
        parent_text = item.parent.get_text(" ", strip=True) if item.parent else ""
        parent_date_match = date_pattern.search(parent_text)
        if parent_date_match:
            item_date = parent_date_match.group(1)
            dow_m = days_ua_pattern.search(parent_text)
            if dow_m:
                item_dow = dow_m.group(1).capitalize()

        if not item_date:
            continue

        # Витягуємо посилання без прив'язки до слова "дистанційно"
        meet_url = ""
        # Шукаємо в тегах <a>
        links = item.find_all("a", href=True)
        for a in links:
            href = a['href']
            found_meet = extract_meet_url(href)
            if found_meet:
                meet_url = found_meet
                if "meet.google.com" in meet_url or "zoom.us" in meet_url:
                    break  # Пріоритет зустрічі
        
        # Якщо в тегах <a> не було, шукаємо в тексті
        if not meet_url:
            meet_url = extract_meet_url(text)

        # Тип пари
        l_type = "Лек"
        if "(Лаб)" in text or "Лаб" in text or "лабораторна" in text.lower():
            l_type = "Лаб"
        elif "(Пр)" in text or "Пр" in text or "практична" in text.lower():
            l_type = "Пр"

        # Аудиторія
        room = ""
        room_match = re.search(r'(\d+[\.\-][А-Яа-я0-9]+(?:\.ауд|\s*ауд)?|дистанційно|онлайн)', text, re.I)
        if room_match:
            room = room_match.group(1).strip()
        elif meet_url:
            room = "Онлайн (Google Meet)"

        # Викладач
        teacher = ""
        teacher_match = re.search(r'((?:доцент|професор|асистент|викладач)?\s*[А-ЯЄІЇ][а-яєії]+\s+[А-ЯЄІЇ][а-яєії]+\s+[А-ЯЄІЇ][а-яєії]+)', text)
        if teacher_match:
            teacher = teacher_match.group(1).strip()

        # Назва дисципліни
        # Очищуємо зайві маркери
        subject = text
        for remove_part in [item_date, item_dow, room, teacher, meet_url, "пара"]:
            if remove_part:
                subject = subject.replace(remove_part, "")
        subject = re.sub(r'https?://[^\s<>"]+', '', subject)
        subject = re.sub(r'[1-7]\s*пара', '', subject)
        subject = re.sub(r'\s+', ' ', subject).strip()
        if len(subject) > 80:
            subject = subject[:77] + "..."

        target_sg = detect_subgroup(text)
        elective_num = detect_elective(text)

        parsed_lessons.append({
            "date_str": item_date,
            "day_of_week": item_dow,
            "lesson_number": lesson_num,
            "time_start": t_start,
            "time_end": t_end,
            "subject": subject or "Заняття",
            "lesson_type": l_type,
            "teacher": teacher,
            "room": room,
            "target_subgroup": target_sg,
            "meet_url": meet_url,
            "elective_num": elective_num
        })

    # Збереження розібраних занять у базі даних Postgres (Supabase)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for l in parsed_lessons:
                await conn.execute("""
                    INSERT INTO schedule (
                        date_str, day_of_week, lesson_number, time_start, time_end,
                        subject, lesson_type, teacher, room, target_subgroup, meet_url, elective_num
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (date_str, lesson_number, target_subgroup, COALESCE(elective_num, 0))
                    DO UPDATE SET 
                        room = CASE WHEN EXCLUDED.room != '' THEN EXCLUDED.room ELSE schedule.room END,
                        teacher = CASE WHEN EXCLUDED.teacher != '' THEN EXCLUDED.teacher ELSE schedule.teacher END,
                        meet_url = CASE WHEN EXCLUDED.meet_url != '' THEN EXCLUDED.meet_url ELSE schedule.meet_url END,
                        subject = CASE WHEN EXCLUDED.subject != '' THEN EXCLUDED.subject ELSE schedule.subject END,
                        lesson_type = EXCLUDED.lesson_type
                """, l["date_str"], l["day_of_week"], l["lesson_number"], l["time_start"], l["time_end"],
                     l["subject"], l["lesson_type"], l["teacher"], l["room"], l["target_subgroup"],
                     l["meet_url"], l["elective_num"])

    print(f"[{datetime.now()}] Розклад успішно синхронізовано з сайту ІФНТУНГ: {len(parsed_lessons)} занять оновлено.")
