# debug_sports.py
import json
from datetime import datetime, timedelta, timezone
import requests
from config import COOKIES, CSRF_TOKEN

API_BASE = "https://sport.innopolis.university/api"
s = requests.Session()
s.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, */*",
    "Referer": "https://sport.innopolis.university/profile/",
    "X-CSRFToken": CSRF_TOKEN,
})
s.cookies.update(COOKIES)

start = datetime.now(timezone.utc)
end = start + timedelta(days=7)
r = s.get(f"{API_BASE}/calendar/trainings",
          params={"start": start.isoformat(), "end": end.isoformat()})

print("STATUS:", r.status_code)
data = r.json()
print("Кол-во тренировок:", len(data))
if data:
    print("\n=== ПЕРВАЯ ТРЕНИРОВКА (полностью) ===")
    print(json.dumps(data[0], indent=2, ensure_ascii=False))
    print("\n=== ВСЕ КЛЮЧИ, которые встречаются ===")
    def keys(o, prefix=""):
        if isinstance(o, dict):
            for k, v in o.items():
                print(f"{prefix}{k} = {type(v).__name__}")
                keys(v, prefix + "  ")
    keys(data[0])