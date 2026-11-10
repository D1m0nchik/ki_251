import re
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database import get_db_pool

GROUP_ID = 2658  # ID групи КІ-25-1 в системі розкладу ІФНТУНГ
BASE_URL = "https://online.nung.edu.ua/schedule/view"
KYIV_TZ = ZoneInfo("Europe/Kyiv")

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
    """Витягує посилання на відеоконференцію або зовнішній ресурс."""
    if not text:
        return ""
    meet_match = re.search(r'(https?://(?:meet\.google\.com/[a-z0-9\-]+|zoom\.us/j/[0-9\?&=]+|teams\.microsoft\.com/[^\s<>"]+))', text, re.I)
    if meet_match:
        return meet_match.group(1).strip()
    any_link = re.search(r'(https?://[^\s<>"]+)', text, re.I)
    if any_link:
        return any_link.group(1).strip()
    return ""

def detect_subgroup(text: str) -> int:
    """Визначає цільову підгрупу заняття."""
    text_lower = text.lower()
    if "підгр. 1" in text_lower or "1 підгр" in text_lower or "(1 підгрупа)" in text_lower or "підгр.1" in text_lower:
        return 1
    if "підгр. 2" in text_lower or "2 підгр" in text_lower or "(2 підгрупа)" in text_lower or "підгр.2" in text_lower:
        return 2
    return 0

def detect_elective(text: str) -> int:
    """Визначає номер вибіркової дисципліни."""
    text_lower = text.lower()
    if "вибіркова 2" in text_lower or "вибіркова №2" in text_lower:
        return 2
    if "вибіркова 1" in text_lower or "вибіркова №1" in text_lower or text.strip().startswith("*") or "вибіркова" in text_lower:
        return 1
    return 0

