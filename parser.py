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

DATE_REGEX = re.compile(r'(\d{2}\.\d{2}\.\d{4})')
DAYS_REGEX = re.compile(r'(понеділок|вівторок|середа|четвер|п\'ятниця|субота|неділя)', re.I)
PAIR_REGEX = re.compile(r'([1-7])\s*(?:пара|п\.|\))', re.I)

ROOM_REGEX = re.compile(r'(\d+[\.\-][А-Яа-яA-Za-z0-9]+(?:\.ауд|\s*ауд)?\.?|дистанційно|онлайн)', re.I)
TEACHER_REGEX = re.compile(
    r'((?:доцент|професор|асистент|викладач|ст\.?\s*викладач|ст\.?\s*викл\.?|доц\.|проф\.|ас\.)\s+[А-ЯЄІЇҐ][а-яєіїґ\'\-]+\s+[А-ЯЄІЇҐ][а-яєіїґ\'\-]+(?:\s+[А-ЯЄІЇҐ][а-яєіїґ\'\-]+)?)',
    re.I
)
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

def split_multi_subgroup_text(raw_text: str) -> list:
    """Розділяє пари, якщо підгрупа 1 та підгрупа 2 записані в одній клітинці."""
    has_sg1 = bool(re.search(r'підгр\.?\s*1', raw_text, re.I))
    has_sg2 = bool(re.search(r'підгр\.?\s*2', raw_text, re.I))

    if has_sg1 and has_sg2:
        rooms = list(ROOM_REGEX.finditer(raw_text))
        if len(rooms) >= 2:
            split_pos = rooms[0].end()
            part1 = clean_text(raw_text[:split_pos])
            part2 = clean_text(raw_text[split_pos:])
            return [part1, part2]

    return [clean_text(raw_text)]

def parse_lesson_segment(segment: str, date_str: str, dow_str: str, lesson_num: int, default_meet: str = "") -> dict:
    t_start, t_end = LESSON_TIMES.get(lesson_num, ("08:00", "09:20"))
    working = segment

    # Meet
    meet_url = default_meet or extract_meet_url(working)

    # Аудиторія
    room = ""
    r_m = ROOM_REGEX.search(working)
    if r_m:
        raw_room = r_m.group(1).strip()
        working = working[:r_m.start()] + " " + working[r_m.end():]
        if "дистанцій" in raw_room.lower() or "онлайн" in raw_room.lower():
            room = "Дистанційно"
        else:
            clean_r = re.sub(r'[\.\s]*ауд[\.\s]*$', '', raw_room, flags=re.I).strip()
            room = f"{clean_r}.ауд."
    elif meet_url:
        room = "Онлайн (Google Meet)"

    # Викладач
    teacher = ""
    t_m = TEACHER_REGEX.search(working)
    if t_m:
        teacher = clean_text(t_m.group(0))
        working = working[:t_m.start()] + " " + working[t_m.end():]

    # Видалення потоків / збірних груп
    working = STREAM_REGEX.sub('', working)

    # Підгрупа
    target_sg = 0
    sg_m = SUBGROUP_REGEX.search(working)
    if sg_m:
        target_sg = int(sg_m.group(1))
        working = SUBGROUP_REGEX.sub('', working)

    # Тип заняття
    lesson_type = "Лек"
    tp_m = TYPE_REGEX.search(working)
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
    if "вибіркова 2" in segment.lower() or "вибіркова №2" in segment.lower():
        elective_num = 2
    elif "вибіркова 1" in segment.lower() or "вибіркова №1" in segment.lower() or working.strip().startswith("*"):
        elective_num = 1

    # Чиста назва дисципліни
    subject = clean_text(working)
    subject = re.sub(r'^[1-7]\s*(?:пара|п\.|\))\s*', '', subject)
    subject = re.sub(r'https?://[^\s<>"]+', '', subject)
    subject = re.sub(r'\(\s*(?:Лек|Л|Лаб|Пр|Сем)\s*\)', '', subject, flags=re.I)
    subject = re.sub(r'[\,\;\:\.\-]+$', '', subject.strip()).strip()
    subject = re.sub(r'^\s*[\,\;\:\.\-]+', '', subject).strip()
    subject = clean_text(subject)

    if not subject:
        subject = "Заняття"

    return {
        "date_str": date_str,
        "day_of_week": dow_str,
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
    }

