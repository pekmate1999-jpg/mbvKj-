#!/usr/bin/env python3
"""
MBVK Árverési Monitor v3 - Beköltözhető ingatlanokra szűrve (moveln=true)
"""

import csv
import os
import re
import sys
import time
import sqlite3
import logging
import unicodedata
from datetime import datetime
from typing import Optional, List, Dict

import requests

# ── Globális szótár (megye kiegészítéshez, de nem kötelező) ──
TELEPULES_MAP = {}

def load_telepules_map():
    try:
        with open("telepulesek.csv", mode='r', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter=';')
            for row in reader:
                if len(row) >= 2:
                    TELEPULES_MAP[normalize(row[0].strip())] = row[1].strip()
        log.info("Település mappa betöltve: %d elem", len(TELEPULES_MAP))
    except FileNotFoundError:
        log.warning("telepulesek.csv nem található")
    except Exception as e:
        log.error("Hiba a CSV betöltésekor: %s", e)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
BASE_URL      = "https://arveres.mbvk.hu"
API_BASE      = "https://arveres.mbvk.hu/publicapi"
DB_PATH       = "mbvk_v7.db"
MAX_PRICE     = 1_000_000

# Megye szűrés KI (üres lista)
COUNTIES = []

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

def parse_area(val) -> Optional[float]:
    if val is None:
        return None
    s = str(val).strip()
    m = re.search(r"([\d]+(?:[.,][\d]+)?)", s)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    return None

# ── API hívások ───────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://arveres.mbvk.hu/",
}

def api_list(session: requests.Session, offset=0, limit=100) -> List[Dict]:
    """Csak beköltözhető (moveln=true) ingatlanokat kérdez."""
    url = (f"{API_BASE}/auction/list"
           f"?offset={offset}&limit={limit}"
           f"&sortMod=feltolt&sortDirection=desc"
           f"&phaseCode=normal_ingatlan_2021&isLive=true"
           f"&moveln=true")   # ← BEKÖLTÖZHETŐ SZŰRŐ
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
        return body.get("data", {})
    except Exception as exc:
        log.warning("Részlet API hiba (%s/%s): %s", exec_id, auction_id, exc)
        return None

def extract(data: Dict) -> Dict:
    """Kinyeri a szükséges mezőket (beköltözhetőt már nem kell ellenőrizni, de azért kitölti)."""
    def get_from_attrs(key):
        attrs = data.get("propertyAttributes", [])
        for attr in attrs:
            if attr.get("key") == key:
                return attr.get("value")
        return None

    def g(*keys):
        for k in keys:
            if k in data:
                return data[k]
            val = get_from_attrs(k)
            if val is not None and str(val).strip() not in ("", "null", "None"):
                return val
            addr = data.get("propertyAddress", {})
            if isinstance(addr, dict) and k in addr:
                return addr[k]
        return None

    megye = g("county", "megye", "varmegye", "countyName")
    telepules = g("city", "telepules", "cityName", "addressCity")
    if not megye and telepules and normalize(str(telepules)) in TELEPULES_MAP:
        megye = TELEPULES_MAP[normalize(str(telepules))]

    cim = g("address", "cim", "fullAddress", "ingatlanCim") or str(telepules) if telepules else "N/A"
    hanyad = g("p_tulajdonihanyad", "ownershipShare", "tulajdoniHanyad", "hanyad")

    # Beköltözhető – mivel a lista már szűrt, alapból "igen", de az adatból is kiolvassuk
    bek_raw = g("isFree", "bekoltözheto", "moveln", "movable")
    bekoltözhető = "igen" if str(bek_raw).lower() in ("true", "1", "igen", "yes") else "nem"

    kikialtas_ar = parse_price(g("putUpPrice", "startPrice", "kikialtasiAr"))
    minimum_ar   = parse_price(g("minPrice", "minimumAr", "minimumBid"))
    legmagasabb_licit = parse_price(g("currentBid", "highestBid", "legmagasabbLicit"))
    price = legmagasabb_licit or minimum_ar or kikialtas_ar

    licit_szam = g("bidCount", "licitekSzama")
    if licit_szam is not None:
        try:
            licit_szam = int(licit_szam)
        except:
            licit_szam = 0

    telek_raw = g("area", "telekmeret", "builtArea", "alapterulet")
    telek = parse_area(telek_raw)

    arveres_vege = g("endDate", "auctionEnd", "deadline", "befejezesDatuma")
    ft_per_m2 = int(price / telek) if price and telek and telek > 0 else None

    return {
        "megye": megye,
        "telepules": telepules,
        "cim": cim,
        "tulajdoni_hanyad": hanyad,
        "bekoltözhető": bekoltözhető,
        "price": price,
        "legmagasabb_licit": legmagasabb_licit,
        "licitek_szama": licit_szam,
        "telekmeret": telek,
        "ft_per_m2": ft_per_m2,
        "arveres_vege": arveres_vege or "N/A",
        "url": data.get("url", ""),
    }

