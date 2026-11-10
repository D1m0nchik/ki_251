import re
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from database import get_db_pool

GROUP_ID = 2658  # ID групи КІ-25-1 в системі розкладу ІФНТУНГ
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

    # 1. Посилання Meet
    meet_url = default_meet or extract_meet_url(working)

    # 2. Аудиторія
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

    # 3. Викладач (підтримує апострофи Мар'ян тощо)
    teacher = ""
    t_m = TEACHER_REGEX.search(working)
    if t_m:
        teacher = clean_text(t_m.group(0))
        working = working[:t_m.start()] + " " + working[t_m.end():]

    # 4. Видалення потоків / збірних груп
    working = STREAM_REGEX.sub('', working)

    # 5. Підгрупа
    target_sg = 0
    sg_m = SUBGROUP_REGEX.search(working)
    if sg_m:
        target_sg = int(sg_m.group(1))
        working = SUBGROUP_REGEX.sub('', working)

    # 6. Тип пари
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

    # 7. Вибіркова
    elective_num = 0
    if "вибіркова 2" in segment.lower() or "вибіркова №2" in segment.lower():
        elective_num = 2
    elif "вибіркова 1" in segment.lower() or "вибіркова №1" in segment.lower() or working.strip().startswith("*"):
        elective_num = 1

    # 8. Чиста назва дисципліни
    subject = clean_text(working)
    subject = re.sub(r'^[1-7]\s*(?:пара|п\.|\))\s*', '', subject)
    subject = re.sub(r'https?://[^\s<>"]+', '', subject)
    subject = re.sub(r'\(\s*(?:Лек|Л|Лаб|Пр|Сем)\s*\)', '', subject, flags=re.I)
    subject = re.sub(r'[\,\;\:\.\-]+$', '', subject).strip()
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

def find_date_and_dow(item, raw_text: str):
    """Шукає дату в самому елементі або реверсивно в найближчих попередніх тегах."""
    # 1. Безпосередньо в тексті
    m = DATE_REGEX.search(raw_text)
    if m:
        date_str = m.group(1)
        dow_m = DAYS_REGEX.search(raw_text)
        dow_str = dow_m.group(1).capitalize() if dow_m else ""
        return date_str, dow_str

    # 2. Реверсивний пошук по попередніх блоках документа
    for prev in item.find_all_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'div', 'p', 'b', 'strong', 'th', 'td', 'caption']):
        txt = clean_text(prev.get_text(" ", strip=True))
        if len(txt) > 250:
            continue
        m = DATE_REGEX.search(txt)
        if m:
            date_str = m.group(1)
            dow_m = DAYS_REGEX.search(txt)
            dow_str = dow_m.group(1).capitalize() if dow_m else ""
            return date_str, dow_str

    # 3. Батьківські контейнери
    curr = item.parent
    while curr and curr.name not in ['body', 'html', '[document]']:
        c_text = clean_text(curr.get_text(" ", strip=True))
        m = DATE_REGEX.search(c_text)
        if m:
            date_str = m.group(1)
            dow_m = DAYS_REGEX.search(c_text)
            dow_str = dow_m.group(1).capitalize() if dow_m else ""
            return date_str, dow_str
        curr = curr.parent

    return "", ""

def parse_html_content(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    parsed_lessons = []

    trs = soup.find_all('tr')
    lesson_items = [tr for tr in trs if PAIR_REGEX.search(tr.get_text(" ", strip=True))]

    if not lesson_items:
        divs = soup.find_all(['div', 'li', 'p'])
        for el in divs:
            txt = clean_text(el.get_text(" ", strip=True))
            if PAIR_REGEX.search(txt):
                has_child = any(PAIR_REGEX.search(c.get_text(" ", strip=True)) for c in el.find_all(['div', 'li', 'p']) if c != el)
                if not has_child:
                    lesson_items.append(el)

    for item in lesson_items:
        raw_text = clean_text(item.get_text(" ", strip=True))
        p_match = PAIR_REGEX.search(raw_text)
        if not p_match:
            continue

        lesson_num = int(p_match.group(1))
        item_date, item_dow = find_date_and_dow(item, raw_text)

        if not item_date:
            continue

        if not item_dow:
            try:
                dt = datetime.strptime(item_date, "%d.%m.%Y")
                item_dow = DAYS_UA[dt.weekday()]
            except Exception:
                pass

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
    raw_events = unfolded.split("BEGIN:VEVENT")

    lessons = []
    for block in raw_events[1:]:
        lines = block.splitlines()
        props = {}
        for line in lines:
            if ":" in line:
                k, v = line.split(":", 1)
                k_clean = k.split(";")[0].strip().upper()
                props[k_clean] = v.strip()

        dtstart = props.get("DTSTART", "")
        clean_dt = re.sub(r'[^0-9T]', '', dtstart)
        if len(clean_dt) < 13:
            continue

        y, m, d = clean_dt[0:4], clean_dt[4:6], clean_dt[6:8]
        t_start = f"{clean_dt[9:11]}:{clean_dt[11:13]}"
        date_str = f"{d}.{m}.{y}"

        dtend = props.get("DTEND", "")
        clean_end = re.sub(r'[^0-9T]', '', dtend)
        t_end = f"{clean_end[9:11]}:{clean_end[11:13]}" if len(clean_end) >= 13 else "09:20"

        try:
            dow = DAYS_UA[datetime(int(y), int(m), int(d)).weekday()]
        except Exception:
            dow = ""

        summary = clean_text(props.get("SUMMARY", ""))
        location = clean_text(props.get("LOCATION", ""))
        desc = props.get("DESCRIPTION", "").replace("\\n", "\n").replace("\\,", ",")

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
            desc_lines = [l.strip() for l in desc.splitlines() if l.strip()]
            if desc_lines:
                teacher = clean_text(desc_lines[0])

        lessons.append({
            "date_str": date_str,
            "day_of_week": dow,
            "lesson_number": lesson_num,
            "time_start": t_start,
            "time_end": t_end,
            "subject": summary or "Заняття",
            "lesson_type": "Лаб" if "Лаб" in summary else "Лек",
            "teacher": teacher,
            "room": location,
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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    connector = aiohttp.TCPConnector(ssl=False)

    # 1. Завантаження через офіційну HTML сторінку розкладу
    try:
        params = {"group": GROUP_ID, "sdate": sdate.strip(), "edate": edate.strip()}
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            async with session.get(BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    parsed_lessons = parse_html_content(html)
                    print(f"[PARSER] Отримано {len(parsed_lessons)} занять з HTML.")
    except Exception as e:
        print(f"[PARSER] Помилка HTML-запиту: {e}")

    # 2. Резервний канал: iCal експорт деканату
    if not parsed_lessons:
        try:
            print("[PARSER] Використовуємо резервний iCal експорт...")
            async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
                async with session.get(ICAL_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        raw_ics = await resp.text()
                        parsed_lessons = parse_ical_content(raw_ics)
                        print(f"[PARSER] Отримано {len(parsed_lessons)} занять з iCal.")
        except Exception as e:
            print(f"[PARSER] Помилка iCal-запиту: {e}")

    if not parsed_lessons:
        print("[PARSER] Не вдалося знайти занять у жодному джерелі.")
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

    print(f"[PARSER] Успішно збережено в БД {len(parsed_lessons)} пар.")
    return True
