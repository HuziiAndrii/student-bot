"""
diag.py — швидка діагностика автопошуку розкладу. Запуск із папки бота:  python3 diag.py
1) показує, який текст бот бере зі сторінки розкладу;
2) показує моделі Gemini, доступні твоєму ключу;
3) міряє, за скільки секунд кожна відповідає на найпростіший запит.
"""
import json
import os
import time
import urllib.request

from dotenv import load_dotenv

load_dotenv()

import schedule_import as si  # noqa: E402

GROUP = "КІ-108"
KEY = os.environ["GEMINI_API_KEY"]
BASE = "https://generativelanguage.googleapis.com/v1beta"


def api(path: str, body: dict | None = None, timeout: int = 40) -> dict:
    request = urllib.request.Request(
        f"{BASE}/{path}", method="POST" if body else "GET",
        data=json.dumps(body).encode() if body else None,
        headers={"x-goog-api-key": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.load(resp)


print("═" * 60, "\n1) ТЕКСТ СТОРІНКИ РОЗКЛАДУ\n" + "═" * 60)
url = si.auto_schedule_url("Львівська політехніка", GROUP)
print("URL:", url)
try:
    parts = si.fetch_url(url)
    text = parts[0][1] if parts[0][0] == "text" else f"<{parts[0][0]}>"
    print(f"Довжина: {len(text)} символів. Початок:\n")
    print(text[:1500])
except Exception as exc:  # noqa: BLE001
    print("Помилка завантаження:", exc)

print("\n" + "═" * 60, "\n2) МОДЕЛІ GEMINI, ДОСТУПНІ ТВОЄМУ КЛЮЧУ\n" + "═" * 60)
candidates = []
try:
    for m in api("models?pageSize=200").get("models", []):
        if "generateContent" in m.get("supportedGenerationMethods", []):
            name = m["name"].removeprefix("models/")
            if "flash" in name and not any(x in name for x in ("image", "tts", "live", "audio")):
                candidates.append(name)
    print("\n".join(candidates) or "(нічого не знайдено)")
except Exception as exc:  # noqa: BLE001
    print("Не вдалося отримати список:", exc)

print("\n" + "═" * 60, "\n3) ШВИДКІСТЬ ВІДПОВІДІ (запит «Скажи: ок»)\n" + "═" * 60)
current = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
for model in dict.fromkeys([current, *candidates[:6]]):
    started = time.monotonic()
    try:
        data = api(f"models/{model}:generateContent",
                   {"contents": [{"parts": [{"text": "Скажи: ок"}]}]}, timeout=40)
        answer = data["candidates"][0]["content"]["parts"][-1].get("text", "").strip()
        print(f"{model:40s} {time.monotonic() - started:5.1f} с  → {answer[:30]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"{model:40s} {time.monotonic() - started:5.1f} с  ✗ {str(exc)[:60]}")
