#!/usr/bin/env python3
"""
MBVK Árverési Monitor v6 – API first, minden adat a JSON-ból
"""

import os
import re
import sys
import time
import sqlite3
import logging
import unicodedata
import urllib.parse
from datetime import datetime
from typing import Optional, List, Dict, Any

import requests

# ── Konfiguráció ──────────────────────────────────────────────────────────────
BASE_URL      = "https://arveres.mbvk.hu"
API_BASE      = "https://arveres.mbvk.hu/publicapi"
DB_PATH       = "mbvk_v10.db"
MAX_PRICE     = 1_000_000          # Maximum ár (Ft)
COUNTIES      = []                 # Üres lista = minden megye, pl. ["Békés", "Csongrád"]

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── SQLite ────────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS properties (
            auction_id TEXT PRIMARY KEY,
            created    TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

def is_new(conn, auction_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM properties WHERE auction_id = ?", (auction_id,)
    ).fetchone() is None

def mark_seen(conn, auction_id: str):
    conn.execute(
        "INSERT OR IGNORE INTO properties (auction_id, created) VALUES (?, ?)",
        (auction_id, datetime.utcnow().isoformat()),
    )
    conn.commit()

# ── Segédfüggvények ───────────────────────────────────────────────────────────
def normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))

def parse_price(val) -> Optional[int]:
    if val is None:
        return None
    digits = re.sub(r"[^\d]", "", str(val))
    return int(digits) if digits else None

def extract_area_from_description(desc: str) -> Optional[float]:
    """Kinyeri a telekméretet (m²) a leírásból."""
    if not desc:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m[²2]", desc, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except:
            pass
    return None

# ── API hívások ───────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://arveres.mbvk.hu/",
}

def api_list(session: requests.Session, offset=0, limit=100) -> List[Dict]:
    url = (f"{API_BASE}/auction/list"
           f"?offset={offset}&limit={limit}"
           f"&sortMod=feltolt&sortDirection=desc"
           f"&phaseCode=normal_ingatlan_2021&isLive=true"
           f"&moveln=true")
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        body = r.json()
        log.info("Lista API: %d elem (offset=%d)", len(body.get("data", [])), offset)
        return body.get("data", [])
    except Exception as exc:
        log.warning("Lista API hiba: %s", exc)
        return []

def api_detail(session: requests.Session, exec_id, auction_id) -> Optional[Dict]:
    url = f"{API_BASE}/auction/detail/{exec_id}/{auction_id}"
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        body = r.json()
        if body.get("success"):
            return body.get("data", {})
        else:
            log.warning("API detail sikertelen: %s", body.get("message"))
            return None
    except Exception as exc:
        log.warning("Részlet API hiba (%s/%s): %s", exec_id, auction_id, exc)
        return None

