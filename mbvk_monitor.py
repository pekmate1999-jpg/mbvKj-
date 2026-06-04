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
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

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
           f"&phaseCode=online_ingo_2021&isLive=true")
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


def api_licit(session: requests.Session, exec_id, auction_id) -> dict:
    """Lekéri a licit adatokat."""
    url = f"{API_BASE}/auction/licit/{exec_id}/{auction_id}?page=1"
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        body = r.json()
        return body
    except Exception as exc:
        log.warning("Licit API hiba: %s", exc)
        return {}

# ── Adatkinyerés ──────────────────────────────────────────────────────────────

def extract(data: dict) -> dict:
    """
    Kinyeri a szükséges mezőket az API 'data' objektumából.
    Naplóz minden kulcsot debug céllal.
    """
    log.info("API data kulcsok: %s", list(data.keys()))

    def g(*keys):
        for k in keys:
            # Közvetlen kulcs
            v = data.get(k)
            if v is not None and str(v).strip() not in ("", "null", "None"):
                return v
            # Beágyazott objektumokban keresés
            for dk, dv in data.items():
                if isinstance(dv, dict):
                    v2 = dv.get(k)
                    if v2 is not None and str(v2).strip() not in ("", "null", "None"):
                        return v2
        return None

    # Megye
    megye = g(
        "county", "megye", "varmegye", "countyName", "countyId",
        "addressCounty", "address_county", "cityCounty",
    )
    # Ha az érték szám (ID), nem szöveg – próbáljuk a névmezőt
    if megye and str(megye).isdigit():
        megye = g("countyName", "countyLabel", "countyText", "varmegyeNev")

    # Település
    telepules = g(
        "city", "telepules", "settlement", "cityName", "town",
        "addressCity", "address_city", "helyszin",
    )

    # Cím
    cim = g(
        "address", "cim", "fullAddress", "ingatlanCim",
        "addressFull", "streetAddress", "location",
    )
    if not cim and telepules:
        cim = str(telepules)

    # Tulajdoni hányad
    hanyad = g(
        "ownershipShare", "tulajdoniHanyad", "hanyad",
        "ownership", "share", "tulajdoni_hanyad",
    )

    # Beköltözhető
    bek_raw = g(
        "isFree", "bekoltözheto", "bekoltozheto", "movable",
        "isFreeToMove", "isMovable", "szabad", "free",
    )
    if bek_raw is None:
        bekoltözhető = None
    elif str(bek_raw).lower() in ("true", "1", "igen", "yes"):
        bekoltözhető = "igen"
    elif str(bek_raw).lower() in ("false", "0", "nem", "no"):
        bekoltözhető = "nem"
    else:
        bekoltözhető = str(bek_raw).lower()

    # Árak
    kikialtas_ar     = parse_price(g("startPrice", "kikialtasiAr", "openingPrice", "startingPrice"))
    minimum_ar       = parse_price(g("minimumPrice", "minimumAr", "minPrice", "minBid", "minimumBid"))
    legmagasabb_licit = parse_price(g("currentBid", "highestBid", "legmagasabbLicit", "maxBid", "currentPrice"))
    licitek_szama_raw = g("bidCount", "licitekSzama", "numberOfBids", "bidNumber")
    licitek_szama    = int(str(licitek_szama_raw)) if licitek_szama_raw and str(licitek_szama_raw).isdigit() else 0

    # Árverés vége
    arveres_vege = g(
        "endDate", "auctionEnd", "arveresVege", "closingDate",
        "endDateTime", "vegeDatuma", "lezarasDatuma", "closeDate",
    )

    # Telekméret
    telek_raw = g(
        "plotArea", "telekMeret", "landArea", "plotSize",
        "terulet", "telekTerulet", "area", "landSize",
    )
    telekmeret = parse_area(telek_raw)

    # Épület méret
    ep_raw = g(
        "floorArea", "epuletMeret", "buildingArea", "livingArea",
        "alapterulet", "usableArea", "netArea",
    )
    epulet_meret = parse_area(ep_raw)

    price = legmagasabb_licit or minimum_ar
    ft_per_m2 = round(price / telekmeret) if price and telekmeret and telekmeret > 0 else None

    return {
        "megye":            str(megye) if megye else None,
        "telepules":        str(telepules) if telepules else None,
        "cim":              str(cim) if cim else "N/A",
        "tulajdoni_hanyad": str(hanyad) if hanyad else None,
        "bekoltözhető":     bekoltözhető,
        "kikialtas_ar":     kikialtas_ar,
        "minimum_ar":       minimum_ar,
        "legmagasabb_licit": legmagasabb_licit,
        "licitek_szama":    licitek_szama,
        "arveres_vege":     str(arveres_vege) if arveres_vege else None,
        "telekmeret":       telekmeret,
        "epulet_meret":     epulet_meret,
        "price":            price,
        "ft_per_m2":        ft_per_m2,
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
        exec_id    = (item.get("auctionId") or item.get("vegrehajtoid") or
                      item.get("executionId") or item.get("id") or "")
        auction_id = (item.get("auctionItemId") or item.get("itemId") or
                      item.get("auction_item_id") or item.get("azonosito") or "")

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
