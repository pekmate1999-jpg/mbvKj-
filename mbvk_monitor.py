#!/usr/bin/env python3
"""
MBVK Árverési Monitor v4 – Minden adat a részletes HTML oldal DOM-jából
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
DB_PATH       = "mbvk_v8.db"
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

# ── API hívások (lista lekéréséhez) ───────────────────────────────────────────
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
           f"&moveln=true")   # beköltözhető = true
    try:
        r = session.get(url, timeout=20)
        r.raise_for_status()
        body = r.json()
        log.info("Lista API: %d elem (offset=%d)", len(body.get("data", [])), offset)
        return body.get("data", [])
    except Exception as exc:
        log.warning("Lista API hiba: %s", exc)
        return []

# ── HTML elemzés (összes szükséges adat kinyerése) ────────────────────────────
def extract_from_html(session: requests.Session, detail_url: str) -> Dict[str, Any]:
    """
    Letölti a részletes oldal HTML-jét, és kinyeri az összes elérhető adatot.
    """
    result = {
        # Alapadatok
        "cim": "N/A",
        "megye": None,
        "telepules": None,
        "telekmeret": None,
        "szobak_szama": None,
        "komfort": None,
        "allapot": None,
        "epites_eve": None,
        "energia_tanusitvany": None,
        # Árverési adatok
        "min_price": None,
        "starting_price": None,
        "bidding_ladder": None,
        "down_payment": None,
        "arveres_vege": None,
        "licitek_szama": 0,
        "legmagasabb_licit": None,
        # Jogi adatok
        "tulajdoni_hanyad": None,
        "bekoltozheto": None,
        "helyrajzi_szam": None,
        "jogi_jelleg": None,
        "muvelesi_ag": None,
        "fekves": None,
        "epulet_tipusok": None,
        "besorolas": None,
        "nem_torolheto_jogok": None,
        "foldhasznalat": None,
        # Képek
        "kepek": [],
        # Egyéb
        "url": detail_url,
    }

    try:
        resp = session.get(detail_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        # --- Cím (li.location p) ---
        cim_elem = soup.select_one("li.location p")
        if cim_elem:
            result["cim"] = cim_elem.get_text(strip=True)
            # Megye, település kiszedése a címből (pl. "5530 Vésztő, ...")
            city_match = re.search(r"\d{4}\s+([^,]+)", result["cim"])
            if city_match:
                result["telepules"] = city_match.group(1).strip()
                norm_telep = normalize(result["telepules"])
                if norm_telep in TELEPULES_MAP:
                    result["megye"] = TELEPULES_MAP[norm_telep]

        # --- Leírás szöveg (telekméret, szobák, állapot, építés éve) ---
        desc_elem = soup.select_one("div.description")
        full_text = desc_elem.get_text() if desc_elem else ""

        # Telekméret / alapterület
        area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*m[²2]", full_text, re.IGNORECASE)
        if area_match:
            result["telekmeret"] = parse_float(area_match.group(1))

        # Szobák száma
        room_match = re.search(r"(\d+)\s+szoba", full_text, re.IGNORECASE)
        if room_match:
            result["szobak_szama"] = int(room_match.group(1))

        # Állapot (pl. "állapota: jó")
        allapot_match = re.search(r"állapota:\s*([^.\n]+)", full_text, re.IGNORECASE)
        if allapot_match:
            result["allapot"] = allapot_match.group(1).strip()

        # Építés éve (ha életkor van megadva)
        kor_match = re.search(r"életkora:\s*(\d+)\s*év", full_text, re.IGNORECASE)
        if kor_match:
            result["epites_eve"] = str(datetime.now().year - int(kor_match.group(1)))

        # Komfort, energia (ha szerepel)
        komfort_match = re.search(r"Komfort:\s*([^.\n]+)", full_text, re.IGNORECASE)
        if komfort_match:
            result["komfort"] = komfort_match.group(1).strip()
        energia_match = re.search(r"Energia tanúsítvány:\s*([^.\n]+)", full_text, re.IGNORECASE)
        if energia_match:
            result["energia_tanusitvany"] = energia_match.group(1).strip()

        # --- Árverési alapadatok (speciális li osztályok) ---
        def get_li_value(class_name):
            elem = soup.select_one(f"li.{class_name} span:last-child")
            return elem.get_text(strip=True) if elem else None

        result["min_price"] = get_li_value("min-price")
        result["starting_price"] = get_li_value("starting-price")
        result["bidding_ladder"] = get_li_value("bidding-ladder")
        result["down_payment"] = get_li_value("down-payment")

        # --- Árverés vége ---
        end_elem = soup.select_one("li.end-date p")
        if end_elem:
            result["arveres_vege"] = end_elem.get_text(strip=True)

        # --- Licitnapló adatok (táblázatból) ---
        licit_sorok = soup.select(".table-wrapper tbody tr")
        result["licitek_szama"] = len(licit_sorok)
        if licit_sorok:
            highest_bid_elem = licit_sorok[0].select_one("td:nth-child(2) strong")
            if highest_bid_elem:
                result["legmagasabb_licit"] = highest_bid_elem.get_text(strip=True)

        # --- Dinamikus adatsorok (li.data-row) ---
        for li in soup.select("li.data-row"):
            spans = li.find_all("span")
            if len(spans) >= 2:
                label = spans[0].get_text(strip=True).lower()
                value = spans[1].get_text(strip=True)
                if "helyrajzi" in label:
                    result["helyrajzi_szam"] = value
                elif "tulajdoni hányad" in label:
                    result["tulajdoni_hanyad"] = value
                elif "beköltözhető" in label:
                    result["bekoltozheto"] = value
                elif "jogi jelleg" in label:
                    result["jogi_jelleg"] = value
                elif "művelési ág" in label:
                    result["muvelesi_ag"] = value
                elif "fekvés" in label:
                    result["fekves"] = value
                elif "épület típusok" in label:
                    result["epulet_tipusok"] = value
                elif "besorolás" in label:
                    result["besorolas"] = value
                elif "sikeres árverés esetén sem törölhető jogok" in label:
                    result["nem_torolheto_jogok"] = value
                elif "bejegyzett földhasználat" in label:
                    result["foldhasznalat"] = value

        # --- Képek ---
        for img in soup.select(".desktop-gallery .img-button img, .mobile-gallery img"):
            src = img.get("src") or img.get("data-src")
            if src:
                if src.startswith("/"):
                    src = BASE_URL + src
                if src.startswith("http") and src not in result["kepek"]:
                    result["kepek"].append(src)

        log.info("HTML elemzés: cím='%s', telek=%s, szobák=%s, ár=%s",
                 result["cim"], result["telekmeret"], result["szobak_szama"], result["min_price"])
        return result

    except Exception as e:
        log.warning("HTML elemzési hiba %s: %s", detail_url, e)
        return result

# ── Adatok összefésülése (itt a HTML adatok dominálnak) ───────────────────────
def extract_combined(api_data: Dict, html_data: Dict) -> Dict:
    """
    Az API-ból csak azokat az adatokat használjuk, amelyeket a HTML-ből nem tudtunk kinyerni.
    """
    # Alapértelmezések a HTML-ből
    combined = html_data.copy()

    # Ár: ha a HTML-ben nincs, próbáljuk az API-ból
    if not combined.get("min_price") and not combined.get("starting_price"):
        api_price = parse_price(api_data.get("putUpPrice") or api_data.get("startPrice"))
        if api_price:
            combined["min_price"] = f"{api_price} Ft.-"
    if not combined.get("starting_price") and api_data.get("putUpPrice"):
        combined["starting_price"] = f"{api_data.get('putUpPrice')} Ft.-"

    # Tulajdoni hányad: ha HTML nem adta, API-ból
    if not combined.get("tulajdoni_hanyad"):
        combined["tulajdoni_hanyad"] = api_data.get("p_tulajdonihanyad") or api_data.get("ownershipShare")

    # Árverés vége: ha HTML nem adta
    if not combined.get("arveres_vege"):
        combined["arveres_vege"] = api_data.get("endDate") or api_data.get("auctionEnd")

    # Licit szám: ha HTML 0, API-ból
    if combined.get("licitek_szama", 0) == 0:
        combined["licitek_szama"] = api_data.get("bidCount", 0)

    return combined

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

    # Árverés vége ellenőrzés
    end_str = data.get("arveres_vege")
    if end_str and end_str != "N/A":
        try:
            # Próbáljuk ISO formátumra alakítani (pl. "2026.08.03. 16:00:00")
            end_date = datetime.strptime(end_str, "%Y.%m.%d. %H:%M:%S")
        except:
            try:
                end_date = datetime.fromisoformat(end_str.replace(' ', 'T'))
            except:
                end_date = None
        if end_date and end_date < datetime.now():
            log.debug("Lejárt árverés: %s", end_str)
            return False

    # Tulajdoni hányad
    if not share_accepted(data.get("tulajdoni_hanyad")):
        return False

    # Ár (minimum ár vagy kikiáltási ár)
    price_str = data.get("min_price") or data.get("starting_price")
    price = parse_price(price_str) if price_str else None
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
    """Összeállítja a részletes üzenetet és elküldi a szöveget + képeket."""
    # Segédfüggvény az érték formázására
    def fmt(label, value, suffix=""):
        if value and value not in ("N/A", "None", ""):
            return f"{label} {value}{suffix}"
        return None

    lines = []
    lines.append("🏠 *ÚJ MBVK ÁRVERÉS*")
    lines.append("")
    if data.get("cim"):
        lines.append(f"📍 *Cím:* {data['cim']}")
    if data.get("megye"):
        lines.append(f"🗺️ *Megye:* {data['megye']}")
    price_str = data.get("min_price") or data.get("starting_price")
    if price_str:
        lines.append(f"💰 *Minimum ár:* {price_str}")
    if data.get("legmagasabb_licit"):
        lines.append(f"📈 *Legmagasabb licit:* {data['legmagasabb_licit']}")
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
    if data.get("energia_tanusitvany"):
        lines.append(f"⚡ *Energia tanúsítvány:* {data['energia_tanusitvany']}")
    if data.get("tulajdoni_hanyad"):
        lines.append(f"📄 *Tulajdoni hányad:* {data['tulajdoni_hanyad']}")
    if data.get("bekoltozheto"):
        lines.append(f"🚪 *Beköltözhető:* {data['bekoltozheto']}")
    if data.get("helyrajzi_szam"):
        lines.append(f"🗺️ *Helyrajzi szám:* {data['helyrajzi_szam']}")
    if data.get("arveres_vege"):
        lines.append(f"⏳ *Árverés vége:* {data['arveres_vege']}")

    # Google Maps link
    if data.get("cim") and data["cim"] != "N/A":
        encoded_cim = urllib.parse.quote(data["cim"])
        lines.append(f"🗺️ [Térkép](https://www.google.com/maps/search/?api=1&query={encoded_cim})")
    lines.append("")
    lines.append(f"🔗 [Részletek]({data.get('url', '')})")

    text = "\n".join([l for l in lines if l])  # üres sorok megtartva

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
    log.info("MBVK Monitor indítás (HTML DOM alapú) – %s", datetime.now().isoformat())
    conn = init_db()

    session = requests.Session()
    session.headers.update(HEADERS)

    # 1. Lekérjük a beköltözhető árverések listáját az API-ból
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
        # 2. Részletes HTML letöltése és elemzése
        html_data = extract_from_html(session, url)
        # (API adatokat csak akkor használjuk, ha a HTML-ből hiányzik valami)
        # Itt most nem kérjük le az API részleteket, mert a HTML tartalmaz mindent.
        # Ha mégis kellene, api_detail(session, exec_id, auction_id) meghívható.
        data = html_data
        data["auction_id"] = auction_id
        data["url"] = url

        log.info("Feldolgozva: %s | megye=%s | hányad=%s | ár=%s | cím=%s | telek=%s",
                 auction_id, data.get("megye"), data.get("tulajdoni_hanyad"),
                 data.get("min_price") or data.get("starting_price"), data.get("cim"), data.get("telekmeret"))

        if passes_filters(data):
            log.info("✅ ÁTMENT: %s", auction_id)
            send_telegram(data)
            notified_count += 1
        else:
            log.info("❌ Nem ment át (hányad/ár/dátum/megye): %s", auction_id)

        mark_seen(conn, auction_id)
        time.sleep(2)   # kíméletes lekérés

    log.info("Kész – Új: %d / Értesítés: %d / Összes beköltözhető: %d",
             new_count, notified_count, len(items))
    conn.close()

if __name__ == "__main__":
    run()
