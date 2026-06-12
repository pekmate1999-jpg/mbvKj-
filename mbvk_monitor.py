#!/usr/bin/env python3
"""
MBVK Árverési Monitor v7.2.1 – Beköltözhető ingatlanok (moveln=true)
Vármegye, javított szakaszok, négyzetzméter javitások, vezetékjog, telekméret hibák további javítása
Kategorizált Telegram üzenet formátummal + Google Naptár integrációval.
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
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Tuple
from urllib.parse import quote_plus

import requests

# geopy opcionális – ha nincs telepítve, a távolság-funkció ki van kalkulálva
try:
    from geopy.geocoders import Nominatim
    from geopy.distance import geodesic
    GEOPY_OK = True
except ImportError:
    GEOPY_OK = False

# ── Település-megye szótár (opcionális) ───────────────────────────────────────
TELEPULES_MAP: Dict[str, str] = {}

def load_telepules_map():
    """
    Betölti a telepulesek.csv-t egy {normalize(helység) -> megye} szótárba.
    A CSV első 3 sora (üres + fejléc + törött 2. fejlécsor) skip-elésre kerül.
    A "fõváros" (latin-2 hibás kódolású) → "főváros" normalizálva lesz.
    Budapest kerületei (Budapest 01. ker. stb.) → "Budapest (főváros)".
    """
    try:
        with open("telepulesek.csv", mode="r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=";")
            skip = 3   # üres sor + "Helység;Megye..." fejléc + törött "megnevezése;" sor
            for row in reader:
                if skip > 0:
                    skip -= 1
                    continue
                if len(row) < 2:
                    continue
                helyseg = row[0].strip()
                megye_raw = row[1].strip()
                if not helyseg or not megye_raw:
                    continue
                # latin-2 → unicode javítás a "fõváros" esetére
                megye_clean = megye_raw.replace("fõváros", "főváros").replace("fovaros", "főváros")
                # Budapest kerületek egységesítése
                if helyseg.lower().startswith("budapest") and "ker." in helyseg.lower():
                    megye_clean = "Budapest (főváros)"
                elif helyseg.lower() == "budapest":
                    megye_clean = "Budapest (főváros)"
                TELEPULES_MAP[normalize(helyseg)] = megye_clean
        log.info("Település mappa betöltve: %d elem", len(TELEPULES_MAP))
    except FileNotFoundError:
        log.warning("telepulesek.csv nem található – megye kiegészítés nem működik")
    except Exception as e:
        log.error("Hiba a CSV betöltésekor: %s", e)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
BASE_URL  = "https://arveres.mbvk.hu"
API_BASE  = "https://arveres.mbvk.hu/publicapi"
DB_PATH   = "mbvk_v8.db"
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

# ── Segédfüggvények ───────────────────────────────────────────────────────────
def normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))

def parse_hungarian_float(s: str) -> Optional[float]:
    """
    Magyar számformátum (pl. '1.590' -> 1590.0, '1,5' -> 1.5, '1 000,5' -> 1000.5)
    átalakítása float-ra.
    """
    if not s:
        return None
    # Szóközök eltávolítása
    s = s.strip().replace(" ", "")
    # Pont (ezres elválasztó) eltávolítása
    s = s.replace(".", "")
    # Vessző (tizedes) átalakítása ponttá
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None

def parse_price(val) -> Optional[int]:
    if val is None:
        return None
    digits = re.sub(r"[^\d]", "", str(val))
    return int(digits) if digits else None

def parse_area(val) -> Optional[float]:
    """
    Kinyeri az első előforduló számot (magyar formátumban) a szöveges értékből.
    Pl. '1.590 m²' -> 1590.0, '65 m2' -> 65.0
    """
    if val is None:
        return None
    s = str(val).strip()
    # Megkeressük az első számot (tizedesvesszőt és ezres pontot is kezelve)
    m = re.search(r"([\d]+(?:[.,][\d]+)?)", s)
    if m:
        return parse_hungarian_float(m.group(1))
    return None

def escape_markdown(text: str) -> str:
    """
    Specialis karakterek levédése Telegram legacy Markdown modhoz.
    Hagyományos Markdown módban csak a *, _, [, és ` karaktereket kell védeni.
    """
    if not text:
        return ""
    escape_chars = r"_*[`"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

def generate_gcal_url(title, date_str, location="", details=""):
    """
    Készít egy Google Calendar URL-t az MBVK dátumaiból.
    Átváltja UTC-vé és ctz paramétert ad hozzá a mobilos appokhoz.
    """
    if not date_str or date_str == "N/A":
        return None

    try:
        import zoneinfo
        bp_tz = zoneinfo.ZoneInfo("Europe/Budapest")
    except Exception:
        bp_tz = None

    try:
        # Keresünk dátum-idő formátumot (pl. 2024.05.12. 10:00 vagy 2024-05-12 10:00)
        matches = re.findall(r'(\d{4})[-.]\s*(\d{2})[-.]\s*(\d{2})[^\d]*(\d{2}):(\d{2})', date_str)
        
        if not matches:
            return None

        # Év, Hónap, Nap, Óra, Perc kinyerése
        y, m, d, h, minute = matches[0]
        start_dt = datetime(int(y), int(m), int(d), int(h), int(minute))
        
        # Alapértelmezetten 1 órás eseményt csinálunk a naptárban
        end_dt = start_dt + timedelta(hours=1)

        # UTC átváltás a mobilos appok miatt
        if bp_tz:
            start_dt = start_dt.replace(tzinfo=bp_tz)
            end_dt = end_dt.replace(tzinfo=bp_tz)
            start_utc = start_dt.astimezone(timezone.utc)
            end_utc = end_dt.astimezone(timezone.utc)
            start_str = start_utc.strftime("%Y%m%dT%H%M00Z")
            end_str = end_utc.strftime("%Y%m%dT%H%M00Z")
        else:
            start_str = start_dt.strftime("%Y%m%dT%H%M00")
            end_str = end_dt.strftime("%Y%m%dT%H%M00")

        # URL összeállítása
        url = f"https://calendar.google.com/calendar/render?action=TEMPLATE"
        url += f"&text={urllib.parse.quote(title)}"
        url += f"&dates={start_str}/{end_str}"
        url += f"&ctz=Europe/Budapest"
        
        if location and location != "N/A":
            url += f"&location={urllib.parse.quote(location)}"
        if details:
            url += f"&details={urllib.parse.quote(details)}"
            
        return url
    except Exception as e:
        log.warning(f"Nem sikerült a naptár link generálása MBVK-hoz: {e}")
        return None

# ── Telek- és épületméret kinyerése a leírásból ───────────────────────────────
def parse_sizes_from_description(desc: str) -> Tuple[Optional[float], Optional[float]]:
    if not desc:
        return None, None

    desc_lower = desc.lower()
    pattern = re.compile(
        r"(\d+(?:[.,]\d+)?)\s*(?:m[²2]|nm|négyzetméter)",
        re.IGNORECASE
    )
    
    telek_matches = []
    epulet_candidates = []
    
    # Telek kulcsszavak: "telek", "udvar", "ingatlan" (a "terület" túl általános, elhagyjuk)
    telek_kws = ["telek", "udvar", "ingatlan"]
    
    epulet_prior_kws = [
        "lakóház", "lakás", "épület", "hasznos alapterület",
        "alapterület", "alapterülete"
    ]
    
    area_blacklist = [
        "vezetékjog", "szolgalmi jog", "terhel", "terheli",
        "bejegyzett", "engedélyszám", "javára", "vázrajz",
    ]
    
    for match in pattern.finditer(desc_lower):
        num_str = match.group(1)
        val = parse_hungarian_float(num_str)
        if val is None:
            continue
        if val < 5 or val > 250_000:
            continue
        
        start = match.start()
        end = match.end()
        
        # Feketelista ellenőrzés (szűk ablak)
        bl_start = max(0, start - 40)
        bl_end   = min(len(desc_lower), end + 40)
        bl_ctx   = desc_lower[bl_start:bl_end]
        if any(kw in bl_ctx for kw in area_blacklist):
            continue
        
        # Tágabb kontextus (kulcsszavak kereséséhez)
        ctx_start = max(0, start - 80)
        ctx_end   = min(len(desc_lower), end + 80)
        context   = desc_lower[ctx_start:ctx_end]
        
        # --- Először épület kulcsszavak keresése ---
        best_dist = None
        for kw in epulet_prior_kws:
            kw_pos = context.find(kw)
            if kw_pos != -1:
                abs_kw_pos = ctx_start + kw_pos
                dist = abs(abs_kw_pos - start)
                if best_dist is None or dist < best_dist:
                    best_dist = dist
        
        # Ha találtunk épület kulcsszót, akkor ez épület méret, és nem telek
        if best_dist is not None:
            epulet_candidates.append((val, best_dist))
            continue   # <- fontos: nem megyünk tovább a telek ellenőrzésre
        
        # --- Ha nincs épület kulcsszó, akkor telek-e? ---
        is_telek = any(kw in context for kw in telek_kws)
        if is_telek:
            telek_matches.append(val)
    
    telek = max(telek_matches) if telek_matches else None
    
    epulet = None
    if epulet_candidates:
        epulet_candidates.sort(key=lambda x: (x[1], -x[0]))
        epulet = epulet_candidates[0][0]
    
    return telek, epulet

# ── Budapest-távolság (geopy) ─────────────────────────────────────────────────
_geocoder = Nominatim(user_agent="mbvk_monitor_v4") if GEOPY_OK else None
_geocache: Dict[str, Optional[float]] = {}

def bp_distance_km(telepules: Optional[str], cim: Optional[str] = None) -> Optional[float]:
    if not GEOPY_OK or not _geocoder:
        return None

    lookup = telepules or cim
    if not lookup:
        return None

    key = normalize(str(lookup))
    if key in _geocache:
        return _geocache[key]

    query = f"{lookup}, Magyarország"
    try:
        time.sleep(1.1)
        location = _geocoder.geocode(query, timeout=10)
        if location:
            coords = (location.latitude, location.longitude)
            dist = round(geodesic(BUDAPEST_COORDS, coords).kilometers, 1)
            _geocache[key] = dist
            return dist
        else:
            _geocache[key] = None
            return None
    except Exception as exc:
        log.warning("Geocode hiba (%s): %s", lookup, exc)
        _geocache[key] = None
        return None

# ── Google Térkép link ────────────────────────────────────────────────────────
def google_maps_url(cim: Optional[str]) -> Optional[str]:
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

def _parse_dt(s: str) -> Optional[datetime]:
    """ISO/pont formátumú dátumstring -> datetime, vagy None."""
    if not s or s == "N/A":
        return None
    s2 = s.split("T")[0].split()[0].replace(".", "-").strip("-")
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(s2, fmt)
        except ValueError:
            continue
    return None

def calculate_phase_ends(
    arveres_kezdete: str,
    arveres_vege: str,
    licit_szam: int = 0,
    minimum_ar: Optional[int] = None,
    kikialtas_ar: Optional[int] = None,
) -> List[str]:
    """
    MBVK szakasz-határok pontos kiszámítása (Vht. 145/B. §).
    """
    start_dt = _parse_dt(arveres_kezdete)
    end_dt   = _parse_dt(arveres_vege)

    if not start_dt or not end_dt:
        return []

    total_sec = (end_dt - start_dt).total_seconds()
    if total_sec <= 0:
        return []

    time_part = "15:00:00"
    if arveres_vege and "T" in arveres_vege:
        time_part = arveres_vege.split("T")[1]
    elif arveres_vege and " " in arveres_vege:
        time_part = arveres_vege.split(" ")[1]

    def fmt(dt: "datetime") -> str:
        return f"{dt.strftime('%Y-%m-%d')}T{time_part}"

    phase_sec = total_sec / 3
    phase1_end = start_dt + timedelta(seconds=phase_sec)
    phase2_end = start_dt + timedelta(seconds=phase_sec * 2)
    phase3_end = end_dt  

    if licit_szam and minimum_ar and kikialtas_ar and minimum_ar >= kikialtas_ar:
        log.debug("Árverés lezárult (min_ar >= kiki_ar): csak 1 dátum")
        return [fmt(phase3_end)]

    return [fmt(phase1_end), fmt(phase2_end), fmt(phase3_end)]

def get_current_stage(phase_ends):
    if not phase_ends:
        return 1
    now_dt = datetime.now()
    parsed_ends = sorted(filter(None, [_parse_dt(e) for e in phase_ends]))
    for idx, ph_end in enumerate(parsed_ends, start=1):
        if now_dt <= ph_end:
            return idx
    return len(parsed_ends)

def generate_timeline(
    kezdete: str,
    vege: str,
    kiki_ar: Optional[int],
    min_ar: Optional[int],
    phase_ends: Optional[List[str]] = None,
) -> str:
    now_dt = datetime.now()
    start_dt = _parse_dt(kezdete)

    if phase_ends:
        parsed_ends = sorted(filter(None, [_parse_dt(e) for e in phase_ends]))
        total_end_dt = parsed_ends[-1] if parsed_ends else _parse_dt(vege)
    else:
        total_end_dt = _parse_dt(vege)

    if not start_dt or not total_end_dt:
        return "`[░░░|░░░|░░░]`\n_Ismeretlen időszak_"

    total_sec = (total_end_dt - start_dt).total_seconds()
    if total_sec <= 0:
        return "`[███|███|███]`\n_Lezárult_"

    elapsed  = (now_dt - start_dt).total_seconds()
    progress = max(0.0, min(1.0, elapsed / total_sec))

    filled = int(progress * 9)
    blocks = ["█" if i < filled else "░" for i in range(9)]
    bar_str = f"`[{''.join(blocks[0:3])}|{''.join(blocks[3:6])}|{''.join(blocks[6:9])}]` {int(progress * 100)}%"

    if phase_ends:
        current_stage = len(parsed_ends) 
        for idx, ph_end in enumerate(parsed_ends, start=1):
            if now_dt <= ph_end:
                current_stage = idx
                break
    else:
        if progress < 0.333:
            current_stage = 1
        elif progress < 0.666:
            current_stage = 2
        else:
            current_stage = 3

    ratio_text = ""
    if kiki_ar and min_ar and kiki_ar > 0:
        ratio_text = f" ({int(min_ar / kiki_ar * 100)}%)"

    return f"{bar_str}\n*{current_stage}. szakasz{ratio_text}*"

def calculate_phase_prices(
    kikialtas_ar: Optional[int],
    ar_tipus: str = "altalanos",
) -> Optional[Tuple[int, int, int]]:
    if not kikialtas_ar or kikialtas_ar <= 0:
        return None

    if ar_tipus == "jelzalog":
        return (kikialtas_ar, kikialtas_ar, kikialtas_ar)

    stage1 = int(kikialtas_ar * 0.90)
    stage2 = int(kikialtas_ar * 0.70)
    stage3 = int(kikialtas_ar * 0.50)
    return (stage1, stage2, stage3)

def format_phase_prices(prices: Tuple[int, int, int]) -> str:
    if prices[0] == prices[1] == prices[2]:
        return f"{prices[0]:,} Ft (minden szakaszban)".replace(",", " ")
    return f"{prices[0]:,} Ft / {prices[1]:,} Ft / {prices[2]:,} Ft".replace(",", " ")

def calculate_phase_ft_per_m2(phase_prices: Tuple[int, int, int], ref_area: Optional[float]) -> Optional[Tuple[int, int, int]]:
    if not ref_area or ref_area <= 0:
        return None
    return (
        int(phase_prices[0] / ref_area),
        int(phase_prices[1] / ref_area),
        int(phase_prices[2] / ref_area)
    )

def format_phase_ft_per_m2(ft_per_m2: Tuple[int, int, int]) -> str:
    return f"{ft_per_m2[0]:,} Ft/m² / {ft_per_m2[1]:,} Ft/m² / {ft_per_m2[2]:,} Ft/m²".replace(",", " ")

def format_phase_remaining_days(phase_ends: Optional[List[str]]) -> Optional[str]:
    if not phase_ends:
        return None
    now = datetime.now()
    days_list = []
    for end_str in phase_ends:
        end_dt = _parse_dt(end_str)
        if end_dt is None:
            days_list.append("?")
            continue
        delta = (end_dt - now).days
        if delta < 0:
            days_list.append("lejárt")
        else:
            days_list.append(f"{delta} nap")
    return ", ".join(days_list) if days_list else None

# ── Adatbázis (SQLite) ────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS properties (
            auction_id  TEXT PRIMARY KEY,
            created_at  TEXT,
            notified_at TEXT,
            price       INTEGER
        )
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(properties)")}
    
    if "created_at" not in cols:
        conn.execute("ALTER TABLE properties ADD COLUMN created_at TEXT")
        log.info("DB séma frissítve: created_at oszlop hozzáadva")
    if "notified_at" not in cols:
        conn.execute("ALTER TABLE properties ADD COLUMN notified_at TEXT")
        log.info("DB séma frissítve: notified_at oszlop hozzáadva")
    if "price" not in cols:
        conn.execute("ALTER TABLE properties ADD COLUMN price INTEGER")
        log.info("DB séma frissítve: price oszlop hozzáadva")
    if "licit_szam" not in cols:
        conn.execute("ALTER TABLE properties ADD COLUMN licit_szam INTEGER")
        log.info("DB séma frissítve: licit_szam oszlop hozzáadva")
    if "arveres_vege" not in cols:
        conn.execute("ALTER TABLE properties ADD COLUMN arveres_vege TEXT")
        log.info("DB séma frissítve: arveres_vege oszlop hozzáadva")
    if "current_szakasz" not in cols:
        conn.execute("ALTER TABLE properties ADD COLUMN current_szakasz INTEGER")
        log.info("DB séma frissítve: current_szakasz oszlop hozzáadva")

    return conn

