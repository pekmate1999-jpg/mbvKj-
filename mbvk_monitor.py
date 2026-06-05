#!/usr/bin/env python3
"""
MBVK Árverési Monitor v6.03 – Beköltözhető ingatlanok (moveln=true)
Szűrés: tulajdoni hányad (1/1 vagy 1/2+1/2) és max ár 1M Ft

Változások (v6.03):
  - Szakaszonkénti Ft/m² megjelenítés (1./2./3. szakasz) - optimalizált méret
  - A szakaszok vége (hátralévő napok) a státusz sor fölé került
  - Törölve az egyedi Ft/m² érték (a jelenlegi árból számolt)
  - Telegram progress bar és Markdown escape javítások
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

def escape_markdown(text: str) -> str:
    """
    Specialis karakterek levédése Telegram legacy Markdown modhoz.
    Hagyományos Markdown módban csak a *, _, [, és ` karaktereket kell védeni.
    """
    if not text:
        return ""
    escape_chars = r"_*[`"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

# ── Telek- és épületméret kinyerése a leírásból ───────────────────────────────
def parse_sizes_from_description(desc: str) -> Tuple[Optional[float], Optional[float]]:
    if not desc:
        return None, None

    desc_lower = desc.lower()
    pattern = re.compile(r"(\d+(?:[.,]\d+)?)\s*m[²2]", re.IGNORECASE)
    
    telek_matches = []
    epulet_candidates = []
    
    epulet_prior_kws = ["lakóház", "lakás", "épület", "hasznos alapterület"]
    epulet_blacklist = ["vezetékjog", "szolgalmi jog", "terhel", "bejegyzett", "terheli"]
    
    for match in pattern.finditer(desc_lower):
        num_str = match.group(1).replace(",", ".")
        try:
            val = float(num_str)
        except ValueError:
            continue
        
        if val < 5 or val > 250_000:
            continue
        
        start = match.start()
        end = match.end()
        ctx_start = max(0, start - 60)
        ctx_end = min(len(desc_lower), end + 60)
        context = desc_lower[ctx_start:ctx_end]
        
        telek_kws = ["telek", "udvar", "terület"]
        is_telek = any(kw in context for kw in telek_kws)
        if is_telek:
            telek_matches.append(val)
        
        blacklisted = any(kw in context for kw in epulet_blacklist)
        if blacklisted:
            continue
        
        best_dist = None
        for kw in epulet_prior_kws:
            kw_pos = context.find(kw)
            if kw_pos != -1:
                abs_kw_pos = ctx_start + kw_pos
                dist = abs(abs_kw_pos - start)
                if best_dist is None or dist < best_dist:
                    best_dist = dist
                    
        if best_dist is not None:
            epulet_candidates.append((val, best_dist))
    
    telek = max(telek_matches) if telek_matches else None
    
    epulet = None
    if epulet_candidates:
        epulet_candidates.sort(key=lambda x: (x[1], -x[0]))
        epulet = epulet_candidates[0][0]
    
    if telek is not None and epulet is not None and epulet >= telek:
        epulet = None
        
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

def estimate_phase_ends(
    arveres_kezdete: str,
    arveres_vege: str,
    licit_szam: int = 0,
    minimum_ar: Optional[int] = None,
    kikialtas_ar: Optional[int] = None,
) -> List[str]:
    """
    MBVK szakasz-határok becslése az API adatokból (Vht. 145/B. §).
    A szakaszok mindig egyenlő hosszúak.
    """
    import datetime as _dt2
    start_dt   = _parse_dt(arveres_kezdete)
    api_end_dt = _parse_dt(arveres_vege)
    if not start_dt or not api_end_dt:
        return []

    total_days = (api_end_dt - start_dt).days
    time_part  = arveres_vege.split("T")[1] if "T" in arveres_vege else "12:00:00"

    def fmt(dt) -> str:
        return f"{dt.strftime('%Y-%m-%d')}T{time_part}"

    # minimum ár elérve -> lezárult
    if licit_szam and minimum_ar and kikialtas_ar and minimum_ar >= kikialtas_ar:
        return [fmt(api_end_dt)]

    # nincs licit -> API a 3. szakasz végét adja
    if licit_szam == 0:
        if total_days >= 45:
            sd = round(total_days / 3)
            return [
                fmt(start_dt + _dt2.timedelta(days=sd)),
                fmt(start_dt + _dt2.timedelta(days=sd * 2)),
                fmt(api_end_dt),
            ]
        return []

    # van licit -> API az aktuális szakasz végét adja
    if 5 <= total_days <= 30:           # 1. szakasz vége
        sd = total_days
        return [
            fmt(api_end_dt),
            fmt(api_end_dt + _dt2.timedelta(days=sd)),
            fmt(api_end_dt + _dt2.timedelta(days=sd * 2)),
        ]
    elif 30 < total_days <= 50:         # 2. szakasz vége
        sd = total_days // 2
        return [
            fmt(start_dt + _dt2.timedelta(days=sd)),
            fmt(api_end_dt),
            fmt(api_end_dt + _dt2.timedelta(days=sd)),
        ]
    elif 50 < total_days <= 75:         # 3. szakasz vége
        sd = total_days // 3
        return [
            fmt(start_dt + _dt2.timedelta(days=sd)),
            fmt(start_dt + _dt2.timedelta(days=sd * 2)),
            fmt(api_end_dt),
        ]

    return []

def generate_timeline(
    kezdete: str,
    vege: str,
    kiki_ar: Optional[int],
    min_ar: Optional[int],
    phase_ends: Optional[List[str]] = None,
) -> str:
    """
    Vizuális haladás-sáv + szakasz info a Telegram üzenethez.
    Kompakt, 9-karakteres verzió a sortörések elkerülésére.
    """
    import datetime as _dt

    now_dt = _dt.datetime.now()

    start_dt = _parse_dt(kezdete)
    end_dt   = _parse_dt(vege)

    if not start_dt or not end_dt:
        return "`[░░░|░░░|░░░]`\n_Ismeretlen időszak_"

    total_sec = (end_dt - start_dt).total_seconds()
    if total_sec <= 0:
        return "`[███|███|███]`\n_Lezárult_"

    elapsed  = (now_dt - start_dt).total_seconds()
    progress = max(0.0, min(1.0, elapsed / total_sec))

    filled = int(progress * 9)
    blocks = ["█" if i < filled else "░" for i in range(9)]
    bar_str = f"`[{''.join(blocks[0:3])}|{''.join(blocks[3:6])}|{''.join(blocks[6:9])}]` {int(progress * 100)}%"

    final_stage = 1
    if phase_ends:
        parsed_ends = sorted(filter(None, [_parse_dt(e) for e in phase_ends]))
        for idx, ph_end in enumerate(parsed_ends, start=1):
            if now_dt <= ph_end:
                final_stage = idx
                break
        else:
            final_stage = len(parsed_ends)
    else:
        if progress < 0.333:
            final_stage = 1
        elif progress < 0.666:
            final_stage = 2
        else:
            final_stage = 3

    ratio_text = ""
    if kiki_ar and min_ar and kiki_ar > 0:
        ratio_text = f" ({int(min_ar / kiki_ar * 100)}%)"

    return f"{bar_str}\n*{final_stage}. szakasz{ratio_text}*"

# ── Szakasz árak, ft/m² és hátralévő napok ───────────────────────────────────
def calculate_phase_prices(kikialtas_ar: Optional[int]) -> Optional[Tuple[int, int, int]]:
    """Visszaadja az 1., 2., 3. szakasz minimális vételárait (Ft-ban)."""
    if not kikialtas_ar or kikialtas_ar <= 0:
        return None
    stage1 = kikialtas_ar
    stage2 = int(kikialtas_ar * 2 / 3)
    stage3 = int(kikialtas_ar / 2)
    return (stage1, stage2, stage3)

def format_phase_prices(prices: Tuple[int, int, int]) -> str:
    """Formázza a szakasz árakat: '1 000 000 Ft / 666 666 Ft / 500 000 Ft'"""
    return f"{prices[0]:,} Ft / {prices[1]:,} Ft / {prices[2]:,} Ft".replace(",", " ")

def calculate_phase_ft_per_m2(phase_prices: Tuple[int, int, int], ref_area: Optional[float]) -> Optional[Tuple[int, int, int]]:
    """Kiszámolja a szakaszonkénti Ft/m² értékeket (kerekítve)."""
    if not ref_area or ref_area <= 0:
        return None
    return (
        int(phase_prices[0] / ref_area),
        int(phase_prices[1] / ref_area),
        int(phase_prices[2] / ref_area)
    )

def format_phase_ft_per_m2(ft_per_m2: Tuple[int, int, int]) -> str:
    """Formázza a szakaszonkénti Ft/m² értékeket kompaktabban: '260 / 173 / 130'"""
    return f"{ft_per_m2[0]:,} / {ft_per_m2[1]:,} / {ft_per_m2[2]:,}".replace(",", " ")

def format_phase_remaining_days(phase_ends: Optional[List[str]]) -> Optional[str]:
    """
    A szakaszok végdátumaiból kiszámolja a hátralévő napokat.
    Visszaad egy stringet: "5 nap, 15 nap, 25 nap" vagy None.
    """
    if not phase_ends:
        return None
    now = datetime.now()
    days_list = []
    for end_str in phase_ends:
        end_dt = _parse_dt(end_str)
        if end_dt is None:
            return None
        delta = (end_dt - now).days
        if delta < 0:
            days_list.append("lejárt")
        else:
            days_list.append(f"{delta} nap")
    return ", ".join(days_list)

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
    price = legmagasabb_licit or minimum_ar or kikialtas_ar

    licit_szam = g("bidCount", "licitekSzama")
    try:
        licit_szam = int(licit_szam) if licit_szam is not None else 0
    except (ValueError, TypeError):
        licit_szam = 0

    leiras_full = g("description", "leiras", "propertyDescription") or ""
    leiras = leiras_full[:200].rstrip() if leiras_full else ""

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

    # Referencia terület: telek > épület > None
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
        "bekoltözhető":       "igen",
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

    # Alapadatok escape-elése
    cim      = escape_markdown(data.get("cim", "N/A"))
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
    licit_str  = str(licit_n)                           if licit_n > 0 else None
    telek_str  = fmt_area(telek_m, telek_f)
    epulet_str = fmt_area(epulet_m, epulet_f)

    def fmt_date(s: Optional[str]) -> Optional[str]:
        if not s or s == "N/A":
            return None
        return s.replace("T", " ")[:16]
    end_str    = fmt_date(end)
    dist_str   = f"{dist_km:.0f} km"                   if dist_km is not None else None

    timeline = generate_timeline(
        data.get("arveres_kezdete", ""),
        data.get("arveres_vege", ""),
        data.get("kikialtas_ar"),
        data.get("minimum_ar"),
        data.get("phase_end_dates") or None,
    )

    # Szakasz árak és szakaszonkénti ft/m²
    phase_prices = calculate_phase_prices(data.get("kikialtas_ar"))
    phase_prices_str = format_phase_prices(phase_prices) if phase_prices else None
    phase_ft_per_m2 = calculate_phase_ft_per_m2(phase_prices, ref_area) if phase_prices and ref_area else None
    phase_ft_per_m2_str = format_phase_ft_per_m2(phase_ft_per_m2) if phase_ft_per_m2 else None
    phase_remaining = format_phase_remaining_days(data.get("phase_end_dates"))

    INDOK_EMOJI = {
        "új":        "🆕",
        "új licit":  "🔔",
        "új dátum":  "📅",
        "árcsökkenés": "📉",
    }
    emoji = INDOK_EMOJI.get(indok, "🏠")
    lines = [f"{emoji} *MBVK TALÁLAT – {indok.upper()}*", ""]

    if cim and cim != "N/A":
        lines.append(f"📍 *Cím:* {cim}")
    if dist_str:
        lines.append(f"🗺 *Budapest-távolság:* {dist_str}")
    if price_str:
        lines.append(f"💰 *Jelenlegi ár:* {price_str}")
    if legh_str:
        lines.append(f"📈 *Legmagasabb licit:* {legh_str}")

    if phase_prices_str:
        lines.append(f"💰 *Szakasz árak:* {phase_prices_str}")

    # Szakaszonkénti Ft/m² (kompakt forma ismétlések nélkül)
    if phase_ft_per_m2_str:
        lines.append(f"📈 *Ft/m²:* {phase_ft_per_m2_str}")

    if telek_str:
        lines.append(f"🏕 *Telekméret:* {telek_str}")
    if epulet_str:
        lines.append(f"🏠 *Épület alapterülete:* {epulet_str}")
    lines.append("🚪 *Beköltözhető:* igen")
    if hanyad:
        lines.append(f"📄 *Tulajdoni hányad:* {hanyad}")
    if licit_str:
        lines.append(f"🔄 *Licitek száma:* {licit_str}")

    # Szakaszok vége és Árverés vége a státusz fölé csoportosítva (duplikáció kiszűrve)
    if phase_remaining:
        lines.append(f"⏳ *Szakaszok vége:* {phase_remaining}")
    if end_str:
        lines.append(f"📅 *Árverés vége:* {end_str}")

    lines.append(f"📊 *Státusz:* {timeline}")

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
    log.info("MBVK Monitor v6.03 indítás – %s", datetime.now().isoformat())
    if not GEOPY_OK:
        log.warning("geopy nincs telepítve – Budapest-távolság nem elérhető.")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram beállítások hiányoznak – értesítések nem lesznek elküldve.")

    conn = init_db()
    session = requests.Session()
    session.headers.update(HEADERS)

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
        conn.close()
        return

    new_count = notified_count = 0

    for item in items:
        exec_id    = str(item.get("auctionId") or "")
        auction_id = str(item.get("auctionItemId") or item.get("id") or "")
        if not exec_id or not auction_id:
            continue

        url = f"{BASE_URL}/arveres-reszletek/{exec_id}/{auction_id}"

        detail = api_detail(session, exec_id, auction_id)
        if not detail:
            continue

        data = extract(detail)
        data["auction_id"] = auction_id
        data["url"] = url

        # Szakasz-határok becslése
        phase_ends = estimate_phase_ends(
            data.get("arveres_kezdete", ""),
            data.get("arveres_vege", ""),
            licit_szam   = data.get("licitek_szama", 0),
            minimum_ar   = data.get("minimum_ar"),
            kikialtas_ar = data.get("kikialtas_ar"),
        )
        if phase_ends:
            data["phase_end_dates"] = phase_ends
            data["arveres_vege"]    = phase_ends[-1]   # valódi teljes vége
        else:
            data["phase_end_dates"] = []

        log.info(
            "Feldolgozva: %s | %s | hányad=%s | ár=%s | telek=%s | épület=%s | licit_sz=%s",
            auction_id, data.get("cim", "N/A"), data.get("tulajdoni_hanyad"),
            data.get("price"), data.get("telek_meret"), data.get("epulet_meret"),
            data.get("licitek_szama"),
        )

        # Korábbi adatok lekérése
        existing = conn.execute(
            "SELECT price, licit_szam, arveres_vege FROM properties WHERE auction_id = ?",
            (auction_id,)
        ).fetchone()

        current_price  = data.get("price")
        current_licits = data.get("licitek_szama", 0)
        current_vege   = data.get("arveres_vege", "")

        is_new = existing is None

        indok: Optional[str] = None

        if is_new:
            if current_licits > 0:
                indok = "új licit"
            else:
                indok = "új"
            conn.execute(
                """INSERT INTO properties (auction_id, created_at, price, licit_szam, arveres_vege)
                   VALUES (?, ?, ?, ?, ?)""",
                (auction_id, datetime.utcnow().isoformat(),
                 current_price, current_licits, current_vege)
            )
        else:
            prev_price, prev_licits, prev_vege = existing

            price_decreased  = prev_price  is not None and current_price  is not None and current_price  < prev_price
            licit_increased  = prev_licits is not None and current_licits is not None and current_licits > prev_licits

            prev_dt = _parse_dt(prev_vege) if prev_vege and prev_vege != "N/A" else None
            curr_dt = _parse_dt(current_vege) if current_vege and current_vege != "N/A" else None
            date_moved_closer = prev_dt and curr_dt and curr_dt < prev_dt

            if price_decreased:
                indok = "árcsökkenés"
            elif licit_increased and date_moved_closer:
                indok = "új licit"
            elif date_moved_closer:
                indok = "új dátum"
            elif licit_increased:
                indok = "új licit"

            if price_decreased or licit_increased or date_moved_closer:
                conn.execute(
                    """UPDATE properties
                       SET price = ?, licit_szam = ?, arveres_vege = ?
                       WHERE auction_id = ?""",
                    (current_price, current_licits, current_vege, auction_id)
                )

        if passes_filters(data) and indok:
            log.info("✅ Értesítés küldése: %s (indok=%s)", auction_id, indok)
            send_telegram(data, indok=indok)
            conn.execute(
                "UPDATE properties SET notified_at = ? WHERE auction_id = ?",
                (datetime.utcnow().isoformat(), auction_id)
            )
            notified_count += 1
            if is_new:
                new_count += 1
        elif passes_filters(data):
            log.info("⚠️ Nincs változás, nem küldünk értesítést: %s", auction_id)
        else:
            log.info("❌ Nem ment át (szűrő): %s", auction_id)

        time.sleep(1)

    log.info("Kész – Új: %d / Értesítés: %d", new_count, notified_count)
    conn.close()

if __name__ == "__main__":
    run()
