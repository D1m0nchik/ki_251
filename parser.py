import re
import aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database import get_db_pool

GROUP_OBJ_ID = -2281  # OBJ_ID групи КІ-25-1 в системі деканату ІФНТУНГ
DEKANAT_EXPORT_URL = "https://dekanat.nung.edu.ua/cgi-bin/timetable_export.cgi"
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

ROOM_REGEX = re.compile(r'(\d+[\.\-][А-Яа-яA-Za-z0-9]+(?:\.ауд|\s*ауд)?\.?|дистанційно|онлайн)', re.I)
STREAM_REGEX = re.compile(r'(?:Збірна\s+група|Потік|Група)[^(\n\r]*(?:\([^\)]*\))?', re.I)
SUBGROUP_REGEX = re.compile(r'\(?\s*підгр\.?\s*([12])\s*\)?', re.I)
TYPE_REGEX = re.compile(r'\(\s*([ЛL]ек|[ЛL]|[ЛL]аб|[ПP]р|[СC]ем)\s*\)', re.I)

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

def clean_subject_name(raw_name: str) -> str:
    """Видаляє з назви зайві мітки вибіркових, підгруп і типів пар."""
    s = clean_text(raw_name)
    s = STREAM_REGEX.sub('', s)
    s = SUBGROUP_REGEX.sub('', s)
    s = re.sub(r'\(\s*(?:Лек|Л|Лаб|Пр|Сем)\s*\)', '', s, flags=re.I)
    s = re.sub(r'[\,\;\:\.\-]+$', '', s).strip()
    s = re.sub(r'^\s*[\,\;\:\.\-]+', '', s).strip()
    s = clean_text(s)
    return s or "Заняття"

def parse_ical_content(raw_ics: str) -> list:
    if "BEGIN:VEVENT" not in raw_ics:
        return []

    # Розгортаємо перенесені рядки згідно з RFC 5545
    unfolded = re.sub(r'\r?\n[ \t]', '', raw_ics)
    raw_events = re.split(r'BEGIN:VEVENT', unfolded, flags=re.I)

    lessons = []
    for ev in raw_events[1:]:
        # DTSTART може бути YYYYMMDDTHHMMSS або YYYYMMDDTHHMM
        dtstart_m = re.search(r'DTSTART[^:\r\n]*:(\d{8})(?:T(\d{2})(\d{2}))?', ev)
        if not dtstart_m:
            continue

        raw_d = dtstart_m.group(1)
        y, m, d = raw_d[0:4], raw_d[4:6], raw_d[6:8]
        date_str = f"{d}.{m}.{y}"

        h = dtstart_m.group(2) or "08"
        minute = dtstart_m.group(3) or "00"
        t_start = f"{h}:{minute}"

        dtend_m = re.search(r'DTEND[^:\r\n]*:(\d{8})(?:T(\d{2})(\d{2}))?', ev)
        if dtend_m and dtend_m.group(2):
            t_end = f"{dtend_m.group(2)}:{dtend_m.group(3)}"
        else:
            t_end = "09:20"

        try:
            dow = DAYS_UA[datetime(int(y), int(m), int(d)).weekday()]
        except Exception:
            dow = ""

        summary_m = re.search(r'SUMMARY:(.*?)(?:\r?\n[A-Z]|\r?\n?$)', ev, re.S)
        location_m = re.search(r'LOCATION:(.*?)(?:\r?\n[A-Z]|\r?\n?$)', ev, re.S)
        desc_m = re.search(r'DESCRIPTION:(.*?)(?:\r?\n[A-Z]|\r?\n?$)', ev, re.S)

        raw_summary = summary_m.group(1).replace("\r", "").replace("\n", " ") if summary_m else ""
        location = clean_text(location_m.group(1).replace("\r", "").replace("\n", " ")) if location_m else ""
        desc = desc_m.group(1).replace("\\n", "\n").replace("\\,", ",") if desc_m else ""

        # Визначаємо номер пари за часом початку
        lesson_num = 1
        if t_start.startswith("08:"): lesson_num = 1
        elif t_start.startswith("09:"): lesson_num = 2
        elif t_start.startswith("11:"): lesson_num = 3
        elif t_start.startswith("12:") or t_start.startswith("13:"): lesson_num = 4
        elif t_start.startswith("14:"): lesson_num = 5
        elif t_start.startswith("15:") or t_start.startswith("16:"): lesson_num = 6
        elif t_start.startswith("17:"): lesson_num = 7

        meet_url = extract_meet_url(desc) or extract_meet_url(raw_summary)

        # Підгрупа
        target_sg = 0
        if "підгр. 1" in raw_summary.lower() or "1 підгр" in raw_summary.lower():
            target_sg = 1
        elif "підгр. 2" in raw_summary.lower() or "2 підгр" in raw_summary.lower():
            target_sg = 2

        # Вибіркова дисципліна
        elective_num = 0
        if "вибіркова 2" in raw_summary.lower() or "вибіркова №2" in raw_summary.lower():
            elective_num = 2
        elif "вибіркова 1" in raw_summary.lower() or "вибіркова №1" in raw_summary.lower() or raw_summary.strip().startswith("*"):
            elective_num = 1

        # Тип заняття
        lesson_type = "Лек"
        if "лаб" in raw_summary.lower():
            lesson_type = "Лаб"
        elif "пр" in raw_summary.lower():
            lesson_type = "Пр"

        # Викладач
        teacher = ""
        if desc:
            lines = [l.strip() for l in desc.splitlines() if l.strip()]
            if lines:
                teacher = clean_text(lines[0])

        clean_subj = clean_subject_name(raw_summary)

        lessons.append({
            "date_str": date_str,
            "day_of_week": dow,
            "lesson_number": lesson_num,
            "time_start": t_start,
            "time_end": t_end,
            "subject": clean_subj,
            "lesson_type": lesson_type,
            "teacher": teacher,
            "room": location,
            "target_subgroup": target_sg,
            "meet_url": meet_url,
            "elective_num": elective_num
        })

    return lessons

