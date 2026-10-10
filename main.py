import os
import time
import asyncio
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

load_dotenv()

app = FastAPI(title="Stockly Backend", version="0.2.0")

# Zara product-detail API provider. Keep this key on the backend; never put it in Swift code.
ZARA_DETAIL_ENDPOINT = "https://api.reefapi.com/zara/v1/product_detail"
ZARA_MARKET = os.getenv("ZARA_MARKET", "tr")
REEF_API_KEY = os.getenv("REEF_API_KEY", "").strip()
CACHE_SECONDS = 30
_detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class ProductRequest(BaseModel):
    url: str = Field(min_length=8)
    size: str = Field(min_length=1, max_length=30)
    color: Optional[str] = Field(default=None, max_length=80)


class ProductOptionsRequest(BaseModel):
    url: str = Field(min_length=8)


class ProductCheckRequest(BaseModel):
    url: str = Field(min_length=8)
    size: str = Field(min_length=1, max_length=30)
    color: Optional[str] = Field(default=None, max_length=80)


def detect_store(url: str) -> Optional[str]:
    parsed = urlparse(url.strip())
    domain = (parsed.hostname or "").lower().removeprefix("www.")
    stores = {
        "zara.com": "zara",
        "stradivarius.com": "stradivarius",
        "pullandbear.com": "pull_and_bear",
        "hm.com": "hm",
        "mango.com": "mango",
    }
    for website, store_name in stores.items():
        if domain == website or domain.endswith("." + website):
            return store_name
    return None


def get_zara_product_id(url: str) -> Optional[str]:
    try:
        params = parse_qs(urlparse(url).query)
        product_id = params.get("v1", [None])[0]
        return product_id if product_id and product_id.isdigit() else None
    except Exception:
        return None


def validate_product_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Geçerli bir ürün bağlantısı gir.")
    store = detect_store(url)
    if not store:
        raise HTTPException(status_code=400, detail="Bu mağaza Stockly tarafından tanınmıyor.")
    return store


def require_zara_api_key() -> None:
    if not REEF_API_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "Canlı Zara beden/stok verisi için backend .env dosyasına REEF_API_KEY eklenmeli. "
                "Bu anahtar iOS uygulamasına eklenmemeli ve GitHub'a yüklenmemeli."
            ),
        )


