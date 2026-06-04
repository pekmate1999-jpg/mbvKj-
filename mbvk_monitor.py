#!/usr/bin/env python3
"""
MBVK Árverési Monitor v4 – Beköltözhető ingatlanok (moveln=true)
Szűrés: tulajdoni hányad (1/1 vagy 1/2+1/2) és max ár 1M Ft

Új funkciók (v4):
  1. Budapest-távolság: geopy alapján, Nominatim geocoder
  2. Google Térkép link a Telegram üzenetben
  3. Duplikáció-mentes értesítés: SQLite notified_at oszloppal
  4. Leírás kinyerése (max 200 kar.) + telekméret regex a leírásból

Telepítés:
  pip install requests geopy
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
from urllib.parse import quote_plus

import requests

# geopy opcionális – ha nincs telepítve, a távolság-funkció ki van kapcsolva
try:
    from geopy.geocoders import Nominatim
    from geopy.distance import geodesic
    GEOPY_OK = True
except ImportError:
    GEOPY_OK = False

# ── Település-megye szótár (opcionális) ───────────────────────────────────────
TELEPULES_MAP: Dict[str, str] = {}

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
BASE_URL  = "https://arveres.mbvk.hu"
API_BASE  = "https://arveres.mbvk.hu/publicapi"
DB_PATH   = "mbvk_v4.db"
MAX_PRICE = 1_000_000

# Budapest koordinátái (geocoding fallback)
BUDAPEST_COORDS = (47.4979, 19.0402)

# Megye szűrés KI (üres lista = minden megye jó)
COUNTIES: List[str] = []

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
    """
    Tábla sémája:
      auction_id  – elsődleges kulcs (duplikáció-védelem)
      created_at  – első megtalálás ideje
      notified_at – mikor ment ki a Telegram értesítés (NULL = nem ment)
    """
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")                # párhuzamos írás-véd.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS properties (
            auction_id  TEXT PRIMARY KEY,
            created_at  TEXT NOT NULL,
            notified_at TEXT
        )
    """)
    # Régi séma (v3) frissítése ha szükséges
    cols = {row[1] for row in conn.execute("PRAGMA table_info(properties)")}
    if "notified_at" not in cols:
        conn.execute("ALTER TABLE properties ADD COLUMN notified_at TEXT")
        log.info("DB séma frissítve: notified_at oszlop hozzáadva")
    return conn

def is_new(conn: sqlite3.Connection, auction_id: str) -> bool:
    """Igaz, ha az aukció még soha nem szerepelt az adatbázisban."""
    row = conn.execute(
        "SELECT 1 FROM properties WHERE auction_id = ?", (auction_id,)
    ).fetchone()
    return row is None

def already_notified(conn: sqlite3.Connection, auction_id: str) -> bool:
    """Igaz, ha már küldtünk értesítést erről az aukcióról."""
    row = conn.execute(
        "SELECT notified_at FROM properties WHERE auction_id = ?", (auction_id,)
    ).fetchone()
    if row is None:
        return False
    return row[0] is not None  # notified_at ki van töltve

def mark_seen(conn: sqlite3.Connection, auction_id: str):
    """Felveszi az aukciót az adatbázisba (értesítés nélkül)."""
    conn.execute(
        "INSERT OR IGNORE INTO properties (auction_id, created_at) VALUES (?, ?)",
        (auction_id, datetime.utcnow().isoformat()),
    )