# ── Adatok összegyűjtése a JSON-ból ──────────────────────────────────────────
def extract_auction_data(api_data: Dict, auction_id: str, url: str) -> Dict:
    """Kinyer minden szükséges mezőt az API JSON válaszából."""
    # Alapadatok
    case_number = api_data.get("caseNumber", "")
    put_up_price = parse_price(api_data.get("putUpPrice"))
    min_price = api_data.get("minPrice")  # már szám
    if min_price is not None:
        min_price = int(min_price)
    bid_step = parse_price(api_data.get("bidStep"))
    down_pay = parse_price(api_data.get("downPay"))
    start_date = api_data.get("auctionStartDate")
    end_date = api_data.get("auctionEndDate")
    bid_count = api_data.get("bidCount", 0)
    ownership_share = api_data.get("p_tulajdonihanyad", "")

    # Cím (propertyAddress tömb, az első elem)
    addr = api_data.get("propertyAddress", [{}])[0]
    zip_code = addr.get("zipCode", "")
    settlement = addr.get("settlement", "")
    street = addr.get("nameOfPublicArea", "")
    formatted_address = addr.get("formattedAddress", "")
    if not formatted_address and settlement and street:
        formatted_address = f"{zip_code} {settlement}, {street}"

    # Megye – nincs közvetlenül, de a település alapján lehet szótárból
    # (opcionális: töltsd be a telepulesek.csv-t a megye kiegészítéshez)
    county = None  # itt majd később beilleszthető a TELEPULES_MAP használata

    # Leírásból telekméret
    description = api_data.get("description", "")
    land_area = extract_area_from_description(description)

    # Képek (largeImageUrl)
    images = []
    for img in api_data.get("imageList", []):
        url_img = img.get("largeImageUrl")
        if url_img:
            images.append(url_img)

    # Tulajdoni hányad ellenőrzés (már van)
    # Beköltözhető (propertyAttributes-ból)
    is_move_in = False
    for attr in api_data.get("propertyAttributes", []):
        if attr.get("attributesGroup") == "bekoltozheto" and attr.get("attribute") == True:
            is_move_in = True
            break

    # Ár: a jelenlegi licit nincs külön mező, de a legmagasabb licit a bidCount alapján
    # Használjuk a min_price-t vagy put_up_price-t
    current_price = None
    if bid_count > 0:
        # Sajnos az API nem adja vissza a legmagasabb licit összegét, csak a darabszámot.
        # Ilyenkor a kikiáltási ár (putUpPrice) a mérvadó.
        current_price = put_up_price
    else:
        current_price = min_price or put_up_price

    # Összeállítjuk a dict-et
    data = {
        "auction_id": auction_id,
        "url": url,
        "case_number": case_number,
        "megye": county,
        "telepules": settlement,
        "cim": formatted_address,
        "put_up_price": put_up_price,       # kikiáltási ár
        "min_price": min_price,             # minimum ár
        "bid_step": bid_step,
        "down_pay": down_pay,
        "current_price": current_price,
        "bid_count": bid_count,
        "ownership_share": ownership_share,
        "start_date": start_date,
        "end_date": end_date,
        "land_area": land_area,
        "is_move_in": is_move_in,
        "images": images,
        "description": description[:500] + "..." if len(description) > 500 else description,
    }
    return data

# ── Szűrési feltételek ───────────────────────────────────────────────────────
def county_matches(megye: Optional[str]) -> bool:
    if not COUNTIES:
        return True
    if not megye:
        return False
    norm = normalize(megye)
    return any(normalize(c) in norm or norm in normalize(c) for c in COUNTIES)

def share_accepted(hanyad: Optional[str]) -> bool:
    if not hanyad:
        return False
    h = hanyad.strip()
    if re.fullmatch(r"1/1", h):
        return True
    parts = re.split(r"\s*[+&]\s*", h)
    if len(parts) == 2 and all(re.fullmatch(r"1/2", p.strip()) for p in parts):
        return True
    return False

def passes_filters(data: Dict) -> bool:
    if not county_matches(data.get("megye")):
        return False

    end_date_str = data.get("end_date")
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str.replace(' ', 'T'))
            if end_date < datetime.now():
                log.debug("Lejárt árverés: %s", end_date_str)
                return False
        except:
            pass

    if not share_accepted(data.get("ownership_share")):
        return False

    price = data.get("current_price")
    if price is None or price > MAX_PRICE:
        return False

    return True

# ── Telegram értesítés (szöveg + képek) ──────────────────────────────────────
def send_telegram_photo(photo_url: str, caption: str = ""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        img_data = requests.get(photo_url, timeout=10).content
        files = {'photo': img_data}
        data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                             data=data, files=files, timeout=20)
        if resp.status_code != 200:
            log.error("Kép küldési hiba: %s", resp.text[:200])
        else:
            log.info("Kép elküldve: %s", photo_url)
    except Exception as e:
        log.error("Kép küldési kivétel: %s", e)

