import re
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database import get_db_pool

GROUP_ID = 2658  # ID групи КІ-25-1 в системі ІФНТУНГ
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
    if not text:
        return ""
    meet_match = re.search(r'(https?://(?:meet\.google\.com/[a-z0-9\-]+|zoom\.us/j/[0-9\?&=]+|teams\.microsoft\.com/[^\s<>"]+))', text, re.I)
    if meet_match:
        return meet_match.group(1).strip()
    any_link = re.search(r'(https?://[^\s<>"]+)', text, re.I)
    if any_link:
        return any_link.group(1).strip()
    return ""

def clean_text_segment(text: str) -> str:
    if not text:
        return ""
    t = text.replace('\xa0', ' ').replace('\u200b', ' ')
    # Нормалізуємо всі можливі види апострофів до класичного
    t = t.replace('`', "'").replace('’', "'").replace('ʼ', "'")
    return re.sub(r'\s+', ' ', t).strip()

async def fetch_and_save_schedule(sdate: str = "", edate: str = "") -> bool:
    """
    Завантажує та розбирає розклад з сайту ІФНТУНГ з точним виділенням
    назви дисципліни, викладача, типу пари та аудиторії.
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
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=25)) as response:
                if response.status != 200:
                    return False
                html = await response.text()
    except Exception as e:
        print("Помилка з'єднання з сайтом ІФНТУНГ:", e)
        return False

    soup = BeautifulSoup(html, "html.parser")
    pool = await get_db_pool()
    if not pool:
        return False

    date_pattern = re.compile(r'(\d{2}\.\d{2}\.\d{4})')
    days_ua_pattern = re.compile(r'(понеділок|вівторок|середа|четвер|п\'ятниця|субота|неділя)', re.I)

    current_date = ""
    current_dow = ""

    elements = soup.find_all(["div", "tr", "p", "td"])
    for el in elements:
        t = el.get_text(" ", strip=True)
        d_find = date_pattern.search(t)
        if d_find:
            current_date = d_find.group(1)
            dow_find = days_ua_pattern.search(t)
            if dow_find:
                current_dow = dow_find.group(1).capitalize()

    lesson_items = soup.find_all(["div", "tr"], class_=re.compile(r'(lesson|pair|item|row)', re.I))
    if not lesson_items:
        lesson_items = soup.find_all("tr")

    parsed_lessons = []

    # Регулярні вирази
    room_regex = re.compile(r'(\d+[\.\-][А-Яа-яA-Za-z0-9]+(?:\.ауд|\s*ауд)?\.?|дистанційно|онлайн)', re.I)
    teacher_regex = re.compile(
        r'(?:доцент|професор|асистент|викладач|ст\.?\s*викладач|ст\.?\s*викл\.?|доц\.|проф\.|ас\.)\s+[А-ЯЄІЇҐ][а-яєіїґ\'\-]+\s+[А-ЯЄІЇҐ][а-яєіїґ\'\-]+(?:\s+[А-ЯЄІЇҐ][а-яєіїґ\'\-]+)?',
        re.I
    )
    stream_regex = re.compile(r'(?:Збірна\s+група|Потік|Група)\s+[^(\n\r]+(?:\([^\)]*\))?', re.I)
    subgroup_regex = re.compile(r'\(?\s*підгр\.?\s*([12])\s*\)?', re.I)
    type_regex = re.compile(r'\(\s*([ЛL]ек|[ЛL]|[ЛL]аб|[ПP]р|[СC]ем)\s*\)', re.I)

    for item in lesson_items:
        raw_text = clean_text_segment(item.get_text(" ", strip=True))
        if not raw_text:
            continue

        pair_match = re.search(r'([1-7])\s*(?:пара|п\.|\))', raw_text, re.I)
        if not pair_match:
            continue

        lesson_num = int(pair_match.group(1))
        t_start, t_end = LESSON_TIMES.get(lesson_num, ("08:00", "09:20"))

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

        # 1. Посилання (Meet, Zoom, Moodle)
        meet_url = ""
        for a in item.find_all("a", href=True):
            f_meet = extract_meet_url(a['href'])
            if f_meet:
                meet_url = f_meet
                if "meet.google.com" in meet_url or "zoom.us" in meet_url:
                    break
        if not meet_url:
            meet_url = extract_meet_url(raw_text)

        working = raw_text

        # 2. Очищення від дати і номера пари
        working = re.sub(r'^[1-7]\s*(?:пара|п\.|\))\s*', '', working)
        if item_date:
            working = working.replace(item_date, '')
        if item_dow:
            working = working.replace(item_dow, '')

        # 3. Аудиторія
        room = ""
        r_m = room_regex.search(working)
        if r_m:
            room = r_m.group(1).strip()
            working = working[:r_m.start()] + " " + working[r_m.end():]
            if "дистанцій" in room.lower() or "онлайн" in room.lower():
                room = "Дистанційно"
            else:
                clean_r = re.sub(r'[\.\s]*ауд[\.\s]*$', '', room, flags=re.I).strip()
                room = f"{clean_r}.ауд."
        elif meet_url:
            room = "Онлайн (Google Meet)"

        # 4. Викладач (з підтримкою апострофів у Мар'ян тощо)
        teacher = ""
        t_m = teacher_regex.search(working)
        if t_m:
            teacher = clean_text_segment(t_m.group(0))
            working = working[:t_m.start()] + " " + working[t_m.end():]

        # 5. Потік / Збірна група (вирізаємо повністю)
        working = stream_regex.sub('', working)

        # 6. Підгрупа
        target_sg = 0
        sg_m = subgroup_regex.search(working)
        if sg_m:
            target_sg = int(sg_m.group(1))
            working = subgroup_regex.sub('', working)

        # 7. Тип заняття (Лек, Лаб, Пр)
        lesson_type = "Лек"
        tp_m = type_regex.search(working)
        if tp_m:
            code = tp_m.group(1).upper()
            if 'ЛАБ' in code or 'LАБ' in code:
                lesson_type = "Лаб"
            elif 'ПР' in code or 'PR' in code or 'PР' in code:
                lesson_type = "Пр"
            else:
                lesson_type = "Лек"
            working = working[:tp_m.start()] + " " + working[tp_m.end():]
        else:
            if "лаб" in working.lower():
                lesson_type = "Лаб"
            elif "пр" in working.lower() or "практ" in working.lower():
                lesson_type = "Пр"

        # 8. Вибіркова дисципліна
        elective_num = 0
        if "вибіркова 2" in raw_text.lower() or "вибіркова №2" in raw_text.lower():
            elective_num = 2
        elif "вибіркова 1" in raw_text.lower() or "вибіркова №1" in raw_text.lower() or working.strip().startswith("*"):
            elective_num = 1

        # 9. Чиста назва дисципліни
        subject = clean_text_segment(working)
        subject = re.sub(r'https?://[^\s<>"]+', '', subject)
        subject = re.sub(r'\(\s*(?:Лек|Л|Лаб|Пр|Сем)\s*\)', '', subject, flags=re.I)
        subject = re.sub(r'[\,\;\:\.\-]+$', '', subject).strip()
        subject = re.sub(r'^\s*[\,\;\:\.\-]+', '', subject).strip()
        subject = clean_text_segment(subject)

        if not subject:
            subject = "Заняття"

        parsed_lessons.append({
            "date_str": item_date,
            "day_of_week": item_dow,
            "lesson_number": lesson_num,
            "time_start": t_start,
            "time_end": t_end,
            "subject": subject,
            "lesson_type": lesson_type,
            "teacher": teacher,
            "room": room,
            "target_subgroup": target_sg,
            "meet_url": meet_url,
            "elective_num": elective_num
        })

    if not parsed_lessons:
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
                        subject = EXCLUDED.subject,
                        lesson_type = EXCLUDED.lesson_type
                """, l["date_str"], l["day_of_week"], l["lesson_number"], l["time_start"], l["time_end"],
                     l["subject"], l["lesson_type"], l["teacher"], l["room"], l["target_subgroup"],
                     l["meet_url"], l["elective_num"])

    print(f"[{datetime.now(KYIV_TZ).strftime('%H:%M:%S')}] Успішно оновлено {len(parsed_lessons)} занять з сайту ІФНТУНГ.")
    return True
