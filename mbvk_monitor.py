#!/usr/bin/env python3
"""
MBVK Árverési Monitor v3 – Beköltözhető ingatlanok, Google Maps link, képlekérés HTML-ből
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
from typing import Optional, List, Dict

import requests

# BeautifulSoup opcionális – ha nincs, a képküldés kimarad
try:
    from bs4 import BeautifulSoup
    BS_AVAILABLE = True
except ImportError:
    BS_AVAILABLE = False
    logging.warning("BeautifulSoup4 nincs telepítve – képek nem lesznek letöltve. Telepítsd: pip install beautifulsoup4")

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
DB_PATH       = "mbvk_v7.db"
MAX_PRICE     = 1_000_000

# Megye szűrés KI (üres lista = minden megye jó)
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

def extract(data: Dict) -> Dict:
    """Kinyeri a szükséges mezőket az API válaszából. A címet összerakja."""
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

    # Cím összerakása (elsőbbséget ad a propertyAddress-nek)
    cim_parts = []
    addr = data.get("propertyAddress", {})
    if isinstance(addr, dict):
        irsz = addr.get("postCode") or g("postCode", "irsz")
        if irsz:
            cim_parts.append(str(irsz))
        city = addr.get("city") or telepules
        if city:
            cim_parts.append(str(city))
        street = addr.get("street") or addr.get("addressLine") or g("street", "addressLine")
        if street:
            cim_parts.append(str(street))
    if not cim_parts:
        cim = g("address", "cim", "fullAddress", "ingatlanCim")
        if not cim and telepules:
            cim = str(telepules)
    else:
        cim = ", ".join(cim_parts)

    hanyad = g("p_tulajdonihanyad", "ownershipShare", "tulajdoniHanyad", "hanyad")

    # Beköltözhető – mivel moveln=true, fixen igen
    bekoltözhető = "igen"

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

    telek_raw = g("area", "totalArea", "landArea", "builtArea", "alapterulet", "telekmeret")
    telek = parse_area(telek_raw)

    arveres_vege = g("endDate", "auctionEnd", "deadline", "befejezesDatuma")
    ft_per_m2 = int(price / telek) if price and telek and telek > 0 else None

    return {
        "megye": megye,
        "telepules": telepules,
        "cim": cim if cim else "N/A",
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

def fetch_images_from_html(session: requests.Session, detail_url: str) -> List[str]:
    """Letölti a részletes oldal HTML-jét és kinyeri a kép URL-eket."""
    if not BS_AVAILABLE:
        return []
    try:
        resp = session.get(detail_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        image_urls = []
        # Keresés img tag-ekben
        for img in soup.find_all('img'):
            src = img.get('src')
            if src:
                # Teljes URL képzése, ha relatív
                if src.startswith('/'):
                    src = BASE_URL + src
                if src.startswith('http'):
                    image_urls.append(src)
        # Keresés div vagy más elemekben, ahol data-src lehet (lazy load)
        for elem in soup.find_all(attrs={"data-src": True}):
            src = elem['data-src']
            if src.startswith('/'):
                src = BASE_URL + src
            if src.startswith('http'):
                image_urls.append(src)
        # Deduplikáció
        image_urls = list(dict.fromkeys(image_urls))
        log.info("%d képet találtam az oldalon: %s", len(image_urls), detail_url)
        return image_urls
    except Exception as e:
        log.warning("Nem sikerült letölteni a HTML-t képekhez: %s", e)
        return []

# ── Szűrés (csak hányad és ár, futó árverés) ─────────────────────────────────
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
    # Megye (opcionális)
    if not county_matches(data.get("megye")):
        return False

    # Futó árverés (endDate a jövőben)
    end_date_str = data.get("arveres_vege")
    if end_date_str and end_date_str != "N/A":
        try:
            end_date = datetime.fromisoformat(end_date_str.replace(' ', 'T'))
        except:
            try:
                end_date = datetime.strptime(end_date_str, "%Y-%m-%d")
            except:
                end_date = None
        if end_date and end_date < datetime.now():
            log.debug("Lejárt árverés: %s", end_date_str)
            return False

    # Tulajdoni hányad
    if not share_accepted(data.get("tulajdoni_hanyad")):
        return False

    # Ár
    price = data.get("price")
    if price is None or price > MAX_PRICE:
        return False

    return True

# ── Telegram (szöveg + kép) ──────────────────────────────────────────────────
def send_telegram_text(text: str):
    """Csak szöveges üzenet küldése (hasznos hibakezeléshez)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.error("Telegram szöveg hiba: %s", resp.text[:200])
    except Exception as e:
        log.error("Telegram szöveg küldési hiba: %s", e)

