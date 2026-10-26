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

async def fetch_and_save_schedule(sdate: str = "", edate: str = ""):
    payload = f"faculty=1015&teacher=&course=0&group=%CA%B2-25-1&sdate={sdate}&edate={edate}&n=700"
    response = requests.post(URL, data=payload.encode('ascii'), headers=HEADERS)
    html_text = response.content.decode('cp1251', errors='replace')
    
    soup = BeautifulSoup(html_text, 'html.parser')
    day_blocks = soup.select('.container .row > div.col-12.col-sm-6')

    pool = await get_db_pool()
    if not pool:
        print("Помилка: Немає підключення до бази даних.")
        return

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Очищаємо попередній розклад
            await conn.execute("DELETE FROM schedule")

            for day_block in day_blocks:
                h4 = day_block.find('h4')
                if not h4:
                    continue

                small_tag = h4.find('small')
                day_of_week = small_tag.get_text(strip=True) if small_tag else ""
                date_str = h4.text.replace(day_of_week, '').strip()

                for row in day_block.select('tbody tr'):
                    cols = row.find_all('td')
                    if len(cols) < 3:
                        continue

                    lesson_num = int(cols[0].get_text(strip=True))
                    times = re.findall(r"\d{1,2}:\d{2}", cols[1].get_text())
                    time_start = times[0] if len(times) > 0 else ""
                    time_end = times[1] if len(times) > 1 else ""

                    cell = cols[2]
                    cell_raw_text = cell.get_text(" ", strip=True)
                    if not cell_raw_text:
                        continue

                    # 1. Перевірка на заміну
                    is_replacement = False
                    replacement_note = None
                    rep_match = None
                    if "заміна!" in cell_raw_text.lower():
                        is_replacement = True
                        rep_match = re.search(
                            r'Увага!\s*Заміна!\s*(.*?)\s+замість:\s*(.*?)(?=\s+[А-ЯІЇЄ]|\s*Вища|\s*Об`|\s*Системне|\s*$)', 
                            cell_raw_text, 
                            re.IGNORECASE
                        )
                        if rep_match:
                            replacement_note = f"Заміна: {rep_match.group(1).strip()} замість {rep_match.group(2).strip()}"
                        else:
                            replacement_note = "Увага! Зафіксовано заміну!"

                    # 2. Витягуємо предмети
                    p_names = cell.find_all('span', class_='p_name')
                    if not p_names:
                        p_names = [cell]

                    for p_name_span in p_names:
                        subject = p_name_span.get_text(strip=True) if hasattr(p_name_span, 'get_text') else str(p_name_span)
                        subject = re.sub(r'Увага!\s*Заміна!.*?(замість:.*?)?(?=[А-ЯІЇЄ]|$)', '', subject, flags=re.IGNORECASE).strip()
                        if not subject:
                            subject = "Дисципліна за розкладом"

                        curr = p_name_span
                        lesson_type = ""
                        subgroup_info = ""
                        teacher = ""
                        room = ""

                        while curr and hasattr(curr, 'next_sibling') and curr.next_sibling:
                            curr = curr.next_sibling
                            if getattr(curr, 'name', None) == 'span':
                                classes = curr.get('class', [])
                                if 'p_name' in classes:
                                    break
                                elif 'p_type_name' in classes:
                                    lesson_type = curr.get_text(strip=True)
                                elif 'gr2_name' in classes:
                                    subgroup_info = curr.get_text(strip=True)
                                elif 't_name' in classes:
                                    teacher = curr.get_text(strip=True)
                                elif 'room_name' in classes:
                                    room = curr.get_text(strip=True)

                        if is_replacement and rep_match and not teacher:
                            teacher = rep_match.group(1).strip()

                        target_subgroup = 0
                        full_check = f"{subgroup_info} {cell_raw_text}"
                        if re.search(r'\(?підгр\.?\s*1\)?', full_check, re.IGNORECASE):
                            target_subgroup = 1
                        elif re.search(r'\(?підгр\.?\s*2\)?', full_check, re.IGNORECASE):
                            target_subgroup = 2

                        await conn.execute("""
                            INSERT INTO schedule (
                                date_str, day_of_week, lesson_number, time_start, time_end,
                                subject, lesson_type, teacher, room, target_subgroup,
                                is_replacement, replacement_note
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                        """, date_str, day_of_week, lesson_num, time_start, time_end,
                             subject, lesson_type, teacher, room, target_subgroup,
                             is_replacement, replacement_note)