async def fetch_and_save_schedule(sdate: str = "", edate: str = "") -> bool:
    """
    Запитує офіційний iCal розклад групи КІ-25-1 за вказаним періодом
    (за замовчуванням — на 30 днів уперед) і зберігає в Supabase.
    """
    now_dt = datetime.now(KYIV_TZ)
    if not sdate:
        sdate = (now_dt - timedelta(days=now_dt.weekday())).strftime("%d.%m.%Y")
    if not edate:
        edate = (now_dt + timedelta(days=30)).strftime("%d.%m.%Y")

    # Параметри експорту системи Деканат ІФНТУНГ з обов'язковими датами періоду
    params = {
        "req_type": "rozklad",
        "req_mode": "group",
        "OBJ_ID": str(GROUP_OBJ_ID),
        "req_format": "iCal",
        "coding_mode": "UTF8",
        "bs": "ok",
        "fk": "1",
        "sdate": sdate.strip(),
        "edate": edate.strip()
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/calendar,text/plain,*/*"
    }

    print(f"[PARSER] Запит розкладу: з {sdate} по {edate} (OBJ_ID={GROUP_OBJ_ID})")
    connector = aiohttp.TCPConnector(ssl=False)

    try:
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            async with session.get(DEKANAT_EXPORT_URL, params=params, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status != 200:
                    print(f"[PARSER] Помилка відповіді сервера: HTTP {resp.status}")
                    return False
                raw_ics = await resp.text()
    except Exception as e:
        print(f"[PARSER] Помилка мережевого з'єднання: {e}")
        return False

    parsed_lessons = parse_ical_content(raw_ics)
    print(f"[PARSER] Знайдено та розібрано {len(parsed_lessons)} пар на період {sdate} — {edate}.")

    if not parsed_lessons:
        return False

    pool = await get_db_pool()
    if not pool:
        print("[PARSER] Помилка: немає пулу підключення до БД.")
        return False

    async with pool.acquire() as conn:
        async with conn.transaction():
            for l in parsed_lessons:
                # Шукаємо існуючий слот
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

    print(f"[PARSER] Успішно збережено в Supabase {len(parsed_lessons)} занять.")
    return True
