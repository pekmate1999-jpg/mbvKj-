#!/usr/bin/env python3
"""
MBVK Árverési Monitor v5 – API first, HTML csak kiegészítés
"""

import csv
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
from bs4 import BeautifulSoup

# ── Település-megye szótár (opcionális) ──────────────────────────────────────
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
        log.warning("telepulesek.csv nem található – megye kiegészítés nem működik")
    except Exception as e:
        log.error("Hiba a CSV betöltésekor: %s", e)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
BASE_URL      = "https://arveres.mbvk.hu"
API_BASE      = "https://arveres.mbvk.hu/publicapi"
DB_PATH       = "mbvk_v9.db"
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

def parse_float(val) -> Optional[float]:
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

# ── API hívások (lista + részletek) ───────────────────────────────────────────
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
        return body.get("data", {})
    except Exception as exc:
        log.warning("Részlet API hiba (%s/%s): %s", exec_id, auction_id, exc)
        return None

# ── HTML kiegészítés (telekméret, szobák, állapot) ──────────────────────────
def extract_extra_from_html(session: requests.Session, detail_url: str) -> Dict[str, Any]:
    """
    Megpróbálja kinyerni a HTML-ből azokat az adatokat, amelyek az API-ból hiányozhatnak.
    Ha nem találja, akkor None-t ad vissza.
    """
    extra = {
        "telekmeret": None,
        "szobak_szama": None,
        "allapot": None,
        "epites_eve": None,
        "komfort": None,
        "energia_tanusitvany": None,
        "kepek": [],
    }
    try:
        resp = session.get(detail_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # --- Leírás szöveg (ha van) ---
        desc_elem = soup.select_one("div.description")
        full_text = desc_elem.get_text() if desc_elem else ""

        # Telekméret / alapterület
        area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*m[²2]", full_text, re.IGNORECASE)
        if area_match:
            extra["telekmeret"] = parse_float(area_match.group(1))

        # Szobák száma
        room_match = re.search(r"(\d+)\s+szoba", full_text, re.IGNORECASE)
        if room_match:
            extra["szobak_szama"] = int(room_match.group(1))

        # Állapot
        allapot_match = re.search(r"állapota:\s*([^.\n]+)", full_text, re.IGNORECASE)
        if allapot_match:
            extra["allapot"] = allapot_match.group(1).strip()

        # Építés éve (életkor alapján)
        kor_match = re.search(r"életkora:\s*(\d+)\s*év", full_text, re.IGNORECASE)
        if kor_match:
            extra["epites_eve"] = str(datetime.now().year - int(kor_match.group(1)))

        # Komfort, energia
        komfort_match = re.search(r"Komfort:\s*([^.\n]+)", full_text, re.IGNORECASE)
        if komfort_match:
            extra["komfort"] = komfort_match.group(1).strip()
        energia_match = re.search(r"Energia tanúsítvány:\s*([^.\n]+)", full_text, re.IGNORECASE)
        if energia_match:
            extra["energia_tanusitvany"] = energia_match.group(1).strip()

        # --- Képek ---
        for img in soup.select(".desktop-gallery .img-button img, .mobile-gallery img"):
            src = img.get("src") or img.get("data-src")
            if src:
                if src.startswith("/"):
                    src = BASE_URL + src
                if src.startswith("http") and src not in extra["kepek"]:
                    extra["kepek"].append(src)

        log.info("HTML kiegészítés: telek=%s, szobák=%s, állapot=%s",
                 extra["telekmeret"], extra["szobak_szama"], extra["allapot"])
        return extra
    except Exception as e:
        log.debug("HTML kiegészítés sikertelen: %s", e)
        return extra

# ── Adatok összeállítása (API + HTML kiegészítés) ────────────────────────────
def build_auction_data(api_data: Dict, html_extra: Dict, url: str, auction_id: str) -> Dict:
    """Összefésüli az API adatokat a HTML-ből nyert extra mezőkkel."""
    # Alapadatok az API-ból
    def get_from_attrs(key):
        attrs = api_data.get("propertyAttributes", [])
        for attr in attrs:
            if attr.get("key") == key:
                return attr.get("value")
        return None

    def g(*keys):
        for k in keys:
            if k in api_data:
                return api_data[k]
            val = get_from_attrs(k)
            if val is not None and str(val).strip() not in ("", "null", "None"):
                return val
            addr = api_data.get("propertyAddress", {})
            if isinstance(addr, dict) and k in addr:
                return addr[k]
        return None

    megye = g("county", "megye", "varmegye", "countyName")
    telepules = g("city", "telepules", "cityName", "addressCity")
    if not megye and telepules and normalize(str(telepules)) in TELEPULES_MAP:
        megye = TELEPULES_MAP[normalize(str(telepules))]

    hanyad = g("p_tulajdonihanyad", "ownershipShare", "tulajdoniHanyad", "hanyad")
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
    else:
        licit_szam = 0

    arveres_vege = g("endDate", "auctionEnd", "deadline", "befejezesDatuma")

    # Összeállítás
    data = {
        "auction_id": auction_id,
        "url": url,
        "megye": megye,
        "telepules": telepules,
        "tulajdoni_hanyad": hanyad,
        "price": price,
        "min_price": minimum_ar,
        "starting_price": kikialtas_ar,
        "legmagasabb_licit": legmagasabb_licit,
        "licitek_szama": licit_szam,
        "arveres_vege": arveres_vege,
        # HTML kiegészítés
        "telekmeret": html_extra.get("telekmeret"),
        "szobak_szama": html_extra.get("szobak_szama"),
        "allapot": html_extra.get("allapot"),
        "epites_eve": html_extra.get("epites_eve"),
        "komfort": html_extra.get("komfort"),
        "energia_tanusitvany": html_extra.get("energia_tanusitvany"),
        "kepek": html_extra.get("kepek", []),
        # Cím (ha van az API-ban)
        "cim": g("address", "fullAddress", "cim") or "N/A",
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

    end_str = data.get("arveres_vege")
    if end_str:
        try:
            # Az API általában ISO formátumban adja (pl. "2026-08-03T16:00:00")
            end_date = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
            if end_date < datetime.now():
                log.debug("Lejárt árverés: %s", end_str)
                return False
        except:
            pass

    if not share_accepted(data.get("tulajdoni_hanyad")):
        return False

    price = data.get("price")
    if price is None or price > MAX_PRICE:
        return False

    return True

# ── Telegram üzenet ──────────────────────────────────────────────────────────
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
    if data.get("cim") and data["cim"] != "N/A":
        lines.append(f"📍 *Cím:* {data['cim']}")
    if data.get("megye"):
        lines.append(f"🗺️ *Megye:* {data['megye']}")
    if data.get("telepules"):
        lines.append(f"🏙️ *Település:* {data['telepules']}")
    if data.get("price"):
        lines.append(f"💰 *Aktuális ár:* {data['price']:,} Ft".replace(",", " "))
    if data.get("min_price"):
        lines.append(f"📉 *Minimum ár:* {data['min_price']:,} Ft".replace(",", " "))
    if data.get("legmagasabb_licit"):
        lines.append(f"📈 *Legmagasabb licit:* {data['legmagasabb_licit']:,} Ft".replace(",", " "))
    if data.get("licitek_szama", 0) > 0:
        lines.append(f"🔄 *Licitek száma:* {data['licitek_szama']}")
    if data.get("telekmeret"):
        lines.append(f"📐 *Telek/alapterület:* {data['telekmeret']:.0f} m²")
    if data.get("szobak_szama"):
        lines.append(f"🛏️ *Szobák:* {data['szobak_szama']}")
    if data.get("allapot"):
        lines.append(f"🔧 *Állapot:* {data['allapot']}")
    if data.get("epites_eve"):
        lines.append(f"📅 *Építés éve:* {data['epites_eve']}")
    if data.get("komfort"):
        lines.append(f"🚿 *Komfort:* {data['komfort']}")
    if data.get("tulajdoni_hanyad"):
        lines.append(f"📄 *Tulajdoni hányad:* {data['tulajdoni_hanyad']}")
    if data.get("arveres_vege"):
        lines.append(f"⏳ *Árverés vége:* {data['arveres_vege']}")

    # Google Maps link (ha van cím)
    if data.get("cim") and data["cim"] != "N/A":
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
    for i, img_url in enumerate(data.get("kepek", [])[:3]):
        caption = f"📸 Kép {i+1}" if len(data["kepek"]) > 1 else "📸"
        send_telegram_photo(img_url, caption)

# ── Főprogram ─────────────────────────────────────────────────────────────────
def run():
    load_telepules_map()
    log.info("MBVK Monitor indítás (API first) – %s", datetime.now().isoformat())
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

        # 3. HTML kiegészítés (telekméret, szobák, képek)
        html_extra = extract_extra_from_html(session, url)

        # 4. Adatok összefésülése
        data = build_auction_data(api_data, html_extra, url, auction_id)

        log.info("Feldolgozva: %s | megye=%s | hányad=%s | ár=%s | cím=%s | telek=%s",
                 auction_id, data.get("megye"), data.get("tulajdoni_hanyad"),
                 data.get("price"), data.get("cim"), data.get("telekmeret"))

        if passes_filters(data):
            log.info("✅ ÁTMENT: %s", auction_id)
            send_telegram(data)
            notified_count += 1
        else:
            log.info("❌ Nem ment át (hányad/ár/dátum/megye): %s", auction_id)

        mark_seen(conn, auction_id)
        time.sleep(2)

    log.info("Kész – Új: %d / Értesítés: %d / Összes beköltözhető: %d",
             new_count, notified_count, len(items))
    conn.close()

if __name__ == "__main__":
    run()
