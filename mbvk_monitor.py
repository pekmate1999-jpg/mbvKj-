#!/usr/bin/env python3
"""
MBVK Árverési Monitor v3 (GitHub Action optimalizált)
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

# ── Globális szótár ──────────────────────────────────────────────────────────
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
        log.error("telepulesek.csv NEM TALÁLHATÓ! A megye kiegészítés nem fog működni.")
    except Exception as e:
        log.error("Hiba a CSV betöltésekor: %s", e)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
BASE_URL      = "https://arveres.mbvk.hu"
API_BASE      = "https://arveres.mbvk.hu/publicapi"
DB_PATH       = "mbvk_v7.db"
MAX_PRICE     = 1_000_000

COUNTIES = [
    "veszprém", "veszprem",
    "zala",
    "somogy",
    "pest",
    "komárom", "komarom", "komárom-esztergom",
    "fejér", "fejer",
    "nógrád", "nograd",
    "bács-kiskun", "bacs-kiskun",
    "jász-nagykun", "jasz-nagykun", "jász-nagykun-szolnok",
]

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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
    return conn.execute("SELECT 1 FROM properties WHERE auction_id = ?", (auction_id,)).fetchone() is None

def mark_seen(conn, auction_id: str):
    conn.execute("INSERT OR IGNORE INTO properties (auction_id, created) VALUES (?, ?)",
                 (auction_id, datetime.utcnow().isoformat()))
    conn.commit()

# ── Segédfüggvények ───────────────────────────────────────────────────────────
def normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))

def parse_price(val) -> Optional[int]:
    if val is None: return None
    digits = re.sub(r"[^\d]", "", str(val))
    return int(digits) if digits else None

def parse_area(val) -> Optional[float]:
    if val is None: return None
    m = re.search(r"([\d]+(?:[.,][\d]+)?)", str(val))
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
    "Referer": BASE_URL,
}

def api_list(session: requests.Session, offset=0, limit=100) -> List[Dict]:
    url = f"{API_BASE}/auction/list?offset={offset}&limit={limit}&sortMod=feltolt&sortDirection=desc&phaseCode=normal_ingatlan_2021&isLive=true"
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
        return r.json().get("data", {})
    except Exception as exc:
        log.warning("Részlet API hiba (%s/%s): %s", exec_id, auction_id, exc)
        return None

def extract(data: Dict) -> Dict:
    def get_from_attrs(key):
        attrs = data.get("propertyAttributes", [])
        if isinstance(attrs, list):
            for attr in attrs:
                if isinstance(attr, dict) and attr.get("key") == key:
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

    if not megye and telepules:
        norm_telepules = normalize(str(telepules))
        if norm_telepules in TELEPULES_MAP:
            megye = TELEPULES_MAP[norm_telepules]
            log.debug("Megye kiegészítve: %s -> %s", telepules, megye)

    cim = g("address", "cim", "fullAddress", "ingatlanCim")
    if not cim and telepules:
        cim = str(telepules)

    hanyad = g("p_tulajdonihanyad", "ownershipShare", "tulajdoniHanyad", "hanyad")

    bek_raw = g("isFree", "bekoltözheto", "bekoltozheto", "movable", "isFreeToMove")
    bekoltözhető = "igen" if str(bek_raw).lower() in ("true", "1", "igen", "yes") else "nem"

    kikialtas_ar      = parse_price(g("putUpPrice", "startPrice", "kikialtasiAr"))
    minimum_ar        = parse_price(g("minPrice", "minimumAr", "minimumBid"))
    legmagasabb_licit = parse_price(g("currentBid", "highestBid", "legmagasabbLicit"))
    price = legmagasabb_licit or minimum_ar or kikialtas_ar

    licit_szam = g("bidCount", "licitekSzama")
    try:
        licit_szam = int(licit_szam) if licit_szam else 0
    except:
        licit_szam = 0

    telek_raw = g("area", "telekmeret", "builtArea", "alapterulet")
    telek = parse_area(telek_raw)

    arveres_vege = g("endDate", "auctionEnd", "deadline", "befejezesDatuma")

    ft_per_m2 = None
    if price and telek and telek > 0:
        ft_per_m2 = int(price / telek)

    url = data.get("url", "")

    return {
        "megye": str(megye) if megye else None,
        "telepules": str(telepules) if telepules else None,
        "cim": str(cim) if cim else "N/A",
        "tulajdoni_hanyad": str(hanyad) if hanyad else None,
        "bekoltözhető": bekoltözhető,
        "price": price,
        "legmagasabb_licit": legmagasabb_licit,
        "licitek_szama": licit_szam,
        "telekmeret": telek,
        "ft_per_m2": ft_per_m2,
        "arveres_vege": str(arveres_vege) if arveres_vege else "N/A",
        "url": url,
    }

# ── Szűrés ────────────────────────────────────────────────────────────────────
def county_matches(megye: Optional[str]) -> bool:
    if not megye: return False
    norm = normalize(megye)
    for c in COUNTIES:
        if normalize(c) in norm or norm in normalize(c):
            return True
    return False

def share_accepted(hanyad: Optional[str]) -> bool:
    if not hanyad: return False
    h = hanyad.strip()
    if re.fullmatch(r"1/1", h): return True
    parts = re.split(r"\s*[+&]\s*", h)
    if len(parts) == 2 and all(re.fullmatch(r"1/2", p.strip()) for p in parts): return True
    return False

def passes_filters(data: Dict) -> bool:
    if not county_matches(data.get("megye")):
        return False
    if data.get("bekoltözhető") != "igen":
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
        log.error("Telegram nincs beállítva, nem küldök üzenetet.")
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
            log.error("Telegram hiba: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.error("Telegram küldési hiba: %s", exc)

def send_debug_message(msg: str):
    """Hiba esetén értesítés küldése."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": f"⚠️ MBVK Monitor figyelmeztetés:\n{msg}"},
            timeout=15,
        )
    except:
        pass

