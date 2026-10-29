"""
diag_html.py — чому з сайту розкладу приходить усе, окрім пар.
Запуск із папки бота:  python3 diag_html.py [КІ-108]
Створює page_raw.html (сирий HTML) і page_text.txt (те, що бачить бот) — їх можна надіслати для аналізу.
"""
import re
import sys
import urllib.parse
import urllib.request

import schedule_import as si

GROUP = sys.argv[1] if len(sys.argv) > 1 else "КІ-108"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
      "Accept-Language": "uk,en;q=0.8"}
MARKERS = ["Лекція", "Практична", "Лабораторна", "ауд", "08:30", "10:10", "Понеділок", "Вівторок"]


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.geturl(), r.status, r.headers.get_content_type(), r.read().decode("utf-8", "replace")


def report(label, url):
    print("\n" + "═" * 70 + f"\n{label}\n{url}\n" + "═" * 70)
    try:
        final, status, ctype, html = get(url)
    except Exception as exc:  # noqa: BLE001
        print("Помилка:", exc)
        return None
    text = si.html_to_text(html)
    print(f"HTTP {status}, {ctype}, HTML {len(html)} байт, після html_to_text {len(text)} символів")
    if final != url:
        print("Редирект на:", final)
    print("Маркери пар у СИРОМУ HTML :", {m: html.count(m) for m in MARKERS if html.count(m)})
    print("Маркери пар у ТЕКСТІ бота :", {m: text.count(m) for m in MARKERS if text.count(m)})
    print("\nПоля форми (name → тип), які сайт очікує:")
    for tag in re.findall(r"<(?:input|select)\b[^>]*>", html, re.I):
        name = re.search(r'name="([^"]+)"', tag)
        if name:
            print("  ", name.group(1), "←", re.match(r"<(\w+)", tag).group(1))
    print("\nСкрипти / запити, що можуть підвантажувати пари:")
    for s in sorted(set(re.findall(r'(?:src|data-url|action)="([^"]+)"', html)))[:25]:
        print("  ", s)
    for s in sorted(set(re.findall(r"(?:fetch|ajax|\.get|\.post)\(\s*['\"]([^'\"]+)", html)))[:15]:
        print("  JS:", s)
    return html, text


base = si.auto_schedule_url("Львівська політехніка", GROUP)
res = report("1) URL, який зараз використовує бот", base)
if res:
    open("page_raw.html", "w", encoding="utf-8").write(res[0])
    open("page_text.txt", "w", encoding="utf-8").write(res[1])
    print("\nЗбережено page_raw.html і page_text.txt")
    print("\n--- початок тексту, який бачить бот (1200 символів) ---")
    print(res[1][:1200])

# Варіанти URL: з інститутом і без дублювання параметрів — щоб знайти той, що повертає пари
g = urllib.parse.quote(si.to_cyrillic_group(GROUP))
for label, url in [
    ("2) без semesterduration", f"https://student.lpnu.ua/students_schedule?studygroup_abbrevation={g}&semester=1"),
    ("3) з ІКНІ (departmentparent_abbrevation)",
     f"https://student.lpnu.ua/students_schedule?departmentparent_abbrevation="
     f"{urllib.parse.quote('ІКНІ')}&studygroup_abbrevation={g}&semester=1&semesterduration=1"),
]:
    report(label, url)
