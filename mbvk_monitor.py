#!/usr/bin/env python3
"""
MBVK Árverési Monitor v3
Lista: publicapi/auction/list
Részlet: publicapi/auction/detail/{exec_id}/{auction_id}
"""

import os
import re
import sys
import time
import logging
import sqlite3
import unicodedata
from datetime import datetime
from typing import Optional

import requests
#from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

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
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://arveres.mbvk.hu/",
}

def api_list(session: requests.Session, offset=0, limit=100) -> list[dict]:
    """Lekéri az árverési listát az API-ból."""
    url = (f"{API_BASE}/auction/list"
           f"?offset={offset}&limit={limit}"
           f"&sortMod=feltolt&sortDirection=desc"
           f"&phaseCode=normal_ingatlan_2021&isLive=true") # <--- ITT FRISSÍTVE A KÓD
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        body = r.json()
        log.info("Lista API: %d elem (offset=%d)", len(body.get("data", [])), offset)
        return body.get("data", [])
    except Exception as exc:
        log.warning("Lista API hiba: %s", exc)
        return []


def api_detail(session: requests.Session, exec_id, auction_id) -> Optional[dict]:
    """Lekéri egy árverés részleteit."""
    url = f"{API_BASE}/auction/detail/{exec_id}/{auction_id}"
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        body = r.json()
        data = body.get("data", {})
        log.info("Részlet API OK: %s/%s | kulcsok: %s",
                 exec_id, auction_id, list(data.keys())[:20])
        return data
    except Exception as exc:
        log.warning("Részlet API hiba (%s/%s): %s", exec_id, auction_id, exc)
        return None


def extract(data: dict) -> dict:
    """
    Kinyeri a szükséges mezőket az API 'data' objektumából.
    Módosítva: kezeli a propertyAttributes listát és a propertyAddress szótárat.
    """
    
    def get_from_attrs(key_to_find):
        """Keresés a propertyAttributes listában."""
        attrs = data.get("propertyAttributes", [])
        if isinstance(attrs, list):
            for attr in attrs:
                if isinstance(attr, dict) and attr.get("key") == key_to_find:
                    return attr.get("value")
        return None

    def g(*keys):
        for k in keys:
            # 1. Közvetlen keresés
            if k in data: return data[k]
            
            # 2. propertyAttributes keresés
            val = get_from_attrs(k)
            if val is not None and str(val).strip() not in ("", "null", "None"):
                return val
            
            # 3. propertyAddress keresés
            addr = data.get("propertyAddress", {})
            if isinstance(addr, dict) and k in addr:
                return addr[k]
        return None

    # Megye (próbáljuk a 'county'-t az attribútumokból vagy a címből)
    megye = g("county", "megye", "varmegye", "countyName")
    
    # Település
    telepules = g("city", "telepules", "cityName", "addressCity")

    # Cím
    cim = g("address", "cim", "fullAddress", "ingatlanCim")
    if not cim and telepules:
        cim = str(telepules)

    # Tulajdoni hányad
    hanyad = g("p_tulajdonihanyad", "ownershipShare", "tulajdoniHanyad", "hanyad")

    # Beköltözhető (A log alapján ezeket a kulcsokat kell keresni)
    bek_raw = g("isFree", "bekoltözheto", "bekoltozheto", "movable", "isFreeToMove")
    if str(bek_raw).lower() in ("true", "1", "igen", "yes"):
        bekoltözhető = "igen"
    else:
        bekoltözhető = "nem"

    # Árak
    kikialtas_ar      = parse_price(g("putUpPrice", "startPrice", "kikialtasiAr"))
    minimum_ar        = parse_price(g("minPrice", "minimumAr", "minimumBid"))
    legmagasabb_licit = parse_price(g("currentBid", "highestBid", "legmagasabbLicit"))
    
    price = legmagasabb_licit or minimum_ar or kikialtas_ar

    return {
        "megye":            str(megye) if megye else None,
        "telepules":        str(telepules) if telepules else None,
        "cim":              str(cim) if cim else "N/A",
        "tulajdoni_hanyad": str(hanyad) if hanyad else None,
        "bekoltözhető":     bekoltözhető,
        "price":            price,
        "url":              data.get("url", ""),
    }

# ── Szűrés ────────────────────────────────────────────────────────────────────

