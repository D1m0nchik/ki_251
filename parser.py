import re
import aiohttp
from datetime import datetime
from database import get_db_pool

GROUP_ID = -2281  # OBJ_ID для групи КІ-25-1
ICAL_URL = "https://dekanat.nung.edu.ua/cgi-bin/timetable_export.cgi?req_type=rozklad&req_mode=group&OBJ_ID=-2281&req_format=iCal&coding_mode=UTF8&bs=ok&fk=1"

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

async def fetch_and_save_schedule(sdate: str = "", edate: str = "") -> bool:
    """
    Завантажує офіційний розклад через iCal експорт деканату ІФНТУНГ
    і синхронізує його з базою даних.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(ICAL_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    print(f"Помилка завантаження iCal: статус {resp.status}")
                    return False
                raw_ics = await resp.text()
    except Exception as e:
        print("Помилка з'єднання з iCal сервером:", e)
        return False

    events = raw_ics.split("BEGIN:VEVENT")
    if len(events) <= 1:
        return False

    parsed_lessons = []
    days_ua = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]

    for ev in events[1:]:
        dtstart_m = re.search(r'DTSTART(?:;[^:]+)?:(\d{8}T\d{4})', ev)
        dtend_m = re.search(r'DTEND(?:;[^:]+)?:(\d{8}T\d{4})', ev)
        summary_m = re.search(r'SUMMARY:(.*?)(?:\r?\n[A-Z]|\r?\n$)', ev, re.S)
        location_m = re.search(r'LOCATION:(.*?)(?:\r?\n[A-Z]|\r?\n$)', ev, re.S)
        desc_m = re.search(r'DESCRIPTION:(.*?)(?:\r?\n[A-Z]|\r?\n$)', ev, re.S)

        if not dtstart_m:
            continue

        raw_dt = dtstart_m.group(1)
        year = raw_dt[0:4]
        month = raw_dt[4:6]
        day = raw_dt[6:8]
        time_start = f"{raw_dt[9:11]}:{raw_dt[11:13]}"
        
        time_end = ""
        if dtend_m:
            raw_end = dtend_m.group(1)
            time_end = f"{raw_end[9:11]}:{raw_end[11:13]}"

        date_str = f"{day}.{month}.{year}"
        try:
            dt_obj = datetime(int(year), int(month), int(day))
            day_of_week = days_ua[dt_obj.weekday()]
        except Exception:
            day_of_week = ""

        summary = summary_m.group(1).replace("\r", "").replace("\n", " ").strip() if summary_m else ""
        room = location_m.group(1).replace("\r", "").replace("\n", " ").strip() if location_m else ""
        desc = desc_m.group(1).replace("\\n", "\n").replace("\r", "").strip() if desc_m else ""

        lesson_num = 1
        if time_start.startswith("08:"): lesson_num = 1
        elif time_start.startswith("09:"): lesson_num = 2
        elif time_start.startswith("11:"): lesson_num = 3
        elif time_start.startswith("12:") or time_start.startswith("13:"): lesson_num = 4
        elif time_start.startswith("14:"): lesson_num = 5
        elif time_start.startswith("15:") or time_start.startswith("16:"): lesson_num = 6
        elif time_start.startswith("17:"): lesson_num = 7

        meet_url = extract_meet_url(desc) or extract_meet_url(summary)

        target_sg = 0
        if "підгр. 1" in summary.lower() or "1 підгр" in summary.lower(): 
            target_sg = 1
        elif "підгр. 2" in summary.lower() or "2 підгр" in summary.lower(): 
            target_sg = 2

        elective_num = 1 if (summary.startswith("*") or "вибіркова" in summary.lower()) else 0

        teacher = ""
        if desc:
            lines = [l.strip() for l in desc.split("\n") if l.strip()]
            if lines:
                teacher = lines[0]

        parsed_lessons.append({
            "date_str": date_str,
            "day_of_week": day_of_week,
            "lesson_number": lesson_num,
            "time_start": time_start,
            "time_end": time_end or "09:20",
            "subject": summary or "Заняття",
            "lesson_type": "Лаб" if "Лаб" in summary else "Лек",
            "teacher": teacher,
            "room": room,
            "target_subgroup": target_sg,
            "meet_url": meet_url,
            "elective_num": elective_num
        })

    pool = await get_db_pool()
    if not pool or not parsed_lessons:
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

    return True
