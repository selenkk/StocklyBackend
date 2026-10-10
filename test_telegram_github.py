
import os
import httpx

token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

if not token or not chat_id:
    raise RuntimeError("Telegram GitHub Secrets eksik.")

response = httpx.post(
    f"https://api.telegram.org/bot{token}/sendMessage",
    json={
        "chat_id": chat_id,
        "text": "✅ Stockly GitHub testi başarılı! Telegram bildirimi çalışıyor."
    },
    timeout=15,
)

response.raise_for_status()
result = response.json()

if not result.get("ok"):
    raise RuntimeError("Telegram mesajı gönderilemedi.")

print("Telegram test bildirimi gönderildi.")