# ── Szűrés (megye nélkül, de beköltözhetőség és ár és hányad) ─────────────────
def county_matches(megye: Optional[str]) -> bool:
    if not COUNTIES:   # üres lista => minden megye jó
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
    # Mivel a lista API már beköltözhetőket ad, itt is ellenőrizzük, de nem lesz gond
    if data.get("bekoltözhető") != "igen":
        log.debug("Nem beköltözhető: %s", data.get("bekoltözhető"))
        return False
    if not share_accepted(data.get("tulajdoni_hanyad")):
        return False
    price = data.get("price")
    if price is None or price > MAX_PRICE:
        return False
    return True

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(data: Dict):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram nincs beállítva")
        return

    price_str = f"{data['price']:,} Ft".replace(",", " ") if data.get("price") else "N/A"
    legh_str = f"{data['legmagasabb_licit']:,} Ft".replace(",", " ") if data.get("legmagasabb_licit") else "nincs"
    telek_str = f"{data['telekmeret']:.0f} m2" if data.get("telekmeret") else "N/A"
    ft_m2_str = f"{data['ft_per_m2']:,} Ft/m2".replace(",", " ") if data.get("ft_per_m2") else "N/A"

    text = (
        "✅ ÚJ MBVK TALÁLAT\n\n"
        f"Cím:\n{data.get('cim', 'N/A')}\n\n"
        f"Ár:\n{price_str}\n\n"
        f"Legmagasabb licit:\n{legh_str}\n\n"
        f"Licitek száma:\n{data.get('licitek_szama', 0)}\n\n"
        f"Telek:\n{telek_str}\n\n"
        f"Ft/m²:\n{ft_m2_str}\n\n"
        f"Beköltözhető:\n{data.get('bekoltözhető', 'N/A')}\n\n"
        f"Tulajdoni hányad:\n{data.get('tulajdoni_hanyad', 'N/A')}\n\n"
        f"Árverés vége:\n{data.get('arveres_vege', 'N/A')}\n\n"
        f"Link:\n{data.get('url', '')}"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("Telegram elküldve: %s", data.get("auction_id"))
        else:
            log.error("Telegram hiba: %s %s", resp.status_code, resp.text[:300])
    except Exception as exc:
        log.error("Telegram küldési hiba: %s", exc)

# ── Főprogram ─────────────────────────────────────────────────────────────────
def run():
    load_telepules_map()
    log.info("MBVK Monitor indítás (moveln=true) – %s", datetime.now().isoformat())
    conn = init_db()

    session = requests.Session()
    session.headers.update(HEADERS)

    # Lista lekérése (csak beköltözhető tételek)
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
        log.warning("Nincs beköltözhető ingatlan a listában.")
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
            continue

        new_count += 1
        detail = api_detail(session, exec_id, auction_id)
        if not detail:
            mark_seen(conn, auction_id)
            continue

        data = extract(detail)
        data["auction_id"] = auction_id
        data["url"] = url

        log.info("Feldolgozva: %s | megye=%s | hányad=%s | ár=%s",
                 auction_id, data.get("megye"), data.get("tulajdoni_hanyad"), data.get("price"))

        if passes_filters(data):
            log.info("✅ ÁTMENT: %s", auction_id)
            send_telegram(data)
            notified_count += 1
        else:
            log.info("❌ Nem ment át (hányad/ár): %s", auction_id)

        mark_seen(conn, auction_id)
        time.sleep(1)

    log.info("Kész – Új: %d / Értesítés: %d / Összes beköltözhető: %d",
             new_count, notified_count, len(items))
    conn.close()

if __name__ == "__main__":
    run()