def send_telegram(data: Dict):
    lines = []
    lines.append("🏠 *ÚJ MBVK ÁRVERÉS*")
    lines.append("")
    if data.get("cim"):
        lines.append(f"📍 *Cím:* {data['cim']}")
    if data.get("telepules"):
        lines.append(f"🏙️ *Település:* {data['telepules']}")
    if data.get("case_number"):
        lines.append(f"📑 *Ügyszám:* {data['case_number']}")
    if data.get("put_up_price"):
        lines.append(f"💰 *Kikiáltási ár:* {data['put_up_price']:,} Ft".replace(",", " "))
    if data.get("min_price"):
        lines.append(f"📉 *Minimum ár:* {data['min_price']:,} Ft".replace(",", " "))
    if data.get("current_price"):
        lines.append(f"💵 *Aktuális ár:* {data['current_price']:,} Ft".replace(",", " "))
    if data.get("bid_step"):
        lines.append(f"📈 *Licitlépcső:* {data['bid_step']:,} Ft".replace(",", " "))
    if data.get("down_pay"):
        lines.append(f"💸 *Árverési előleg:* {data['down_pay']:,} Ft".replace(",", " "))
    if data.get("bid_count", 0) > 0:
        lines.append(f"🔄 *Licitek száma:* {data['bid_count']}")
    if data.get("land_area"):
        lines.append(f"📐 *Telek/alapterület:* {data['land_area']:.0f} m²")
    if data.get("ownership_share"):
        lines.append(f"📄 *Tulajdoni hányad:* {data['ownership_share']}")
    if data.get("end_date"):
        lines.append(f"⏳ *Árverés vége:* {data['end_date']}")

    # Google Maps link
    if data.get("cim"):
        encoded_cim = urllib.parse.quote(data["cim"])
        lines.append(f"🗺️ [Térkép](https://www.google.com/maps/search/?api=1&query={encoded_cim})")
    lines.append("")
    lines.append(f"🔗 [Részletek]({data.get('url', '')})")

    text = "\n".join(lines)

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("Telegram szöveg elküldve")
        else:
            log.error("Telegram hiba: %s", resp.text[:200])
    except Exception as e:
        log.error("Telegram küldési hiba: %s", e)

    # Képek küldése (legfeljebb 3)
    for i, img_url in enumerate(data.get("images", [])[:3]):
        caption = f"📸 Kép {i+1}" if len(data["images"]) > 1 else "📸"
        send_telegram_photo(img_url, caption)

# ── Főprogram ─────────────────────────────────────────────────────────────────
def run():
    log.info("MBVK Monitor indítás (API alapú, JSON-ból minden) – %s", datetime.now().isoformat())
    conn = init_db()

    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. Lista lekérése
    items = []
    offset = 0
    while True:
        batch = api_list(session, offset=offset, limit=100)
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(1)

    log.info("Összesen %d beköltözhető árverés a listában", len(items))
    if not items:
        log.warning("Nincs beköltözhető ingatlan.")
        conn.close()
        return

    new_count = notified_count = 0
    for item in items:
        exec_id    = str(item.get("auctionId") or "")
        auction_id = str(item.get("id") or "")
        if not auction_id:
            continue

        url = f"{BASE_URL}/arveres-reszletek/{exec_id}/{auction_id}"
        if not is_new(conn, auction_id):
            log.debug("Már ismert: %s", auction_id)
            continue

        new_count += 1

        # 2. Részletes adatok az API-ból
        api_data = api_detail(session, exec_id, auction_id)
        if not api_data:
            log.warning("API nem adott adatokat, kihagyás: %s", auction_id)
            mark_seen(conn, auction_id)
            continue

        # 3. Adatok kinyerése a JSON-ból
        data = extract_auction_data(api_data, auction_id, url)

        log.info("Feldolgozva: %s | település=%s | tul.hányad=%s | ár=%s | telek=%s",
                 auction_id, data.get("telepules"), data.get("ownership_share"),
                 data.get("current_price"), data.get("land_area"))

        if passes_filters(data):
            log.info("✅ ÁTMENT: %s", auction_id)
            send_telegram(data)
            notified_count += 1
        else:
            log.info("❌ Nem ment át (hányad/ár/dátum/megye): %s", auction_id)

        mark_seen(conn, auction_id)
        time.sleep(2)  # kíméletes lekérés

    log.info("Kész – Új: %d / Értesítés: %d / Összes beköltözhető: %d",
             new_count, notified_count, len(items))
    conn.close()

if __name__ == "__main__":
    run()