async def fetch_and_save_schedule(sdate: str = "", edate: str = "") -> bool:
    """
    Завантажує офіційний HTML-розклад з сайту ІФНТУНГ та оновлює базу даних.
    """
    if not sdate or not edate:
        now_dt = datetime.now(KYIV_TZ)
        sdate = now_dt.strftime("%d.%m.%Y")
        edate = (now_dt + timedelta(days=21)).strftime("%d.%m.%Y")

    params = {
        "group": GROUP_ID,
        "sdate": sdate.strip(),
        "edate": edate.strip()
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            async with session.get(BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=25)) as response:
                if response.status != 200:
                    print(f"[PARSER] Помилка HTTP {response.status}")
                    return False
                html = await response.text()
    except Exception as e:
        print(f"[PARSER] Помилка з'єднання з сервером розкладу: {e}")
        return False

    soup = BeautifulSoup(html, "html.parser")
    pool = await get_db_pool()
    if not pool:
        return False

    date_pattern = re.compile(r'(\d{2}\.\d{2}\.\d{4})')
    days_ua_pattern = re.compile(r'(понеділок|вівторок|середа|четвер|п\'ятниця|субота|неділя)', re.I)

    # Знаходимо всі контейнери занять
    lesson_items = soup.find_all(["div", "tr"], class_=re.compile(r'(lesson|pair|item|row)', re.I))
    if not lesson_items:
        lesson_items = soup.find_all("tr")

    parsed_lessons = []

    for item in lesson_items:
        text = item.get_text(" ", strip=True)
        if not text:
            continue

        pair_match = re.search(r'([1-7])\s*(?:пара|п\.|\))', text, re.I)
        if not pair_match:
            continue

        lesson_num = int(pair_match.group(1))
        t_start, t_end = LESSON_TIMES.get(lesson_num, ("08:00", "09:20"))

        # Визначаємо дату для поточної пари (з тексту або попередніх вузлів)
        item_date = ""
        item_dow = ""

        d_find = date_pattern.search(text)
        if d_find:
            item_date = d_find.group(1)
            dow_find = days_ua_pattern.search(text)
            if dow_find:
                item_dow = dow_find.group(1).capitalize()
        else:
            prev_date_elem = item.find_previous(string=date_pattern)
            if prev_date_elem:
                p_match = date_pattern.search(str(prev_date_elem))
                if p_match:
                    item_date = p_match.group(1)
                    dow_prev = days_ua_pattern.search(str(prev_date_elem))
                    if dow_prev:
                        item_dow = dow_prev.group(1).capitalize()

        if not item_date:
            continue

        if not item_dow:
            try:
                days_ua = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
                dt_obj = datetime.strptime(item_date, "%d.%m.%Y")
                item_dow = days_ua[dt_obj.weekday()]
            except Exception:
                pass

        # Знаходимо посилання (Meet / Zoom / Teams)
        meet_url = ""
        for a in item.find_all("a", href=True):
            found_meet = extract_meet_url(a['href'])
            if found_meet:
                meet_url = found_meet
                if "meet.google.com" in meet_url or "zoom.us" in meet_url:
                    break
        if not meet_url:
            meet_url = extract_meet_url(text)

        # Тип заняття
        l_type = "Лек"
        if "(Лаб)" in text or "Лаб" in text or "лабораторна" in text.lower():
            l_type = "Лаб"
        elif "(Пр)" in text or "Пр" in text or "практична" in text.lower():
            l_type = "Пр"

        # Аудиторія
        room = ""
        room_match = re.search(r'(\d+[\.\-][А-Яа-яA-Za-z0-9]+(?:\.ауд|\s*ауд)?\.?|дистанційно|онлайн)', text, re.I)
        if room_match:
            room = room_match.group(1).strip()
            if "дистанцій" in room.lower() or "онлайн" in room.lower():
                room = "Дистанційно"
            else:
                clean_r = re.sub(r'[\.\s]*ауд[\.\s]*$', '', room, flags=re.I).strip()
                room = f"{clean_r}.ауд."
        elif meet_url:
            room = "Онлайн (Google Meet)"

        # Викладач
        teacher = ""
        teacher_match = re.search(
            r'((?:доцент|професор|асистент|викладач|ст\.?\s*викладач|ст\.?\s*викл\.?|доц\.|проф\.|ас\.)\s+[А-ЯЄІЇҐ][а-яєіїґ\'’`ʼ\-]+\s+[А-ЯЄІЇҐ][а-яєіїґ\'’`ʼ\-]+(?:\s+[А-ЯЄІЇҐ][а-яєіїґ\'’`ʼ\-]+)?)',
            text, re.I
        )
        if teacher_match:
            teacher = re.sub(r'\s+', ' ', teacher_match.group(1)).strip()

        # Очищення назви предмета від зайвих метаданих
        subject = text
        for remove_part in [item_date, item_dow, room, teacher, meet_url, "пара"]:
            if remove_part:
                subject = subject.replace(remove_part, "")
        subject = re.sub(r'https?://[^\s<>"]+', '', subject)
        subject = re.sub(r'[1-7]\s*пара', '', subject)
        subject = re.sub(r'\(?\s*підгр\.?\s*[12]\s*\)?', '', subject, flags=re.I)
        subject = re.sub(r'(?:Збірна\s+група|Потік|Група)[^(\n\r]*(?:\([^\)]*\))?', '', subject, flags=re.I)
        subject = re.sub(r'\(\s*(?:Лек|Л|Лаб|Пр|Сем)\s*\)', '', subject, flags=re.I)
        subject = re.sub(r'[\,\;\:\.\-]+$', '', subject.strip()).strip()
        subject = re.sub(r'^\s*[\,\;\:\.\-]+', '', subject).strip()
        subject = re.sub(r'\s+', ' ', subject).strip()

        if not subject:
            subject = "Заняття"

        target_sg = detect_subgroup(text)
        elective_num = detect_elective(text)

        parsed_lessons.append({
            "date_str": item_date,
            "day_of_week": item_dow,
            "lesson_number": lesson_num,
            "time_start": t_start,
            "time_end": t_end,
            "subject": subject,
            "lesson_type": l_type,
            "teacher": teacher,
            "room": room,
            "target_subgroup": target_sg,
            "meet_url": meet_url,
            "elective_num": elective_num
        })

    if not parsed_lessons:
        print("[PARSER] Не знайдено занять для збереження.")
        return False

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

    print(f"[PARSER] [{datetime.now(KYIV_TZ).strftime('%H:%M:%S')}] Успішно оновлено {len(parsed_lessons)} пар з сайту ІФНТУНГ.")
    return True