async def get_zara_details(url: str) -> dict[str, Any]:
    """Return normalized upstream product details; cache briefly to avoid repeat calls."""
    require_zara_api_key()
    now = time.monotonic()
    cached = _detail_cache.get(url)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]

    headers = {
        "x-api-key": REEF_API_KEY,
        "content-type": "application/json",
        "accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            # ReefAPI documents product_id as the most direct lookup. Zara's v1
            # URL parameter is a colour-level product id, so prefer it when present.
            product_id = get_zara_product_id(url)
            request_body: dict[str, Any] = {"market": ZARA_MARKET}
            if product_id:
                request_body["product_id"] = product_id
            else:
                request_body["url"] = url
            response = await client.post(
                ZARA_DETAIL_ENDPOINT,
                headers=headers,
                json=request_body,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Zara ürün bilgisi zamanında alınamadı; biraz sonra tekrar dene.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Zara ürün bilgi servisine bağlanılamadı.") from exc

    if response.status_code in (401, 403):
        raise HTTPException(status_code=502, detail="Zara ürün bilgi servisi API anahtarını kabul etmedi.")
    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="Ürün bilgi servisinin istek sınırına ulaşıldı; biraz bekle.")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Ürün bilgi servisi hata döndürdü (HTTP {response.status_code}).")

    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Ürün bilgi servisi geçerli JSON döndürmedi.") from exc

    if isinstance(payload, dict) and payload.get("ok") is False:
        error = payload.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("detail") or str(error)
        raise HTTPException(status_code=502, detail=f"Zara ürün verisi alınamadı: {error or 'Bilinmeyen servis hatası'}")

    data = payload.get("data", payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Ürün bilgi servisinden beklenen ürün verisi gelmedi.")

    _detail_cache[url] = (now, data)
    return data


def _iter_dicts(value: Any):
    """Walk nested provider responses without assuming a single envelope shape."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        # Some APIs key variants by id instead of returning an array.
        nested = [item for item in value.values() if isinstance(item, dict)]
        if nested and any("sizes" in item for item in nested):
            return nested
    return []


def _has_size_rows(value: Any) -> bool:
    rows = _as_dict_list(value)
    return bool(rows) and any(
        any(key in row for key in ("name", "size", "label", "sku", "size_id"))
        for row in rows
    )


def _is_color_with_sizes(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and _has_size_rows(value.get("sizes"))
    )


def find_product_container(data: dict[str, Any]) -> dict[str, Any]:
    """Find the actual product node if the provider wraps it in result/product keys."""
    nodes = list(_iter_dicts(data))

    # Prefer a proper all-colours matrix.
    for node in nodes:
        colors = _as_dict_list(node.get("colors"))
        if colors and any(_is_color_with_sizes(color) for color in colors):
            return node

    # Some product-detail responses provide only the requested colour at this level.
    for node in nodes:
        for key in ("selected_color", "selectedColor", "selected_variant", "selectedVariant"):
            selected = node.get(key)
            if _is_color_with_sizes(selected):
                return node

    # Last resort: a direct list of sizes in a nested product object.
    for node in nodes:
        if _has_size_rows(node.get("sizes")):
            return node

    return data


def get_colors(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Read product colourways across the response shapes seen in API envelopes."""
    for key in ("colors", "colorways", "variants"):
        colors = _as_dict_list(data.get(key))
        with_sizes = [color for color in colors if _is_color_with_sizes(color)]
        if with_sizes:
            return with_sizes

    for key in ("selected_color", "selectedColor", "selected_variant", "selectedVariant"):
        selected = data.get(key)
        if _is_color_with_sizes(selected):
            return [selected]

    sizes = data.get("sizes")
    if _has_size_rows(sizes):
        color = data.get("color")
        if not isinstance(color, dict):
            color = {}
        return [{
            "id": data.get("color_id") or color.get("color_id") or color.get("id"),
            "product_id": data.get("product_id") or color.get("product_id"),
            "name": data.get("color_name") or color.get("name") or "Seçili renk",
            "hex": data.get("hex") or color.get("hex"),
            "sizes": _as_dict_list(sizes),
        }]

    return []

def norm(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def availability_for(size: dict[str, Any]) -> tuple[Optional[bool], str]:
    raw = str(size.get("availability") or "unknown").lower()
    in_stock = size.get("in_stock")
    if isinstance(in_stock, bool):
        return in_stock, raw
    if raw in {"in_stock", "low_on_stock", "low stock", "available"}:
        return True, raw
    if raw in {"out_of_stock", "coming_soon", "unavailable"}:
        return False, raw
    return None, raw


def serialize_size(size: dict[str, Any]) -> dict[str, Any]:
    size_name = size.get("name") or size.get("size") or size.get("label")
    if isinstance(size_name, dict):
        size_name = size_name.get("name") or size_name.get("label") or size_name.get("value")
    in_stock, raw = availability_for(size)
    if raw == "unknown" and isinstance(in_stock, bool):
        raw = "in_stock" if in_stock else "out_of_stock"
    return {
        "size": str(size_name) if size_name is not None else None,
        "size_id": size.get("size_id", size.get("id")),
        "sku": size.get("sku"),
        "availability": raw,
        "in_stock": in_stock,
    }


def normalize_options(url: str, data: dict[str, Any]) -> dict[str, Any]:
    container = find_product_container(data)
    colors = get_colors(container)

    # Product metadata may live on a wrapper while variant data lives in product/detail.
    def first_value(key: str):
        if data.get(key) not in (None, "", []):
            return data.get(key)
        if container is not data and container.get(key) not in (None, "", []):
            return container.get(key)
        for node in _iter_dicts(data):
            if node.get(key) not in (None, "", []):
                return node.get(key)
        return None

    selected_product_id = get_zara_product_id(url)
    color_rows = []
    for color in colors:
        product_id = color.get("product_id") or color.get("productId")
        color_meta = color.get("color") if isinstance(color.get("color"), dict) else {}
        sizes = color.get("sizes")
        if not isinstance(sizes, list):
            sizes = _as_dict_list(sizes)
        name = color.get("name") or color.get("color_name") or color_meta.get("name") or "Renk belirtilmemiş"
        color_rows.append({
            "id": str(color.get("id") or color.get("color_id") or color_meta.get("color_id") or ""),
            "product_id": str(product_id) if product_id is not None else None,
            "name": str(name),
            "hex": color.get("hex") or color.get("hex_code") or color_meta.get("hex"),
            "sizes": [serialize_size(s) for s in sizes if isinstance(s, dict)],
        })

    # Prioritize the colour in the URL when the provider includes its id.
    selected_color = next(
        (c for c in color_rows if selected_product_id and c["product_id"] == selected_product_id),
        None,
    )

    raw_price = first_value("price")
    currency = raw_price.get("currency") if isinstance(raw_price, dict) else first_value("currency")
    result = {
        "store": "zara",
        "product_id": first_value("product_id") or selected_product_id,
        "name": first_value("name") or first_value("product_name") or "Zara ürünü",
        "price": raw_price,
        "currency": currency,
        "selected_color": selected_color,
        "colors": color_rows,
        "source": "Zara product-detail feed",
    }
    return result

def pick_color(options: dict[str, Any], requested_color: Optional[str]) -> dict[str, Any]:
    colors = options["colors"]
    if not colors:
        raise HTTPException(status_code=502, detail="Ürün verisinde renk ve beden listesi bulunamadı.")

    if requested_color:
        wanted = norm(requested_color)
        matches = [c for c in colors if wanted in {norm(c["name"]), norm(c["id"]), norm(c["product_id"])}]
        if not matches:
            available_names = [c["name"] for c in colors]
            raise HTTPException(status_code=400, detail={
                "message": "İstenen renk bu üründe bulunamadı.",
                "available_colors": available_names,
            })
        return matches[0]

    selected = options.get("selected_color")
    if selected:
        return selected
    if len(colors) == 1:
        return colors[0]

    raise HTTPException(status_code=400, detail={
        "message": "Bu üründe birden fazla renk var. Stok kontrolü için renk de seçilmeli.",
        "available_colors": [c["name"] for c in colors],
    })


@app.get("/")
def home():
    return {
        "message": "Stockly Backend çalışıyor!",
        "version": "0.2.0",
        "endpoints": ["/product", "/product/options", "/product/check", "/docs"],
    }


@app.get("/stores")
def stores():
    return {
        "stores": [
            {"id": "zara", "name": "Zara", "options_and_live_stock": True},
            {"id": "stradivarius", "name": "Stradivarius", "options_and_live_stock": False},
            {"id": "pull_and_bear", "name": "Pull&Bear", "options_and_live_stock": False},
            {"id": "hm", "name": "H&M", "options_and_live_stock": False},
            {"id": "mango", "name": "Mango", "options_and_live_stock": False},
        ],
        "note": "Şu sürümde beden + canlı stok verisi Zara için etkinleştirildi; diğer mağazalar yalnızca tanınıyor.",
    }


@app.post("/product")
def add_product(product: ProductRequest):
    """Backward-compatible route: keeps the existing iOS app's add-product request working."""
    store = validate_product_url(product.url)
    if store == "zara":
        product_id = get_zara_product_id(product.url)
    else:
        product_id = None
    return {
        "url": product.url,
        "size": product.size,
        "color": product.color,
        "product_id": product_id,
        "store": store,
        "status": "received",
        "options_endpoint": "/product/options?url=<URL-ENCODED-PRODUCT-URL>",
        "check_endpoint": "/product/check",
    }


@app.get("/product/options")
async def product_options(url: str = Query(..., min_length=8)):
    store = validate_product_url(url)
    if store != "zara":
        raise HTTPException(
            status_code=501,
            detail=f"{store} bağlantısı tanındı ancak o mağazanın beden/stok entegrasyonu henüz eklenmedi.",
        )
    data = await get_zara_details(url)
    options = normalize_options(url, data)
    if not options["colors"]:
        # Report response structure only, never API credentials or full product payload.
        key_summary = []
        for node in _iter_dicts(data):
            keys = sorted(str(key) for key in node.keys())
            if keys:
                key_summary.append(keys[:18])
            if len(key_summary) >= 6:
                break
        raise HTTPException(status_code=502, detail={
            "message": "Ürün detay servisi yanıt verdi ancak beden listesi beklenen alanlarda bulunamadı.",
            "response_keys": key_summary,
        })
    return options


@app.post("/product/check")
async def check_product(product: ProductCheckRequest):
    store = validate_product_url(product.url)
    if store != "zara":
        raise HTTPException(
            status_code=501,
            detail=f"{store} bağlantısı tanındı ancak o mağazanın canlı stok entegrasyonu henüz eklenmedi.",
        )
    data = await get_zara_details(product.url)
    options = normalize_options(product.url, data)
    color = pick_color(options, product.color)
    wanted_size = norm(product.size)
    matched = [s for s in color["sizes"] if norm(s.get("size")) == wanted_size]
    if not matched:
        raise HTTPException(status_code=400, detail={
            "message": f"'{product.size}' bedeni/numarası bu renkte bulunamadı.",
            "available_sizes": [s["size"] for s in color["sizes"] if s.get("size")],
            "color": color["name"],
        })

    stock = matched[0]
    return {
        "url": product.url,
        "store": "zara",
        "product_id": options["product_id"],
        "product_name": options["name"],
        "color": color["name"],
        "size": stock["size"],
        "sku": stock["sku"],
        "availability": stock["availability"],
        "in_stock": stock["in_stock"],
        "status": "checked" if stock["in_stock"] is not None else "unknown",
    }


# ---------------------------------------------------------------------------
# Server-side watch list and Telegram alerts.
# The Mac/backend must stay running and awake for these checks to continue.
# ---------------------------------------------------------------------------

WATCH_DB_PATH = Path(
    os.getenv("STOCKLY_DB_PATH", str(Path(__file__).with_name("stockly_watchlist.sqlite3")))
)
WATCH_INTERVAL_SECONDS = max(300, int(os.getenv("STOCKLY_POLL_SECONDS", "600")))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
_watch_task: Optional[asyncio.Task] = None


class WatchCreateRequest(BaseModel):
    url: str = Field(min_length=8)
    size: str = Field(min_length=1, max_length=30)
    color: str = Field(min_length=1, max_length=80)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _watch_db() -> sqlite3.Connection:
    WATCH_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(WATCH_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _init_watch_db() -> None:
    with _watch_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_watches (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                size TEXT NOT NULL,
                color TEXT NOT NULL,
                product_name TEXT,
                last_in_stock INTEGER,
                availability TEXT NOT NULL DEFAULT 'unknown',
                last_checked TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(url, size, color)
            )
            """
        )
        db.commit()


def _row_to_watch(row: sqlite3.Row) -> dict[str, Any]:
    last_in_stock = row["last_in_stock"]
    return {
        "id": row["id"],
        "url": row["url"],
        "size": row["size"],
        "color": row["color"],
        "product_name": row["product_name"],
        "in_stock": None if last_in_stock is None else bool(last_in_stock),
        "availability": row["availability"],
        "last_checked": row["last_checked"],
        "created_at": row["created_at"],
    }


async def _send_telegram_message(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(
            "Stockly: stok değişimi algılandı ancak Telegram ayarları eksik. "
            "TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID değerlerini .env dosyasına ekle."
        )
        return False

    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                telegram_url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            )
        if response.status_code >= 400:
            print(f"Stockly: Telegram bildirimi gönderilemedi (HTTP {response.status_code}).")
            return False
        result = response.json()
        if not result.get("ok", False):
            print("Stockly: Telegram API bildirim isteğini kabul etmedi.")
            return False
        return True
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Stockly: Telegram bildirimi gönderilemedi ({type(exc).__name__}).")
        return False


async def _get_live_watch_status(watch: dict[str, Any]) -> dict[str, Any]:
    """Use the same tested product-check logic as the iOS app."""
    return await check_product(
        ProductCheckRequest(
            url=watch["url"],
            size=watch["size"],
            color=watch["color"],
        )
    )


async def _poll_watchlist_once() -> None:
    _init_watch_db()
    with _watch_db() as db:
        rows = db.execute(
            "SELECT * FROM stock_watches ORDER BY created_at ASC"
        ).fetchall()
    for row in rows:
        watch = dict(row)
        try:
            result = await _get_live_watch_status(watch)
            current = result.get("in_stock")
            if not isinstance(current, bool):
                with _watch_db() as db:
                    db.execute(
                        "UPDATE stock_watches SET availability=?, last_checked=? WHERE id=?",
                        (str(result.get("availability") or "unknown"), _utc_now(), watch["id"]),
                    )
                    db.commit()
                continue

            previous_value = watch.get("last_in_stock")
            previous = None if previous_value is None else bool(previous_value)

            # Notify only on a confirmed transition from unavailable to available.
            if previous is False and current is True:
                message = (
                    "🎉 Stockly: Takip ettiğin ürün yeniden stokta!\n"
                    f"Ürün: {result.get('product_name') or watch.get('product_name') or 'Zara ürünü'}\n"
                    f"Renk: {result.get('color') or watch['color']}\n"
                    f"Beden/numara: {result.get('size') or watch['size']}\n"
                    f"Stok durumu: {result.get('availability') or 'in_stock'}\n"
                    f"{watch['url']}"
                )
                await _send_telegram_message(message)

            with _watch_db() as db:
                db.execute(
                    """
                    UPDATE stock_watches
                    SET last_in_stock=?, availability=?, last_checked=?, product_name=?
                    WHERE id=?
                    """,
                    (
                        1 if current else 0,
                        str(result.get("availability") or "unknown"),
                        _utc_now(),
                        result.get("product_name") or watch.get("product_name"),
                        watch["id"],
                    ),
                )
                db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not delete a watch because a temporary provider/network request failed.
            print(
                f"Stockly: stok kontrolü başarısız (watch={watch.get('id')}, "
                f"error={type(exc).__name__})."
            )


async def _watchlist_monitor() -> None:
    while True:
        try:
            await _poll_watchlist_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Stockly: izleme döngüsü hatası ({type(exc).__name__}).")
        await asyncio.sleep(WATCH_INTERVAL_SECONDS)


@app.on_event("startup")
async def start_watchlist_monitor() -> None:
    global _watch_task
    _init_watch_db()
    if _watch_task is None or _watch_task.done():
        _watch_task = asyncio.create_task(_watchlist_monitor())


@app.on_event("shutdown")
async def stop_watchlist_monitor() -> None:
    global _watch_task
    if _watch_task is not None:
        _watch_task.cancel()
        try:
            await _watch_task
        except asyncio.CancelledError:
            pass
        _watch_task = None


@app.post("/watch")
async def create_watch(item: WatchCreateRequest):
    """Save a product/colour/size for periodic server-side stock checks."""
    store = validate_product_url(item.url)
    if store != "zara":
        raise HTTPException(
            status_code=501,
            detail="Otomatik stok takibi şu an yalnızca Zara için etkin.",
        )

    # Validate the size/colour against the live provider before saving it.
    result = await _get_live_watch_status(item.model_dump())
    initial_stock = result.get("in_stock")
    initial_int = None if not isinstance(initial_stock, bool) else int(initial_stock)
    now = _utc_now()
    candidate_id = str(uuid.uuid4())

    with _watch_db() as db:
        db.execute(
            """
            INSERT INTO stock_watches
                (id, url, size, color, product_name, last_in_stock, availability, last_checked, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url, size, color) DO NOTHING
            """,
            (
                candidate_id,
                item.url,
                item.size,
                item.color,
                result.get("product_name"),
                initial_int,
                str(result.get("availability") or "unknown"),
                now,
                now,
            ),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM stock_watches WHERE url=? AND size=? AND color=?",
            (item.url, item.size, item.color),
        ).fetchone()

    return {
        "status": "watching",
        "watch": _row_to_watch(row),
        "current_check": result,
        "poll_interval_seconds": WATCH_INTERVAL_SECONDS,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "note": "Takip, bu backend çalıştığı ve Mac uykuya geçmediği sürece devam eder.",
    }


@app.get("/watch")
def list_watches():
    _init_watch_db()
    with _watch_db() as db:
        rows = db.execute(
            "SELECT * FROM stock_watches ORDER BY created_at DESC"
        ).fetchall()
    return {
        "watches": [_row_to_watch(row) for row in rows],
        "poll_interval_seconds": WATCH_INTERVAL_SECONDS,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    }


@app.delete("/watch/{watch_id}")
def delete_watch(watch_id: str):
    _init_watch_db()
    with _watch_db() as db:
        cursor = db.execute("DELETE FROM stock_watches WHERE id=?", (watch_id,))
        db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Takip kaydı bulunamadı.")
    return {"status": "deleted", "id": watch_id}


@app.get("/notifications/status")
def notification_status():
    return {
        "channel": "telegram",
        "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "poll_interval_seconds": WATCH_INTERVAL_SECONDS,
        "message": (
            "Telegram bildirimi hazır."
            if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
            else "Telegram bot token ve chat ID .env dosyasına eklenmeli."
        ),
    }



@app.post("/notifications/test")
async def test_telegram_notification():
    success = await _send_telegram_message(
        "✅ Stockly test bildirimi: Telegram bağlantısı çalışıyor!"
    )

    if not success:
        raise HTTPException(
            status_code=502,
            detail="Telegram mesajı gönderilemedi. Terminal çıktısını kontrol et."
        )

    return {"status": "sent", "message": "Test bildirimi gönderildi."}



@app.post("/notifications/test-restock")
async def test_restock_notification():
    # Gerçek veritabanına dokunmadan stok değişimini simüle et.
    previous = False
    current = True

    if previous is False and current is True:
        success = await _send_telegram_message(
            "🎉 Stockly test: Takip ettiğin ürün yeniden stokta!\n"
            "Bu bir test bildirimidir; gerçek stok durumun değiştirilmedi."
        )

        if not success:
            raise HTTPException(
                status_code=502,
                detail="Telegram bildirimi gönderilemedi. Terminal çıktısını kontrol et."
            )

        return {
            "status": "sent",
            "simulated_transition": "out_of_stock_to_in_stock",
            "database_changed": False
        }

    return {"status": "no_notification"}

