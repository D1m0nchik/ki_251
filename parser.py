import re
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database import get_db_pool

GROUP_ID = 2658  # ID групи КІ-25-1 в HTML системі ІФНТУНГ
BASE_URL = "https://online.nung.edu.ua/schedule/view"
ICAL_URL = "https://dekanat.nung.edu.ua/cgi-bin/timetable_export.cgi?req_type=rozklad&req_mode=group&OBJ_ID=-2281&req_format=iCal&coding_mode=UTF8&bs=ok&fk=1"
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

DAYS_UA = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]

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

def clean_text(text: str) -> str:
    if not text:
        return ""
    t = text.replace('\xa0', ' ').replace('\u200b', ' ')
    t = t.replace('`', "'").replace('’', "'").replace('ʼ', "'")
    return re.sub(r'\s+', ' ', t).strip()

async def fetch_html_schedule(sdate: str, edate: str):
    params = {"group": GROUP_ID, "sdate": sdate, "edate": edate}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    connector = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        async with session.get(BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            return await resp.text()

async def fetch_ical_schedule():
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    connector = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        async with session.get(ICAL_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            return await resp.text()

def parse_html_content(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    date_regex = re.compile(r'(\d{2}\.\d{2}\.\d{4})')
    days_regex = re.compile(r'(понеділок|вівторок|середа|четвер|п\'ятниця|субота|неділя)', re.I)
    pair_regex = re.compile(r'([1-7])\s*(?:пара|п\.|\))', re.I)

    room_regex = re.compile(r'(\d+[\.\-][А-Яа-яA-Za-z0-9]+(?:\.ауд|\s*ауд)?\.?|дистанційно|онлайн)', re.I)
    teacher_regex = re.compile(
        r'(?:доцент|професор|асистент|викладач|ст\.?\s*викладач|ст\.?\s*викл\.?|доц\.|проф\.|ас\.)\s+[А-ЯЄІЇҐ][а-яєіїґ\'\-]+\s+[А-ЯЄІЇҐ][а-яєіїґ\'\-]+(?:\s+[А-ЯЄІЇҐ][а-яєіїґ\'\-]+)?',
        re.I
    )
    stream_regex = re.compile(r'(?:Збірна\s+група|Потік|Група)[^(\n\r]*(?:\([^\)]*\))?', re.I)
    subgroup_regex = re.compile(r'\(?\s*підгр\.?\s*([12])\s*\)?', re.I)
    type_regex = re.compile(r'\(\s*([ЛL]ек|[ЛL]|[ЛL]аб|[ПP]р|[СC]ем)\s*\)', re.I)

    # Визначаємо блоки для послідовного лінійного читання
    rows = soup.find_all('tr')
    if not rows or len(rows) < 3:
        rows = soup.find_all(['div', 'p'])

    current_date = ""
    current_dow = ""
    lessons = []

    for item in rows:
        raw_text = clean_text(item.get_text(" ", strip=True))
        if not raw_text:
            continue

        d_match = date_regex.search(raw_text)
        dow_match = days_regex.search(raw_text)
        p_match = pair_regex.search(raw_text)

        # Оновлюємо поточну дату при виявленні заголовка дня
        if d_match and (dow_match or not p_match):
            current_date = d_match.group(1)
            if dow_match:
                current_dow = dow_match.group(1).capitalize()
            else:
                try:
                    dt = datetime.strptime(current_date, "%d.%m.%Y")
                    current_dow = DAYS_UA[dt.weekday()]
                except Exception:
                    current_dow = ""

        # Якщо це рядок заняття
        if p_match:
            if not current_date and d_match:
                current_date = d_match.group(1)
            if not current_date:
                continue

            lesson_num = int(p_match.group(1))
            t_start, t_end = LESSON_TIMES.get(lesson_num, ("08:00", "09:20"))

            # Meet URL
            meet_url = ""
            for a in item.find_all('a', href=True):
                m_found = extract_meet_url(a['href'])
                if m_found:
                    meet_url = m_found
                    break
            if not meet_url:
                meet_url = extract_meet_url(raw_text)

            working = raw_text
            working = re.sub(r'^[1-7]\s*(?:пара|п\.|\))\s*', '', working)
            if current_date:
                working = working.replace(current_date, '')
            if current_dow:
                working = working.replace(current_dow, '')

            # Аудиторія
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

            # Викладач (підтримує апострофи Мар'ян тощо)
            teacher = ""
            t_m = teacher_regex.search(working)
            if t_m:
                teacher = clean_text(t_m.group(0))
                working = working[:t_m.start()] + " " + working[t_m.end():]

            # Очищення від потоків/збірних груп
            working = stream_regex.sub('', working)

            # Підгрупа
            target_sg = 0
            sg_m = subgroup_regex.search(working)
            if sg_m:
                target_sg = int(sg_m.group(1))
                working = subgroup_regex.sub('', working)

            # Тип пари
            lesson_type = "Лек"
            tp_m = type_regex.search(working)
            if tp_m:
                c = tp_m.group(1).upper()
                if 'ЛАБ' in c or 'LАБ' in c:
                    lesson_type = "Лаб"
                elif 'ПР' in c or 'PR' in c:
                    lesson_type = "Пр"
                else:
                    lesson_type = "Лек"
                working = working[:tp_m.start()] + " " + working[tp_m.end():]
            else:
                if "лаб" in working.lower():
                    lesson_type = "Лаб"
                elif "пр" in working.lower():
                    lesson_type = "Пр"

            # Вибіркова
            elective_num = 0
            if "вибіркова 2" in raw_text.lower() or "вибіркова №2" in raw_text.lower():
                elective_num = 2
            elif "вибіркова 1" in raw_text.lower() or "вибіркова №1" in raw_text.lower() or working.strip().startswith("*"):
                elective_num = 1

            # Чиста назва дисципліни
            subject = clean_text(working)
            subject = re.sub(r'https?://[^\s<>"]+', '', subject)
            subject = re.sub(r'\(\s*(?:Лек|Л|Лаб|Пр|Сем)\s*\)', '', subject, flags=re.I)
            subject = re.sub(r'[\,\;\:\.\-]+$', '', subject).strip()
            subject = re.sub(r'^\s*[\,\;\:\.\-]+', '', subject).strip()
            subject = clean_text(subject)

            if not subject:
                subject = "Заняття"

            lessons.append({
                "date_str": current_date,
                "day_of_week": current_dow,
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

    return lessons

def parse_ical_content(raw_ics: str) -> list:
    events = raw_ics.split("BEGIN:VEVENT")
    if len(events) <= 1:
        return []

    lessons = []
    for ev in events[1:]:
        dtstart_m = re.search(r'DTSTART(?:;[^:]+)?:(\d{8}T\d{4})', ev)
        dtend_m = re.search(r'DTEND(?:;[^:]+)?:(\d{8}T\d{4})', ev)
        summary_m = re.search(r'SUMMARY:(.*?)(?:\r?\n[A-Z]|\r?\n$)', ev, re.S)
        location_m = re.search(r'LOCATION:(.*?)(?:\r?\n[A-Z]|\r?\n$)', ev, re.S)
        desc_m = re.search(r'DESCRIPTION:(.*?)(?:\r?\n[A-Z]|\r?\n$)', ev, re.S)

        if not dtstart_m:
            continue

        raw_dt = dtstart_m.group(1)
        y, m, d = raw_dt[0:4], raw_dt[4:6], raw_dt[6:8]
        t_start = f"{raw_dt[9:11]}:{raw_dt[11:13]}"
        
        t_end = "09:20"
        if dtend_m:
            re_end = dtend_m.group(1)
            t_end = f"{re_end[9:11]}:{re_end[11:13]}"

        date_str = f"{d}.{m}.{y}"
        try:
            day_of_week = DAYS_UA[datetime(int(y), int(m), int(d)).weekday()]
        except Exception:
            day_of_week = ""

        summary = clean_text(summary_m.group(1).replace("\r", "").replace("\n", " ")) if summary_m else ""
        room = clean_text(location_m.group(1).replace("\r", "").replace("\n", " ")) if location_m else ""
        desc = desc_m.group(1).replace("\\n", "\n").replace("\r", "").strip() if desc_m else ""

        lesson_num = 1
        if t_start.startswith("08:"): lesson_num = 1
        elif t_start.startswith("09:"): lesson_num = 2
        elif t_start.startswith("11:"): lesson_num = 3
        elif t_start.startswith("12:") or t_start.startswith("13:"): lesson_num = 4
        elif t_start.startswith("14:"): lesson_num = 5
        elif t_start.startswith("15:") or t_start.startswith("16:"): lesson_num = 6
        elif t_start.startswith("17:"): lesson_num = 7

        meet_url = extract_meet_url(desc) or extract_meet_url(summary)

        target_sg = 0
        if "підгр. 1" in summary.lower() or "1 підгр" in summary.lower(): target_sg = 1
        elif "підгр. 2" in summary.lower() or "2 підгр" in summary.lower(): target_sg = 2

        elective_num = 1 if (summary.startswith("*") or "вибіркова" in summary.lower()) else 0

        teacher = ""
        if desc:
            lines = [l.strip() for l in desc.split("\n") if l.strip()]
            if lines:
                teacher = clean_text(lines[0])

        lessons.append({
            "date_str": date_str,
            "day_of_week": day_of_week,
            "lesson_number": lesson_num,
            "time_start": t_start,
            "time_end": t_end,
            "subject": summary or "Заняття",
            "lesson_type": "Лаб" if "Лаб" in summary else "Лек",
            "teacher": teacher,
            "room": room,
            "target_subgroup": target_sg,
            "meet_url": meet_url,
            "elective_num": elective_num
        })

    return lessons

async def fetch_and_save_schedule(sdate: str = "", edate: str = "") -> bool:
    if not sdate or not edate:
        now_dt = datetime.now(KYIV_TZ)
        sdate = now_dt.strftime("%d.%m.%Y")
        edate = (now_dt + timedelta(days=21)).strftime("%d.%m.%Y")

    parsed_lessons = []

    # 1. Спроба завантаження через офіційний HTML розклад
    try:
        html = await fetch_html_schedule(sdate, edate)
        if html:
            parsed_lessons = parse_html_content(html)
    except Exception as e:
        print("Помилка HTML парсингу:", e)

    # 2. Резервний канал: якщо HTML порожній — використовуємо офіційний експорт iCal
    if not parsed_lessons:
        try:
            print("Використовуємо резервний iCal експорт ІФНТУНГ...")
            ics = await fetch_ical_schedule()
            if ics:
                parsed_lessons = parse_ical_content(ics)
        except Exception as e:
            print("Помилка iCal експорту:", e)

    if not parsed_lessons:
        print("Не вдалося отримати жодної пари з обох джерел.")
        return False

    pool = await get_db_pool()
    if not pool:
        return False

    async with pool.acquire() as conn:
        async with conn.transaction():
            for l in parsed_lessons:
                existing_id = await conn.fetchval("""
                    SELECT id FROM schedule 
                    WHERE date_str = $1 
                      AND lesson_number = $2 
                      AND target_subgroup = $3 
                      AND COALESCE(elective_num, 0) = $4
                """, l["date_str"], l["lesson_number"], l["target_subgroup"], l["elective_num"] or 0)

                if existing_id:
                    await conn.execute("""
                        UPDATE schedule SET
                            subject = $1,
                            lesson_type = $2,
                            teacher = CASE WHEN $3 != '' THEN $3 ELSE teacher END,
                            room = CASE WHEN $4 != '' THEN $4 ELSE room END,
                            meet_url = CASE WHEN $5 != '' THEN $5 ELSE meet_url END,
                            day_of_week = $6,
                            time_start = $7,
                            time_end = $8
                        WHERE id = $9
                    """, l["subject"], l["lesson_type"], l["teacher"], l["room"], l["meet_url"],
                         l["day_of_week"], l["time_start"], l["time_end"], existing_id)
                else:
                    await conn.execute("""
                        INSERT INTO schedule (
                            date_str, day_of_week, lesson_number, time_start, time_end,
                            subject, lesson_type, teacher, room, target_subgroup, meet_url, elective_num
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    """, l["date_str"], l["day_of_week"], l["lesson_number"], l["time_start"], l["time_end"],
                         l["subject"], l["lesson_type"], l["teacher"], l["room"], l["target_subgroup"],
                         l["meet_url"], l["elective_num"])

    print(f"[{datetime.now(KYIV_TZ).strftime('%H:%M:%S')}] Синхронізовано {len(parsed_lessons)} занять.")
    return True
