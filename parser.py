import re
import requests
from bs4 import BeautifulSoup
from database import get_db_pool

URL = "https://dekanat.nung.edu.ua/cgi-bin/timetable.cgi?n=700"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": "https://dekanat.nung.edu.ua/cgi-bin/timetable.cgi?n=700"
}

def determine_subgroup(text: str) -> int:
    """Визначає підгрупу суто для конкретного фрагмента заняття."""
    low = text.lower()
    # Перевірка 1 підгрупи
    if re.search(r"(?:підгр\.?|п\.?|підгрупа)\s*1\b|\b1\s*(?:підгр|підгрупа|п\.)", low):
        return 1
    # Перевірка 2 підгрупи
    if re.search(r"(?:підгр\.?|п\.?|підгрупа)\s*2\b|\b2\s*(?:підгр|підгрупа|п\.)", low):
        return 2
    return 0

async def fetch_and_save_schedule(sdate: str = "", edate: str = ""):
    group_encoded = "%CA%B2-25-1"
    payload = f"faculty=1015&teacher=&course=0&group={group_encoded}&sdate={sdate.strip()}&edate={edate.strip()}&n=700"

    response = requests.post(URL, data=payload.encode("ascii"), headers=HEADERS, timeout=20)
    html_text = response.content.decode("cp1251", errors="replace")
    
    soup = BeautifulSoup(html_text, "html.parser")
    day_blocks = soup.select(".container .row > div.col-12.col-sm-6")

    pool = await get_db_pool()
    if not pool:
        return

    async with pool.acquire() as conn:
        async with conn.transaction():
            for day_block in day_blocks:
                h4 = day_block.find("h4")
                if not h4:
                    continue

                small_tag = h4.find("small")
                day_of_week = small_tag.get_text(strip=True) if small_tag else ""
                
                raw_date = h4.text.replace(day_of_week, "").strip()
                date_match = re.search(r"\d{2}\.\d{2}\.\d{4}", raw_date)
                date_str = date_match.group(0) if date_match else raw_date

                for row in day_block.select("tbody tr"):
                    cols = row.find_all("td")
                    if len(cols) < 3:
                        continue

                    lesson_num_match = re.search(r"\d+", cols[0].get_text(strip=True))
                    if not lesson_num_match:
                        continue
                    lesson_num = int(lesson_num_match.group(0))

                    times = re.findall(r"\d{1,2}:\d{2}", cols[1].get_text())
                    time_start = times[0] if len(times) > 0 else ""
                    time_end = times[1] if len(times) > 1 else ""

                    cell = cols[2]
                    p_names = cell.find_all("span", class_="p_name")

                    # Якщо span.p_name відсутні, обробляємо комірку як один блок
                    if not p_names:
                        cell_raw = cell.get_text(" ", strip=True)
                        if not cell_raw:
                            continue
                        subgroup = determine_subgroup(cell_raw)
                        await conn.execute("""
                            INSERT INTO schedule (
                                date_str, day_of_week, lesson_number, time_start, time_end,
                                subject, lesson_type, teacher, room, target_subgroup,
                                is_replacement, replacement_note
                            ) VALUES ($1, $2, $3, $4, $5, $6, '', '', '', $7, false, null)
                            ON CONFLICT (date_str, lesson_number, subject, target_subgroup) DO NOTHING
                        """, date_str, day_of_week, lesson_num, time_start, time_end, cell_raw, subgroup)
                        continue

                    # Якщо в одній парі кілька дисциплін/підгруп — збираємо кожен блок ізольовано
                    for p_name_span in p_names:
                        subject = p_name_span.get_text(strip=True)
                        subject = re.sub(r"Увага!\s*Заміна!.*?(замість:.*?)?(?=[А-ЯІЇЄ]|$)", "", subject, flags=re.IGNORECASE).strip()
                        if not subject or "Увага!" in subject:
                            subject = "Дисципліна за розкладом"

                        lesson_type = ""
                        teacher = ""
                        room = ""
                        subgroup_text_acc = []

                        curr = p_name_span.next_sibling
                        while curr:
                            # Якщо дійшли до наступного предмета у цій же клітинці — зупиняємось
                            if getattr(curr, "name", None) == "span" and "p_name" in curr.get("class", []):
                                break

                            if getattr(curr, "name", None) == "span":
                                classes = curr.get("class", [])
                                txt = curr.get_text(strip=True)
                                if "p_type_name" in classes:
                                    lesson_type = txt
                                elif "t_name" in classes:
                                    teacher = txt
                                elif "room_name" in classes:
                                    room = txt
                                elif "gr2_name" in classes:
                                    subgroup_text_acc.append(txt)
                            elif isinstance(curr, str):
                                subgroup_text_acc.append(curr)

                            curr = curr.next_sibling

                        # Визначаємо підгрупу СУВОРО для цієї частини клітинки
                        combined_sub_text = " ".join(subgroup_text_acc)
                        target_subgroup = determine_subgroup(combined_sub_text)

                        # Перевірка на заміну для цього блоку
                        is_replacement = False
                        replacement_note = None
                        if "заміна!" in combined_sub_text.lower() or "заміна!" in subject.lower():
                            is_replacement = True
                            replacement_note = "Увага! Заміна викладача"

                        await conn.execute("""
                            INSERT INTO schedule (
                                date_str, day_of_week, lesson_number, time_start, time_end,
                                subject, lesson_type, teacher, room, target_subgroup,
                                is_replacement, replacement_note
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                            ON CONFLICT (date_str, lesson_number, subject, target_subgroup) 
                            DO UPDATE SET 
                                day_of_week = EXCLUDED.day_of_week,
                                time_start = EXCLUDED.time_start,
                                time_end = EXCLUDED.time_end,
                                lesson_type = EXCLUDED.lesson_type,
                                teacher = EXCLUDED.teacher,
                                room = EXCLUDED.room,
                                is_replacement = EXCLUDED.is_replacement,
                                replacement_note = EXCLUDED.replacement_note
                        """, date_str, day_of_week, lesson_num, time_start, time_end,
                             subject, lesson_type, teacher, room, target_subgroup,
                             is_replacement, replacement_note)