def mark_notified(conn: sqlite3.Connection, auction_id: str):
    """Beállítja a notified_at időbélyeget – duplikáció-védelem."""
    now = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT INTO properties (auction_id, created_at, notified_at)
           VALUES (?, ?, ?)
           ON CONFLICT(auction_id) DO UPDATE SET notified_at = excluded.notified_at""",
        (auction_id, now, now),
    )

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
    """Számot keres egy szövegből (m² értékhez)."""
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

def extract_area_from_description(desc: str) -> Optional[float]:
    """
    Ha a strukturált mezőkből nem sikerült telekméret, megpróbálja
    kinyerni a leírás szövegéből.

    Kezelt minták (példák):
      "1349 m2 alapterületen"   → 1349
      "alapterülete: 96 m2"     → 96
      "96 m²"                   → 96
      "telekmeret: 1200 m2"     → 1200
    """
    if not desc:
        return None
    # Általános pattern: szám + m2 / m²
    patterns = [
        r"alapter[üu]let[e:]?\s*:?\s*([\d]+(?:[.,][\d]+)?)\s*m[²2]",
        r"([\d]+(?:[.,][\d]+)?)\s*m[²2]\s*alapter[üu]let",
        r"telekm[eé]ret[e:]?\s*:?\s*([\d]+(?:[.,][\d]+)?)\s*m[²2]",
        r"([\d]+(?:[.,][\d]+)?)\s*m[²2]\s*(?:alap|telek|ter[üu]let)",
    ]
    for pat in patterns:
        m = re.search(pat, desc, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", "."))
                if 10 <= val <= 100_000:   # ésszerű tartomány
                    return val
            except ValueError:
                continue
    return None

# ── Budapest-távolság (geopy) ─────────────────────────────────────────────────
# Geocoder példány – egyszer hozzuk létre (Nominatim rate-limit: 1 req/s)
_geocoder = Nominatim(user_agent="mbvk_monitor_v4") if GEOPY_OK else None
_geocache: Dict[str, Optional[float]] = {}   # telepules → km

def bp_distance_km(telepules: Optional[str], cim: Optional[str] = None) -> Optional[float]:
    """
    Visszaadja a Budapest-távolságot km-ben a settlement neve alapján.
    Cache-eli az eredményt, hogy ne hívja fel a Nominatim API-t kétszer
    ugyanarra a városra.
    Returns None ha geopy nincs telepítve vagy a geocoding sikertelen.
    """
    if not GEOPY_OK or not _geocoder:
        return None

    lookup = telepules or cim
    if not lookup:
        return None

    key = normalize(str(lookup))
    if key in _geocache:
        return _geocache[key]

    # Nominatim-nek "Veszprém, Magyarország" stb. formátum a legjobb
    query = f"{lookup}, Magyarország"
    try:
        time.sleep(1.1)   # Nominatim ToS: max 1 req/s
        location = _geocoder.geocode(query, timeout=10)
        if location:
            coords = (location.latitude, location.longitude)
            dist = round(geodesic(BUDAPEST_COORDS, coords).kilometers, 1)
            _geocache[key] = dist
            log.debug("Geocode OK: %s → %.1f km", lookup, dist)
            return dist
        else:
            log.debug("Geocode nem találta: %s", lookup)
            _geocache[key] = None
            return None
    except Exception as exc:
        log.warning("Geocode hiba (%s): %s", lookup, exc)
        _geocache[key] = None
        return None

# ── Google Térkép link ────────────────────────────────────────────────────────
def google_maps_url(cim: Optional[str]) -> Optional[str]:
    """Visszaad egy kattintható Google Térkép URL-t a megadott címre."""
    if not cim or cim == "N/A":
        return None
    encoded = quote_plus(cim)
    return f"https://www.google.com/maps/search/?api=1&query={encoded}"

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

# ── Adatkinyerés ──────────────────────────────────────────────────────────────
def extract(data: Dict) -> Dict:
    """Kinyeri az összes fontos mezőt a részlet API válaszából."""

    def get_from_attrs(group_key: str) -> Optional[str]:
        """propertyAttributes listából keres attributesGroup alapján."""
        attrs = data.get("propertyAttributes", [])
        for attr in attrs:
            if attr.get("attributesGroup") == group_key and attr.get("attribute"):
                return attr.get("attributeName")
        return None

    def g(*keys):
        """Keres a data dict-ben, majd a propertyAddress első elemében."""
        addr_list = data.get("propertyAddress", [])
        addr = addr_list[0] if isinstance(addr_list, list) and addr_list else {}
        for k in keys:
            if k in data and data[k] is not None:
                return data[k]
            if k in addr and addr[k] is not None:
                return addr[k]
        return None

    # propertyAddress első eleme
    addr_list = data.get("propertyAddress", [])
    addr = addr_list[0] if isinstance(addr_list, list) and addr_list else {}

    # Település és megye
    telepules = addr.get("settlement") or addr.get("city") or g("cityName", "addressCity")
    megye = g("county", "megye", "varmegye", "countyName")
    if not megye and telepules and normalize(str(telepules)) in TELEPULES_MAP:
        megye = TELEPULES_MAP[normalize(str(telepules))]

    # Cím összerakása a valódi JSON mezőnevekkel
    cim_parts = []
    irsz = addr.get("zipCode") or addr.get("postCode")
    if irsz:
        cim_parts.append(str(irsz))
    if telepules:
        cim_parts.append(str(telepules))
    utca = addr.get("nameOfPublicArea") or addr.get("street") or addr.get("addressLine")
    if utca:
        cim_parts.append(str(utca))

    if cim_parts:
        cim = " ".join(cim_parts)
    else:
        cim = (addr.get("formattedAddress")
               or g("address", "cim", "fullAddress", "ingatlanCim")
               or "N/A")

    # Tulajdoni hányad
    hanyad = g("p_tulajdonihanyad", "ownershipShare", "tulajdoniHanyad", "hanyad")

    # Árak
    kikialtas_ar      = parse_price(g("putUpPrice", "startPrice", "kikialtasiAr"))
    minimum_ar        = parse_price(g("minPrice", "minimumAr", "minimumBid"))
    legmagasabb_licit = parse_price(g("currentBid", "highestBid", "legmagasabbLicit"))
    price = legmagasabb_licit or minimum_ar or kikialtas_ar

    # Licitek száma
    licit_szam = g("bidCount", "licitekSzama")
    try:
        licit_szam = int(licit_szam) if licit_szam is not None else 0
    except (ValueError, TypeError):
        licit_szam = 0

    # ── LEÍRÁS (max 200 karakter) ─────────────────────────────────────────────
    leiras_full = g("description", "leiras", "propertyDescription") or ""
    leiras = leiras_full[:200].rstrip() if leiras_full else ""

    # ── TELEKMÉRET – strukturált mező, ha nincs: regex a leírásból ───────────
    telek_raw = g("area", "totalArea", "landArea", "builtArea", "alapterulet", "telekmeret")
    telek = parse_area(telek_raw)
    if telek is None and leiras_full:
        telek = extract_area_from_description(leiras_full)
        if telek:
            log.debug("Telekméret leírásból kinyerve: %.0f m²", telek)

    # Árverés vége
    arveres_vege = g("auctionEndDate", "endDate", "auctionEnd", "deadline", "befejezesDatuma")

    # Ft/m²
    ft_per_m2 = int(price / telek) if price and telek and telek > 0 else None

    return {
        "megye":             megye,
        "telepules":         telepules,
        "cim":               cim if cim else "N/A",
        "tulajdoni_hanyad":  hanyad,
        "bekoltözhető":      "igen",
        "price":             price,
        "legmagasabb_licit": legmagasabb_licit,
        "licitek_szama":     licit_szam,
        "telekmeret":        telek,
        "ft_per_m2":         ft_per_m2,
        "arveres_vege":      arveres_vege or "N/A",
        "leiras":            leiras,
        "url":               data.get("url", ""),
    }

# ── Szűrés ────────────────────────────────────────────────────────────────────
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

    # Lejárt árverés kizárása
    end_date_str = data.get("arveres_vege")
    if end_date_str and end_date_str != "N/A":
        end_date = None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                end_date = datetime.strptime(end_date_str.replace("T", " ").split(".")[0], fmt)
                break
            except ValueError:
                continue
        if end_date and end_date < datetime.now():
            log.debug("Lejárt árverés: %s", end_date_str)
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
        log.warning("Telegram nincs beállítva (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID hiányzik)")
        return

    cim      = data.get("cim")
    price    = data.get("price")
    legh     = data.get("legmagasabb_licit")
    licit_n  = data.get("licitek_szama", 0)
    telek    = data.get("telekmeret")
    ft_m2    = data.get("ft_per_m2")
    hanyad   = data.get("tulajdoni_hanyad")
    end      = data.get("arveres_vege")
    leiras   = data.get("leiras", "")
    telepules = data.get("telepules")

    # Budapest-távolság (geopy)
    dist_km = bp_distance_km(telepules, cim)

    # Google Térkép link
    maps_url = google_maps_url(cim)

    # Formázás
    price_str  = f"{price:,} Ft".replace(",", " ")     if price  else None
    legh_str   = f"{legh:,} Ft".replace(",", " ")      if legh   else None
    licit_str  = str(licit_n)                           if licit_n > 0 else None
    telek_str  = f"{telek:.0f} m²"                     if telek  else None
    ft_m2_str  = f"{ft_m2:,} Ft/m²".replace(",", " ") if ft_m2  else None
    hanyad_str = hanyad                                 if hanyad else None
    end_str    = end if end and end != "N/A"            else None
    dist_str   = f"{dist_km:.0f} km"                   if dist_km is not None else None

    lines = ["🏠 *ÚJ MBVK TALÁLAT*", ""]

    if cim and cim != "N/A":
        lines.append(f"📍 *Cím:* {cim}")
    if dist_str:
        lines.append(f"🗺 *Budapest-távolság:* {dist_str}")
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
    if leiras:
        lines.append(f"\n📝 _{leiras}_")

    lines.append("")
    lines.append(f"🔗 [Részletek az MBVK oldalon]({data.get('url', '')})")
    if maps_url:
        lines.append(f"🗺 [Google Térkép]({maps_url})")

    text = "\n".join(lines)

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":                  TELEGRAM_CHAT_ID,
                "text":                     text,
                "parse_mode":               "Markdown",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("✉️  Telegram elküldve: %s", data.get("auction_id"))
        else:
            log.error("Telegram hiba: %s %s", resp.status_code, resp.text[:300])
    except Exception as exc:
        log.error("Telegram küldési hiba: %s", exc)

# ── Főprogram ─────────────────────────────────────────────────────────────────
def run():
    load_telepules_map()
    log.info("MBVK Monitor v4 indítás – %s", datetime.now().isoformat())
    if not GEOPY_OK:
        log.warning("geopy nincs telepítve – Budapest-távolság nem elérhető. "
                    "Telepítés: pip install geopy")

    conn = init_db()
    session = requests.Session()
    session.headers.update(HEADERS)

    # ── Lista lekérése (csak beköltözhető tételek) ────────────────────────────
    items: List[Dict] = []
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
        auction_id = str(item.get("auctionItemId") or item.get("id") or "")
        if not exec_id or not auction_id:
            continue

        # ── Duplikáció-védelem (kétszintű) ────────────────────────────────────
        # 1. szint: már ismert ÉS értesített → skip
        if already_notified(conn, auction_id):
            log.debug("Már értesítve: %s", auction_id)
            continue

        url = f"{BASE_URL}/arveres-reszletek/{exec_id}/{auction_id}"

        # 2. szint: már látott, de nem értesített (pl. szűrőn nem ment át) → skip
        if not is_new(conn, auction_id):
            log.debug("Már ismert (nem értesített): %s", auction_id)
            continue

        new_count += 1
        detail = api_detail(session, exec_id, auction_id)
        if not detail:
            mark_seen(conn, auction_id)
            continue

        data = extract(detail)
        data["auction_id"] = auction_id
        data["url"] = url

        log.info(
            "Feldolgozva: %s | %s | hányad=%s | ár=%s | telek=%s",
            auction_id,
            data.get("cim", "N/A"),
            data.get("tulajdoni_hanyad"),
            data.get("price"),
            data.get("telekmeret"),
        )

        if passes_filters(data):
            log.info("✅ ÁTMENT: %s", auction_id)
            send_telegram(data)
            mark_notified(conn, auction_id)   # értesítés ténye rögzítve
            notified_count += 1
        else:
            log.info("❌ Nem ment át (szűrő): %s", auction_id)
            mark_seen(conn, auction_id)       # látott, de NEM értesített

        time.sleep(1)

    log.info(
        "Kész – Új: %d / Értesítés: %d / Összes beköltözhető: %d",
        new_count, notified_count, len(items),
    )
    conn.close()

if __name__ == "__main__":
    run()
