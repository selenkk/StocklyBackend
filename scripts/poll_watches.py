import asyncio
import json
import os
from pathlib import Path

import httpx
import main

WATCHES_PATH = Path("watches.json")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


async def send_telegram(message: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID GitHub Secrets içinde eksik.")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": message},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError("Telegram API mesajı kabul etmedi.")


async def main_async():
    if not WATCHES_PATH.exists():
        raise RuntimeError("watches.json dosyası bulunamadı.")

    watches = json.loads(WATCHES_PATH.read_text(encoding="utf-8"))
    changed = False

    for watch in watches:
        try:
            result = await main.check_product(
                main.ProductCheckRequest(
                    url=watch["url"],
                    size=watch["size"],
                    color=watch["color"],
                )
            )
            current = result.get("in_stock")
            if not isinstance(current, bool):
                print(f"Stok bilinmiyor: {watch['url']} / {watch['size']}")
                continue

            previous = watch.get("in_stock")
            # İlk başarılı kontrol sadece başlangıç durumunu kaydeder.
            if previous is False and current is True:
                message = (
                    "🎉 Stockly: Takip ettiğin ürün yeniden stokta!\n"
                    f"Ürün: {result.get('product_name') or 'Zara ürünü'}\n"
                    f"Renk: {result.get('color') or watch['color']}\n"
                    f"Beden/numara: {result.get('size') or watch['size']}\n"
                    f"Stok durumu: {result.get('availability') or 'in_stock'}\n"
                    f"{watch['url']}"
                )
                await send_telegram(message)
                print(f"Telegram bildirimi gönderildi: {watch['url']} / {watch['size']}")

            if watch.get("in_stock") != current:
                watch["in_stock"] = current
                changed = True
            watch["availability"] = result.get("availability") or "unknown"
            watch["last_checked"] = result.get("status") and __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(timespec="seconds")
            watch["product_name"] = result.get("product_name") or watch.get("product_name")
            changed = True
            print(f"Kontrol edildi: {watch['url']} / {watch['color']} / {watch['size']} => {current}")

        except Exception as exc:
            # Continue checking other products; fail this run visibly in logs.
            print(f"Kontrol başarısız ({watch.get('url', 'URL bilinmiyor')}): {type(exc).__name__}: {exc}")

    WATCHES_PATH.write_text(json.dumps(watches, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main_async())
