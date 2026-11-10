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

    # 7. Вибіркова дисципліна
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

async def fetch_and_save_schedule(sdate: str = "", edate: str = "") -> bool:
    if not sdate or not edate:
        now_dt = datetime.now(KYIV_TZ)
        sdate = (now_dt - timedelta(days=now_dt.weekday())).strftime("%d.%m.%Y")
        edate = (now_dt + timedelta(days=21)).strftime("%d.%m.%Y")

    params = {"group": GROUP_ID, "sdate": sdate.strip(), "edate": edate.strip()}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    connector = aiohttp.TCPConnector(ssl=False)

    try:
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            async with session.get(BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status != 200:
                    print(f"[PARSER] Помилка HTTP {resp.status}")
                    return False
                html = await resp.text()
    except Exception as e:
        print(f"[PARSER] Помилка з'єднання: {e}")
        return False

    soup = BeautifulSoup(html, "html.parser")
    parsed_lessons = []

    # 1. Послідовний пошук усіх дат на сторінці
    current_date = ""
    current_dow = ""

    # Шукаємо всі рядки та блоки
    all_elements = soup.find_all(['h1', 'h2', 'h3', 'h4', 'div', 'tr', 'p', 'th', 'td'])
    for el in all_elements:
        txt = clean_text(el.get_text(" ", strip=True))
        d_m = DATE_REGEX.search(txt)
        if d_m and len(txt) < 100:
            current_date = d_m.group(1)
            dow_m = DAYS_REGEX.search(txt)
            if dow_m:
                current_dow = dow_m.group(1).capitalize()
            else:
                try:
                    current_dow = DAYS_UA[datetime.strptime(current_date, "%d.%m.%Y").weekday()]
                except Exception:
                    current_dow = ""

    # Шукаємо рядки занять
    rows = soup.find_all('tr')
    if not rows or len(rows) < 3:
        rows = soup.find_all(['div', 'p'])

    active_date = ""
    active_dow = ""

    for item in rows:
        raw_text = clean_text(item.get_text(" ", strip=True))
        if not raw_text:
            continue

        # Якщо рядок містить дату — оновлюємо поточний день
        d_m = DATE_REGEX.search(raw_text)
        dow_m = DAYS_REGEX.search(raw_text)
        p_m = PAIR_REGEX.search(raw_text)

        if d_m and (dow_m or not p_m):
            active_date = d_m.group(1)
            if dow_m:
                active_dow = dow_m.group(1).capitalize()
            else:
                try:
                    active_dow = DAYS_UA[datetime.strptime(active_date, "%d.%m.%Y").weekday()]
                except Exception:
                    active_dow = ""

        # Якщо рядок містить пару
        if p_m:
            lesson_num = int(p_m.group(1))

            # Перевіряємо дату в батьківських контейнерах, якщо в рядку її немає
            item_date = active_date
            item_dow = active_dow
            if not item_date and item.parent:
                p_text = clean_text(item.parent.get_text(" ", strip=True))
                pd_m = DATE_REGEX.search(p_text)
                if pd_m:
                    item_date = pd_m.group(1)
                    pdow_m = DAYS_REGEX.search(p_text)
                    if pdow_m:
                        item_dow = pdow_m.group(1).capitalize()
                    else:
                        try:
                            item_dow = DAYS_UA[datetime.strptime(item_date, "%d.%m.%Y").weekday()]
                        except Exception:
                            pass

            if not item_date:
                item_date = current_date
                item_dow = current_dow

            if not item_date:
                continue

            meet_url = ""
            for a in item.find_all('a', href=True):
                f_meet = extract_meet_url(a['href'])
                if f_meet:
                    meet_url = f_meet
                    break

            # Розділяємо склеєні підгрупи на окремі записи
            segments = split_multi_subgroup_text(raw_text)
            for seg in segments:
                lesson_data = parse_lesson_segment(seg, item_date, item_dow, lesson_num, default_meet=meet_url)
                parsed_lessons.append(lesson_data)

    if not parsed_lessons:
        print("[PARSER] Не вдалося знайти занять у розкладі.")
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