def county_matches(megye: Optional[str]) -> bool:
    if not megye:
        return False
    norm = normalize(megye)
    for c in COUNTIES:
        if normalize(c) in norm or norm in normalize(c):
            return True
    return False

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

def passes_filters(data: dict) -> bool:
    if not county_matches(data.get("megye")):
        log.info("Szűrve (megye): %s", data.get("megye"))
        return False
    if data.get("bekoltözhető") != "igen":
        log.info("Szűrve (beköltözhető): %s", data.get("bekoltözhető"))
        return False
    if not share_accepted(data.get("tulajdoni_hanyad")):
        log.info("Szűrve (hányad): %s", data.get("tulajdoni_hanyad"))
        return False
    price = data.get("price")
    if price is None or price > MAX_PRICE:
        log.info("Szűrve (ár): %s", price)
        return False
    return True

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(data: dict):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram nincs beállítva")
        return

    price = data.get("price")
    price_str = f"{price:,} Ft".replace(",", " ") if price else "N/A"
    legh  = data.get("legmagasabb_licit")
    legh_str = f"{legh:,} Ft".replace(",", " ") if legh else "nincs"
    telek = data.get("telekmeret")
    telek_str = f"{telek:.0f} m2" if telek else "N/A"
    ft_m2 = data.get("ft_per_m2")
    ft_m2_str = f"{ft_m2:,} Ft/m2".replace(",", " ") if ft_m2 else "N/A"

    text = (
        "uj MBVK TALALAT\n\n"
        f"Cim:\n{data.get('cim', 'N/A')}\n\n"
        f"Ar:\n{price_str}\n\n"
        f"Legmagasabb licit:\n{legh_str}\n\n"
        f"Licitek szama:\n{data.get('licitek_szama', 0)}\n\n"
        f"Telek:\n{telek_str}\n\n"
        f"Ft/m2:\n{ft_m2_str}\n\n"
        f"Bekoltozheto:\nigen\n\n"
        f"Tulajdon:\n{data.get('tulajdoni_hanyad', 'N/A')}\n\n"
        f"Arveres vege:\n{data.get('arveres_vege', 'N/A')}\n\n"
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
        log.error("Telegram kuldesi hiba: %s", exc)

# ── Főprogram ─────────────────────────────────────────────────────────────────

def run():
    log.info("MBVK Monitor inditas – %s", datetime.now().isoformat())
    conn = init_db()

    session = requests.Session()
    session.headers.update(HEADERS)

    # ── 1. Lista lekérése API-ból ──
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
        log.warning("Üres lista – kilépés")
        conn.close()
        return

    new_count = notified_count = 0

    for item in items:
      # Az exec_id és auction_id kinyerése a lista elemből
        exec_id    = str(item.get("auctionId") or "")
        auction_id = str(item.get("id") or "")

        log.info("Lista elem kulcsok: %s", list(item.keys()))

        if not auction_id:
            log.warning("Nincs auction_id: %s", item)
            continue

        auction_id = str(auction_id)
        exec_id    = str(exec_id)

        url = f"{BASE_URL}/arveres-reszletek/{exec_id}/{auction_id}"

        if not is_new(conn, auction_id):
            log.debug("Már ismert: %s", auction_id)
            continue

        new_count += 1

        # ── 2. Részlet API ──
        detail = api_detail(session, exec_id, auction_id)
        if not detail:
            log.warning("Nincs részlet: %s/%s", exec_id, auction_id)
            mark_seen(conn, auction_id)
            continue

        data = extract(detail)
        data["auction_id"] = auction_id
        data["url"]        = url

        log.info(
            "Feldolgozva: %s | megye=%s | hányad=%s | bekolt=%s | ár=%s",
            auction_id, data.get("megye"), data.get("tulajdoni_hanyad"),
            data.get("bekoltözhető"), data.get("price"),
        )

        if passes_filters(data):
            log.info("SZÜRÖÖN ÁTMENT: %s", auction_id)
            send_telegram(data)
            notified_count += 1

        mark_seen(conn, auction_id)
        time.sleep(0.5)

    log.info("Kész – Új: %d / Értesítés: %d / Összes: %d",
             new_count, notified_count, len(items))
    conn.close()


if __name__ == "__main__":
    run()