# ── Adatkinyerés ──────────────────────────────────────────────────────────────
def extract(data: Dict) -> Dict:
    def g(*keys):
        addr_list = data.get("propertyAddress", [])
        addr = addr_list[0] if isinstance(addr_list, list) and addr_list else {}
        for k in keys:
            if k in data and data[k] is not None:
                return data[k]
            if k in addr and addr[k] is not None:
                return addr[k]
        return None

    addr_list = data.get("propertyAddress", [])
    addr = addr_list[0] if isinstance(addr_list, list) and addr_list else {}

    telepules = addr.get("settlement") or addr.get("city") or g("cityName", "addressCity")
    megye = g("county", "megye", "varmegye", "countyName")
    if not megye and telepules and normalize(str(telepules)) in TELEPULES_MAP:
        megye = TELEPULES_MAP[normalize(str(telepules))]

    cim_parts = []
    irsz = addr.get("zipCode") or addr.get("postCode")
    if not megye and irsz and re.match(r"^1\d{3}$", str(irsz).strip()):
        megye = "Budapest (főváros)"
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
        cim = (addr.get("formattedAddress") or g("address", "cim", "fullAddress", "ingatlanCim") or "N/A")

    hanyad = g("p_tulajdonihanyad", "ownershipShare", "tulajdoniHanyad", "hanyad")

    kikialtas_ar      = parse_price(g("putUpPrice", "startPrice", "kikialtasiAr"))
    minimum_ar        = parse_price(g("minPrice", "minimumAr", "minimumBid"))
    legmagasabb_licit = parse_price(g("currentBid", "highestBid", "legmagasabbLicit"))

    licit_szam = g("bidCount", "licitekSzama")
    try:
        licit_szam = int(licit_szam) if licit_szam is not None else 0
    except (ValueError, TypeError):
        licit_szam = 0

    leiras_full = g("description", "leiras", "propertyDescription") or ""
    leiras = leiras_full[:200].rstrip() if leiras_full else ""

    leiras_full_lower = leiras_full.lower()
    is_lakott = "lakottan" in leiras_full_lower or "haszonélvezet" in leiras_full_lower

    if kikialtas_ar and minimum_ar and kikialtas_ar > 0:
        ratio = minimum_ar / kikialtas_ar
        if ratio >= 0.95:
            ar_tipus = "jelzalog"       
        elif ratio >= 0.75:
            ar_tipus = "altalanos"      
        else:
            ar_tipus = "lakoingatan"    
    else:
        ar_tipus = "altalanos"          

    if legmagasabb_licit and legmagasabb_licit > 0:
        price = legmagasabb_licit
    elif minimum_ar and minimum_ar > 0:
        price = minimum_ar
    elif kikialtas_ar and kikialtas_ar > 0:
        price = int(kikialtas_ar * 0.90)    
    else:
        price = None

    telek_api   = parse_area(g("landArea",  "totalArea", "telekmeret", "terulet"))
    epulet_api  = parse_area(g("builtArea", "area",      "alapterulet", "livingArea"))

    telek_leiras: Optional[float]  = None
    epulet_leiras: Optional[float] = None

    if telek_api is None or epulet_api is None:
        t_desc, e_desc = parse_sizes_from_description(leiras_full)
        if telek_api is None and t_desc is not None:
            telek_leiras = t_desc
        if epulet_api is None and e_desc is not None:
            epulet_leiras = e_desc

    telek_meret  = telek_api  or telek_leiras
    epulet_meret = epulet_api or epulet_leiras

    if telek_meret is not None and telek_meret > 0:
        ref_area = telek_meret
    elif epulet_meret is not None and epulet_meret > 0:
        ref_area = epulet_meret
    else:
        ref_area = None

    arveres_vege = g("auctionEndDate", "endDate", "auctionEnd", "deadline", "befejezesDatuma")
    arveres_kezdete = g("auctionStartDate", "startDate", "auctionStart", "kezdet", "kibocsatasDatuma")

    return {
        "megye":              megye,
        "telepules":          telepules,
        "cim":                cim,
        "tulajdoni_hanyad":   hanyad,
        "is_lakott":          is_lakott,
        "ar_tipus":           ar_tipus,
        "bekoltozheto":       "nem (lakott)" if is_lakott else "igen",
        "price":              price,
        "kikialtas_ar":       kikialtas_ar,
        "minimum_ar":         minimum_ar,
        "legmagasabb_licit":  legmagasabb_licit,
        "licitek_szama":      licit_szam,
        "telek_meret":        telek_meret,
        "telek_forras":       "api" if telek_api else ("leiras" if telek_leiras else None),
        "epulet_meret":       epulet_meret,
        "epulet_forras":      "api" if epulet_api else ("leiras" if epulet_leiras else None),
        "ref_area":           ref_area,
        "arveres_kezdete":    arveres_kezdete or "N/A",
        "arveres_vege":       arveres_vege or "N/A",
        "leiras":             leiras,
        "url":                data.get("url", ""),
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

    end_date_str = data.get("arveres_vege")
    if end_date_str and end_date_str != "N/A":
        end_date = _parse_dt(end_date_str)
        if end_date and end_date < datetime.now():
            return False

    if not share_accepted(data.get("tulajdoni_hanyad")):
        return False

    price = data.get("price")
    if price is None or price > MAX_PRICE:
        return False

    return True

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(data: Dict, indok: str = "új"):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram nincs beállítva (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID hiányzik)")
        return

    # Naptár linkhez mentjük a NYERS szövegeket mielőtt escape-elnénk
    cim_raw = data.get("cim", "N/A")
    url_raw = data.get("url", "")
    
    # Alapadatok escape-elése (Markdown védelméhez)
    cim      = escape_markdown(cim_raw)
    price    = data.get("price")
    legh     = data.get("legmagasabb_licit")
    licit_n  = data.get("licitek_szama", 0)
    hanyad   = escape_markdown(data.get("tulajdoni_hanyad", "")) if data.get("tulajdoni_hanyad") else None
    end      = data.get("arveres_vege")
    leiras   = escape_markdown(data.get("leiras", ""))
    telepules = data.get("telepules")

    telek_m   = data.get("telek_meret")
    telek_f   = data.get("telek_forras")
    epulet_m  = data.get("epulet_meret")
    epulet_f  = data.get("epulet_forras")
    ref_area  = data.get("ref_area")

    dist_km = bp_distance_km(telepules, data.get("cim"))
    maps_url = google_maps_url(data.get("cim"))

    def fmt_area(val: Optional[float], forras: Optional[str]) -> Optional[str]:
        if val is None:
            return None
        s = f"{val:,.0f} m²".replace(",", " ")
        if forras == "leiras":
            s += " _(leírásból)_"
        return s

    price_str  = f"{price:,} Ft".replace(",", " ")     if price    else None
    legh_str   = f"{legh:,} Ft".replace(",", " ")      if legh     else None
    licit_str  = str(licit_n)                            if licit_n > 0 else None
    telek_str  = fmt_area(telek_m, telek_f)
    epulet_str = fmt_area(epulet_m, epulet_f)

    def fmt_date(s: Optional[str]) -> Optional[str]:
        if not s or s == "N/A":
            return None
        return s.replace("T", " ")[:16]
        
    end_str    = fmt_date(end)
    dist_str   = f"{dist_km:.0f} km"                   if dist_km is not None else None

    # Visszaszámlálás / Szakaszok vége link generálása
    end_gcal_url = None
    if end_str:
        end_gcal_url = generate_gcal_url(
            title=f"MBVK Árverés Vége: {cim_raw}",
            date_str=end_str,
            location=cim_raw,
            details=f"Részletek: {url_raw}"
        )

    timeline = generate_timeline(
        data.get("arveres_kezdete", ""),
        data.get("arveres_vege", ""),
        data.get("kikialtas_ar"),
        data.get("minimum_ar"),
        data.get("phase_end_dates") or None,
    )

    # Szakasz árak és szakaszonkénti ft/m²
    phase_prices = calculate_phase_prices(data.get("kikialtas_ar"), ar_tipus=data.get("ar_tipus", "altalanos"))
    phase_prices_str = format_phase_prices(phase_prices) if phase_prices else None
    phase_ft_per_m2 = calculate_phase_ft_per_m2(phase_prices, ref_area) if phase_prices and ref_area else None
    phase_ft_per_m2_str = format_phase_ft_per_m2(phase_ft_per_m2) if phase_ft_per_m2 else None
    phase_remaining = format_phase_remaining_days(data.get("phase_end_dates"))

    INDOK_EMOJI = {
        "új":        "🆕",
        "új licit":  "🔔",
        "új dátum":  "📅",
        "árcsökkenés": "📉",
        "szakaszváltás": "🔄",
    }
    emoji = INDOK_EMOJI.get(indok, "🏠")
    
    # --- Kategorizált üzenet összeállítása ---
    lines = [f"{emoji} *MBVK TALÁLAT – {indok.upper()}*", ""]

    # 1. Elhelyezkedés és Alapadatok
    megye_str = escape_markdown(data.get("megye", "") or "")
    lines.append("🌍 *1. Elhelyezkedés és Alapadatok*")
    if cim and cim != "N/A":
        lines.append(f"📍 *Cím:* {cim}")
    if megye_str:
        lines.append(f"🏛 *Megye:* {megye_str}")
    if dist_str:
        lines.append(f"🗺 *Budapest-távolság:* {dist_str}")
    lines.append("")

    # 2. Az Ingatlan és a Telek Jellemzői
    lines.append("🏠 *2. Az Ingatlan és a Telek Jellemzői*")
    if telek_str:
        lines.append(f"🏕 *Telekméret:* {telek_str}")
    if epulet_str:
        lines.append(f"🏚 *Épület alapterülete:* {epulet_str}")
    lines.append(f"🚪 *Beköltözhető:* {data.get('bekoltozheto', 'igen')}")
    lines.append("")

    # 3. Pénzügyi Információk
    lines.append("💰 *3. Pénzügyi Információk*")
    if price_str:
        lines.append(f"💵 *Jelenlegi ár:* {price_str}")
    if phase_prices_str:
        lines.append(f"💲 *Szakasz árak:* {phase_prices_str}")
    if phase_ft_per_m2_str:
        lines.append(f"📉 *Ft/m²:* {phase_ft_per_m2_str}")
    if legh_str:
        lines.append(f"📈 *Legmagasabb licit:* {legh_str}")
    lines.append("")

    # 4. Jogi és Árverési Státusz
    lines.append("⚖️ *4. Jogi és Árverési Státusz*")
    if hanyad:
        lines.append(f"📄 *Tulajdoni hányad:* {hanyad}")
    if phase_remaining:
        lines.append(f"⏳ *Szakaszok vége:* {phase_remaining}")
        
    # --- Dátum és naptár link ---
    if end_str:
        if end_gcal_url:
            lines.append(f"📅 *Árverés vége:* [{end_str}]({end_gcal_url})")
        else:
            lines.append(f"📅 *Árverés vége:* {end_str}")
            
    if licit_str:
        lines.append(f"🔄 *Licitek száma:* {licit_str}")
    lines.append(f"📊 *Státusz:* {timeline}")
    lines.append("")

    # Egyéb kiegészítő információk és linkek
    if leiras:
        lines.append(f"📝 *Leírás:*\n_{leiras}_")
        lines.append("")
        
    # ── LEVÁGOTT RÉSZ JAVÍTÁSA ÉS BEFEJEZÉSE ──
    if url_raw:
        lines.append(f"🔗 [Részletek az MBVK oldalán]({url_raw})")
    if maps_url:
        lines.append(f"🗺 [Google Térkép]({maps_url})")

    text = "\n".join(lines)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.error("Telegram küldési hiba: %s\nÜzenet: %s", e, text)


# ── Fő futtatási blokk (Pótolva, hogy teljes legyen a script) ───────────────
def main():
    log.info("MBVK Árverési Monitor indul...")
    load_telepules_map()
    db = init_db()
    session = requests.Session()
    session.headers.update(HEADERS)

    items = api_list(session)
    for row in items:
        exec_id = row.get("executiveId")
        auction_id = row.get("id")
        if not exec_id or not auction_id:
            continue
        
        # Adatbázis ellenőrzés
        cursor = db.execute("SELECT price FROM properties WHERE auction_id = ?", (str(auction_id),))
        record = cursor.fetchone()

        detail_data = api_detail(session, exec_id, auction_id)
        if not detail_data:
            continue

        # Szakasz kalkuláció beágyazása
        detail_data["phase_end_dates"] = calculate_phase_ends(
            detail_data.get("auctionStartDate", ""),
            detail_data.get("auctionEndDate", ""),
            detail_data.get("bidCount", 0),
            parse_price(detail_data.get("minPrice")),
            parse_price(detail_data.get("putUpPrice"))
        )

        data = extract(detail_data)

        # Szűrő
        if not passes_filters(data):
            continue
        
        # Értesítési logika
        if record is None:
            send_telegram(data, indok="új")
            db.execute("INSERT INTO properties (auction_id, created_at, notified_at, price) VALUES (?, ?, ?, ?)",
                       (str(auction_id), datetime.now().isoformat(), datetime.now().isoformat(), data["price"]))
        else:
            old_price = record[0]
            if data["price"] and data["price"] != old_price:
                indok = "új licit" if data["price"] > old_price else "árcsökkenés"
                send_telegram(data, indok=indok)
                db.execute("UPDATE properties SET price = ?, notified_at = ? WHERE auction_id = ?",
                           (data["price"], datetime.now().isoformat(), str(auction_id)))

if __name__ == "__main__":
    main()