def send_telegram_photo(photo_url: str, caption: str):
    """Egyetlen kép küldése URL-ről a Telegram bot segítségével."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
            files={"photo": requests.get(photo_url, timeout=10).content},
            timeout=20,
        )
        if resp.status_code == 200:
            log.info("Kép elküldve: %s", photo_url)
        else:
            log.error("Kép küldési hiba: %s", resp.text[:200])
    except Exception as e:
        log.error("Kép küldési kivétel: %s", e)

def send_telegram(data: Dict, image_urls: List[str] = None):
    """Összeállítja a szöveges üzenetet (Google Maps linkkel) és opcionálisan képeket küld."""
    cim = data.get('cim', 'N/A')
    price = data.get("price")
    price_str = f"{price:,} Ft".replace(",", " ") if price else None
    legh = data.get("legmagasabb_licit")
    legh_str = f"{legh:,} Ft".replace(",", " ") if legh else None
    licit_szam = data.get("licitek_szama", 0)
    licit_str = str(licit_szam) if licit_szam > 0 else None
    telek = data.get("telekmeret")
    telek_str = f"{telek:.0f} m²" if telek else None
    ft_m2 = data.get("ft_per_m2")
    ft_m2_str = f"{ft_m2:,} Ft/m²".replace(",", " ") if ft_m2 else None
    hanyad = data.get("tulajdoni_hanyad")
    hanyad_str = hanyad if hanyad else None
    end = data.get("arveres_vege")
    end_str = end if end and end != "N/A" else None

    # Google Maps link
    maps_link = ""
    if cim and cim != "N/A":
        encoded_cim = urllib.parse.quote(cim)
        maps_link = f"\n🗺️ [Térkép](https://www.google.com/maps/search/?api=1&query={encoded_cim})"

    # Üzenet összeállítása
    lines = []
    lines.append("🏠 *ÚJ MBVK TALÁLAT*")
    lines.append("")
    if cim and cim != "N/A":
        lines.append(f"📍 *Cím:* {cim}")
    if price_str:
        lines.append(f"💰 *Ár:* {price_str}")
    if legh_str:
        lines.append(f"📈 *Legmagasabb licit:* {legh_str}")
    if licit_str:
        lines.append(f"🔄 *Licitek száma:* {licit_str}")
    if telek_str:
        lines.append(f"📐 *Telek/alapterület:* {telek_str}")
    if ft_m2_str:
        lines.append(f"💹 *Ft/m²:* {ft_m2_str}")
    lines.append(f"🚪 *Beköltözhető:* igen")
    if hanyad_str:
        lines.append(f"📄 *Tulajdoni hányad:* {hanyad_str}")
    if end_str:
        lines.append(f"⏳ *Árverés vége:* {end_str}")
    lines.append("")
    lines.append(f"🔗 [Részletek]({data.get('url', '')}){maps_link}")

    text = "\n".join(lines)

    # Szöveg küldése
    send_telegram_text(text)

    # Képek küldése (első 3 kép, hogy ne legyen túl sok)
    if image_urls:
        for i, img_url in enumerate(image_urls[:3]):
            caption = f"📸 Kép {i+1}" if len(image_urls) > 1 else "📸"
            send_telegram_photo(img_url, caption)

# ── Főprogram ─────────────────────────────────────────────────────────────────
def run():
    load_telepules_map()
    log.info("MBVK Monitor indítás (moveln=true, képkinyerés HTML-ből) – %s", datetime.now().isoformat())
    conn = init_db()

    session = requests.Session()
    session.headers.update(HEADERS)

    # Lista lekérése
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
            log.debug("Már ismert: %s", auction_id)
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
            # Képek keresése a részletes oldalon
            image_urls = fetch_images_from_html(session, url) if BS_AVAILABLE else []
            send_telegram(data, image_urls)
            notified_count += 1
        else:
            log.info("❌ Nem ment át (hányad/ár/dátum): %s", auction_id)

        mark_seen(conn, auction_id)
        time.sleep(1)

    log.info("Kész – Új: %d / Értesítés: %d / Összes beköltözhető: %d",
             new_count, notified_count, len(items))
    conn.close()

if __name__ == "__main__":
    run()