def parse_html_content(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    parsed_lessons = []

    # Знаходимо всі рядки занять
    lesson_items = soup.find_all(["div", "tr"], class_=re.compile(r'(lesson|pair|item|row)', re.I))
    if not lesson_items:
        lesson_items = soup.find_all("tr")

    for item in lesson_items:
        raw_text = clean_text(item.get_text(" ", strip=True))
        if not raw_text:
            continue

        p_match = PAIR_REGEX.search(raw_text)
        if not p_match:
            continue

        lesson_num = int(p_match.group(1))

        # Багаторівневий пошук дати
        item_date = ""
        item_dow = ""

        # 1. Безпосередньо в тексті
        d_find = DATE_REGEX.search(raw_text)
        if d_find:
            item_date = d_find.group(1)

        # 2. У батьківських контейнерах (кожен день згрупований у свій блок)
        if not item_date and item.parent:
            p_text = clean_text(item.parent.get_text(" ", strip=True))
            d_parent = DATE_REGEX.search(p_text)
            if d_parent:
                item_date = d_parent.group(1)

        # 3. У дідусевому контейнері
        if not item_date and item.parent and item.parent.parent:
            gp_text = clean_text(item.parent.parent.get_text(" ", strip=True))
            d_gp = DATE_REGEX.search(gp_text)
            if d_gp:
                item_date = d_gp.group(1)

        # 4. Реверсивний пошук попереднього текстового вузла з датою
        if not item_date:
            prev_elem = item.find_previous(string=DATE_REGEX)
            if prev_elem:
                d_prev = DATE_REGEX.search(str(prev_elem))
                if d_prev:
                    item_date = d_prev.group(1)

        if not item_date:
            continue

        try:
            dt = datetime.strptime(item_date, "%d.%m.%Y")
            item_dow = DAYS_UA[dt.weekday()]
        except Exception:
            item_dow = ""

        meet_url = ""
        for a in item.find_all('a', href=True):
            f_meet = extract_meet_url(a['href'])
            if f_meet:
                meet_url = f_meet
                break

        segments = split_multi_subgroup_text(raw_text)
        for seg in segments:
            lesson_data = parse_lesson_segment(seg, item_date, item_dow, lesson_num, default_meet=meet_url)
            parsed_lessons.append(lesson_data)

    return parsed_lessons

def parse_ical_content(raw_ics: str) -> list:
    if "BEGIN:VEVENT" not in raw_ics:
        return []

    unfolded = re.sub(r'\r?\n[ \t]', '', raw_ics)
    raw_events = re.split(r'BEGIN:VEVENT', unfolded, flags=re.I)

    lessons = []
    for ev in raw_events[1:]:
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

        summary = clean_text(summary_m.group(1).replace("\r", "").replace("\n", " ")) if summary_m else ""
        room = clean_text(location_m.group(1).replace("\r", "").replace("\n", " ")) if location_m else ""
        desc = desc_m.group(1).replace("\\n", "\n").replace("\\,", ",") if desc_m else ""

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
        if "підгр. 1" in summary.lower() or "1 підгр" in summary.lower():
            target_sg = 1
        elif "підгр. 2" in summary.lower() or "2 підгр" in summary.lower():
            target_sg = 2

        elective_num = 1 if (summary.startswith("*") or "вибіркова" in summary.lower()) else 0

        teacher = ""
        if desc:
            lines = [l.strip() for l in desc.splitlines() if l.strip()]
            if lines:
                teacher = clean_text(lines[0])

        lessons.append({
            "date_str": date_str,
            "day_of_week": dow,
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
        sdate = (now_dt - timedelta(days=now_dt.weekday())).strftime("%d.%m.%Y")
        edate = (now_dt + timedelta(days=21)).strftime("%d.%m.%Y")

    parsed_lessons = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8"
    }
    connector = aiohttp.TCPConnector(ssl=False)

    # 1. Спроба через HTML сторінку розкладу
    try:
        params = {"group": GROUP_ID, "sdate": sdate.strip(), "edate": edate.strip()}
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            async with session.get(BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    parsed_lessons = parse_html_content(html)
                    print(f"[PARSER] Отримано {len(parsed_lessons)} пар з HTML сторінки.")
    except Exception as e:
        print(f"[PARSER] Помилка HTML-запиту: {e}")

    # 2. Резервний канал: iCal експорт
    if not parsed_lessons:
        try:
            print("[PARSER] Використовуємо резервний iCal експорт...")
            async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
                async with session.get(ICAL_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        raw_ics = await resp.text()
                        parsed_lessons = parse_ical_content(raw_ics)
                        print(f"[PARSER] Отримано {len(parsed_lessons)} пар з iCal експорту.")
        except Exception as e:
            print(f"[PARSER] Помилка iCal-запиту: {e}")

    if not parsed_lessons:
        print("[PARSER] Не вдалося знайти жодної пари.")
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

    print(f"[PARSER] База даних оновлена. Збережено {len(parsed_lessons)} занять.")
    return True