# ── Főprogram ─────────────────────────────────────────────────────────────────
def run():
    load_telepules_map()

    # Ellenőrizzük a Telegram beállításokat
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN vagy TELEGRAM_CHAT_ID nincs beállítva!")
        return

    log.info("MBVK Monitor indítás – %s", datetime.now().isoformat())
    conn = init_db()

    session = requests.Session()
    session.headers.update(HEADERS)

    # Lista lekérés
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
        time.sleep(0.5)

    log.info("Összesen %d árverés a listában", len(items))

    if not items:
        log.warning("Üres lista – nincs árverés.")
        send_debug_message("Az API üres listát adott vissza. Lehet, hogy változott a paraméter vagy nincs élő árverés.")
        conn.close()
        return

    new_count = 0
    notified_count = 0
    total_checked = 0

    for item in items:
        exec_id = str(item.get("auctionId") or "")
        auction_id = str(item.get("id") or "")
        if not auction_id:
            continue

        total_checked += 1

        if not is_new(conn, auction_id):
            continue

        new_count += 1
        detail = api_detail(session, exec_id, auction_id)
        if not detail:
            mark_seen(conn, auction_id)
            continue

        data = extract(detail)
        data["auction_id"] = auction_id
        data["url"] = f"{BASE_URL}/arveres-reszletek/{exec_id}/{auction_id}"

        log.info("Feldolgozva: %s | megye=%s | hányad=%s | bekölt=%s | ár=%s",
                 auction_id, data["megye"], data["tulajdoni_hanyad"], data["bekoltözhető"], data["price"])

        if passes_filters(data):
            log.info("✅ SZŰRŐN ÁTMENT: %s", auction_id)
            send_telegram(data)
            notified_count += 1

        mark_seen(conn, auction_id)
        time.sleep(0.5)

    log.info("Kész – Összes ellenőrzött: %d, Új: %d, Értesítés: %d", total_checked, new_count, notified_count)
    conn.close()

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.critical("Végzetes hiba: %s", e, exc_info=True)
        send_debug_message(f"A script összeomlott: {str(e)}")
        sys.exit(1)
