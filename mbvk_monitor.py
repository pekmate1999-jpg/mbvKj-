#!/usr/bin/env python3
"""
MBVK Árverési Monitor v8.0.0 – Beköltözhető ingatlanok (moveln=true)
Újdonságok v8:
  - config.yaml alapú konfiguráció (max_price, pontozás, szűrők)
  - 1–5 skálás score-rendszer: távolság, telekméret, Ft/m², megye
  - Hiba javítás: szóközzel tagolt számok ("2 971 m²") helyes parse-olása
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


# geopy opcionális
try:
    from geopy.geocoders import Nominatim
    from geopy.distance import geodesic
    GEOPY_OK = True
except ImportError:
    GEOPY_OK = False

# ── Config betöltés ───────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("MBVK_CONFIG", "config.yaml")

def load_config() -> Dict:
    """Betölti a config.yaml-t. Ha nem létezik, alapértelmezett értékeket ad vissza."""
    defaults = {
        "filters": {
            "max_price": 1_000_000,
            "min_score": 3.0,
            "only_movein_ready": True,
        },
        "distance_score": {"enabled": False, "weight": 30, "thresholds": []},
        "land_size_score": {"enabled": False, "weight": 25, "thresholds": []},
        "price_per_m2_score": {"enabled": False, "weight": 35, "thresholds": []},
        "county_score": {"enabled": False, "weight": 10, "scores": [], "default_points": 1},
    }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        # Mély merge: defaults felülírása a betöltött értékekkel
        for key, val in loaded.items():
            if isinstance(val, dict) and key in defaults and isinstance(defaults[key], dict):
                defaults[key].update(val)
            else:
                defaults[key] = val
        log.info("Config betöltve: %s", CONFIG_PATH)
    except FileNotFoundError:
        log.warning("config.yaml nem található – alapértelmezett értékek használatban.")
    except Exception as e:
        log.error("Config betöltési hiba: %s", e)
    return defaults

# ── Logging (config előtt kell) ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Konfiguráció ──────────────────────────────────────────────────────────────
CFG: Dict = {}   # run() elején töltjük be

BASE_URL  = "https://arveres.mbvk.hu"
API_BASE  = "https://arveres.mbvk.hu/publicapi"
DB_PATH   = "mbvk_v8.db"

BUDAPEST_COORDS = (47.4979, 19.0402)
COUNTIES: List[str] = []

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Település-megye szótár ────────────────────────────────────────────────────
TELEPULES_MAP: Dict[str, str] = {}

def load_telepules_map():
    try:
        with open("telepulesek.csv", mode="r", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter=";")
            skip = 3
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
                megye_clean = megye_raw.replace("fõváros", "főváros").replace("fovaros", "főváros")
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

# ── Segédfüggvények ───────────────────────────────────────────────────────────
def normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))

def parse_hungarian_float(s: str) -> Optional[float]:
    """
    v8 JAVÍTÁS: szóközzel tagolt ezres elválasztókat is kezel.
    Pl. '2 971' -> 2971.0, '1.590' -> 1590.0, '1,5' -> 1.5
    """
    if not s:
        return None
    s = s.strip()
    # Szóköz mint ezres elválasztó eltávolítása (pl. "2 971" -> "2971")
    # DE csak ha utána nem tizedes következik (különben "1 500,5" -> "1500.5" is OK)
    s = re.sub(r'(\d)\s+(\d)', r'\1\2', s)
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
    v8 JAVÍTÁS: szóközzel tagolt számokat is felismer a leírásban.
    Pl. '2 971 m²' -> 2971.0
    """
    if val is None:
        return None
    s = str(val).strip()
    # Szóközzel tagolt szám + m² egység: "2 971 m²" vagy "2 971 m2"
    m = re.search(r"([\d]+(?:\s[\d]{3})*(?:[.,][\d]+)?)\s*(?:m[²2]|nm)", s, re.IGNORECASE)
    if m:
        return parse_hungarian_float(m.group(1))
    # Fallback: első szám a szövegben
    m = re.search(r"([\d]+(?:[.,][\d]+)?)", s)
    if m:
        return parse_hungarian_float(m.group(1))
    return None

def escape_markdown(text: str) -> str:
    if not text:
        return ""
    escape_chars = r"_*[`"
    return re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)

def generate_gcal_url(title: str, date_str: str, location: str = "", details: str = "") -> Optional[str]:
    if not date_str or date_str == "N/A":
        return None
    try:
        import zoneinfo
        bp_tz = zoneinfo.ZoneInfo("Europe/Budapest")
    except Exception:
        bp_tz = None
    try:
        matches = re.findall(r'(\d{4})[-.]\s*(\d{2})[-.]\s*(\d{2})[^\d]*(\d{2}):(\d{2})', date_str)
        if not matches:
            return None
        y, m, d, h, minute = matches[0]
        start_dt = datetime(int(y), int(m), int(d), int(h), int(minute))
        end_dt = start_dt + timedelta(hours=1)
        if bp_tz:
            start_dt = start_dt.replace(tzinfo=bp_tz)
            end_dt = end_dt.replace(tzinfo=bp_tz)
            start_str = start_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M00Z")
            end_str   = end_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M00Z")
        else:
            start_str = start_dt.strftime("%Y%m%dT%H%M00")
            end_str   = end_dt.strftime("%Y%m%dT%H%M00")
        url = (f"https://calendar.google.com/calendar/render?action=TEMPLATE"
               f"&text={urllib.parse.quote(title)}"
               f"&dates={start_str}/{end_str}"
               f"&ctz=Europe/Budapest")
        if location and location != "N/A":
            url += f"&location={urllib.parse.quote(location)}"
        if details:
            url += f"&details={urllib.parse.quote(details)}"
        return url
    except Exception as e:
        log.warning("Nem sikerült a naptár link generálása: %s", e)
        return None

# ── Telek- és épületméret kinyerése a leírásból ───────────────────────────────
def parse_sizes_from_description(desc: str) -> Tuple[Optional[float], Optional[float]]:
    """
    v8 JAVÍTÁS: Szóközzel tagolt számok (pl. "2 971 m2") helyes felismerése.
    A regex most \d+(?:\s\d{3})* mintát is kezel.
    """
    if not desc:
        return None, None

    desc_lower = desc.lower()

    # v8: szóközzel tagolt ezres is felismerhető: "2 971 m²"
    pattern = re.compile(
        r"(\d+(?:\s\d{3})*(?:[.,]\d+)?)\s*(?:m[²2]|nm|négyzetméter)",
        re.IGNORECASE
    )

    telek_kws_patterns = [
        r"telek",
        r"udvar",
        r"ingatlan",
        r"terület|terülten|területen|területét|térületre|terüle",
        r"tulajdonból",
        r"földterület",
        r"üres terület",
    ]
    telek_pattern = re.compile("|".join(telek_kws_patterns), re.IGNORECASE)

    epulet_prior_kws = [
        "lakóház", "lakás", "épület", "épületre",
        "hasznos alapterület", "hasznos alapterülete",
        "alapterület", "alapterülete",
        "lakóterület", "lakóépület",
        "házrész", "lakóegység"
    ]

    area_blacklist = [
        "vezetékjog", "vezeték jog",
        "szolgalmi jog", "közművezetékjog",
        "terhelés alá esik", "terhelés", "terhel", "terheli",
        "bejegyzett",
        "engedélyszám",
        "javára",
        "vázrajz", "vázrajza",
        "ű.sz", "helyrajzi szám", "helyrajz", "kataszteri",
        "üzemegység", "közös tulajdon", "közös épületrész",
    ]

    telek_matches = []
    epulet_candidates = []

    for match in pattern.finditer(desc_lower):
        num_str = match.group(1)
        val = parse_hungarian_float(num_str)
        if val is None:
            continue
        if val < 5 or val > 250_000:
            continue

        start = match.start()
        end   = match.end()

        before_str = desc_lower[max(0, start - 30):start]
        if re.search(r"ft\.?\s*(?:/|per)?$", before_str):
            continue

        bl_ctx = desc_lower[max(0, start - 100):min(len(desc_lower), end + 100)]
        if any(kw in bl_ctx for kw in area_blacklist):
            continue

        ctx_start = max(0, start - 150)
        ctx_end   = min(len(desc_lower), end + 150)
        context   = desc_lower[ctx_start:ctx_end]

        best_telek_dist = None
        for tm in telek_pattern.finditer(context):
            abs_pos = ctx_start + tm.start()
            dist = abs(abs_pos - start)
            if best_telek_dist is None or dist < best_telek_dist:
                best_telek_dist = dist

        best_epulet_dist = None
        for kw in epulet_prior_kws:
            kw_pos = context.find(kw)
            if kw_pos != -1:
                abs_kw_pos = ctx_start + kw_pos
                dist = abs(abs_kw_pos - start)
                if best_epulet_dist is None or dist < best_epulet_dist:
                    best_epulet_dist = dist

        if best_telek_dist is not None and best_epulet_dist is not None:
            if best_telek_dist <= best_epulet_dist:
                telek_matches.append(val)
            else:
                epulet_candidates.append((val, best_epulet_dist))
        elif best_telek_dist is not None:
            telek_matches.append(val)
        elif best_epulet_dist is not None:
            epulet_candidates.append((val, best_epulet_dist))
        else:
            telek_matches.append(val)

    telek = max(telek_matches) if telek_matches else None

    epulet = None
    if epulet_candidates:
        epulet_candidates.sort(key=lambda x: (x[1], -x[0]))
        epulet = epulet_candidates[0][0]

    return telek, epulet

# ── Budapest-távolság ─────────────────────────────────────────────────────────
_geocoder = Nominatim(user_agent="mbvk_monitor_v8") if GEOPY_OK else None
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

def google_maps_url(cim: Optional[str]) -> Optional[str]:
    if not cim or cim == "N/A":
        return None
    encoded = quote_plus(cim)
    return f"https://www.google.com/maps/search/?api=1&query={encoded}"

# ── Pontozási rendszer (config.yaml alapján) ──────────────────────────────────
def score_distance(dist_km: Optional[float]) -> Optional[float]:
    cfg = CFG.get("distance_score", {})
    if not cfg.get("enabled") or dist_km is None:
        return None
    for t in cfg.get("thresholds", []):
        if dist_km <= t["max_km"]:
            return float(t["points"])
    return 1.0

def score_land_size(telek_m2: Optional[float]) -> Optional[float]:
    cfg = CFG.get("land_size_score", {})
    if not cfg.get("enabled") or telek_m2 is None:
        return None
    best = 1.0
    for t in cfg.get("thresholds", []):
        if telek_m2 >= t["min_m2"]:
            best = float(t["points"])
    return best

def score_price_per_m2(price: Optional[int], ref_area: Optional[float]) -> Optional[float]:
    cfg = CFG.get("price_per_m2_score", {})
    if not cfg.get("enabled") or not price or not ref_area or ref_area <= 0:
        return None
    ft_per_m2 = price / ref_area
    for t in cfg.get("thresholds", []):
        if ft_per_m2 <= t["max_ft_per_m2"]:
            return float(t["points"])
    return 1.0

def score_county(megye: Optional[str]) -> Optional[float]:
    cfg = CFG.get("county_score", {})
    if not cfg.get("enabled") or not megye:
        return None
    norm_megye = normalize(megye)
    for entry in cfg.get("scores", []):
        if normalize(entry["county"]) in norm_megye or norm_megye in normalize(entry["county"]):
            return float(entry["points"])
    return float(cfg.get("default_points", 1))

def calculate_score(data: Dict, dist_km: Optional[float]) -> Tuple[float, Dict]:
    """
    Súlyozott átlag alapján számít 1–5 skálás pontszámot.
    Visszaadja a végső pontszámot és a részletes bontást.
    """
    components = {}

    d_cfg = CFG.get("distance_score", {})
    l_cfg = CFG.get("land_size_score", {})
    p_cfg = CFG.get("price_per_m2_score", {})
    c_cfg = CFG.get("county_score", {})

    d_score = score_distance(dist_km)
    l_score = score_land_size(data.get("telek_meret"))
    p_score = score_price_per_m2(data.get("price"), data.get("ref_area"))
    c_score = score_county(data.get("megye"))

    weighted_sum = 0.0
    total_weight = 0.0

    def add(name: str, val: Optional[float], weight: float):
        nonlocal weighted_sum, total_weight
        if val is not None:
            weighted_sum += val * weight
            total_weight += weight
            components[name] = round(val, 2)

    if d_cfg.get("enabled"):
        add("távolság", d_score, d_cfg.get("weight", 30))
    if l_cfg.get("enabled"):
        add("telekméret", l_score, l_cfg.get("weight", 25))
    if p_cfg.get("enabled"):
        add("Ft/m²", p_score, p_cfg.get("weight", 35))
    if c_cfg.get("enabled"):
        add("megye", c_score, c_cfg.get("weight", 10))

    if total_weight == 0:
        return 3.0, {}

    final_score = round(weighted_sum / total_weight, 2)
    return final_score, components

def format_score(score: float, components: Dict) -> str:
    """Telegram-barát score megjelenítés."""
    stars = "⭐" * round(score) + "☆" * (5 - round(score))
    details = " | ".join(f"{k}: {v:.1f}" for k, v in components.items())
    return f"{stars} *{score:.1f}/5*" + (f"\n_{details}_" if details else "")

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
    def fmt(dt) -> str:
        return f"{dt.strftime('%Y-%m-%d')}T{time_part}"
    phase_sec = total_sec / 3
    phase1_end = start_dt + timedelta(seconds=phase_sec)
    phase2_end = start_dt + timedelta(seconds=phase_sec * 2)
    phase3_end = end_dt
    if licit_szam and minimum_ar and kikialtas_ar and minimum_ar >= kikialtas_ar:
        return [fmt(phase3_end)]
    return [fmt(phase1_end), fmt(phase2_end), fmt(phase3_end)]

estimate_phase_ends = calculate_phase_ends

def get_current_stage(phase_ends):
    if not phase_ends:
        return 1
    now_dt = datetime.now()
    parsed_ends = sorted(filter(None, [_parse_dt(e) for e in phase_ends]))
    for idx, ph_end in enumerate(parsed_ends, start=1):
        if now_dt <= ph_end:
            return idx
    return len(parsed_ends)

def generate_timeline(kezdete, vege, kiki_ar, min_ar, phase_ends=None) -> str:
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
        current_stage = 1 if progress < 0.333 else (2 if progress < 0.666 else 3)
    ratio_text = ""
    if kiki_ar and min_ar and kiki_ar > 0:
        ratio_text = f" ({int(min_ar / kiki_ar * 100)}%)"
    return f"{bar_str}\n*{current_stage}. szakasz{ratio_text}*"

def calculate_phase_prices(kikialtas_ar, ar_tipus="altalanos"):
    if not kikialtas_ar or kikialtas_ar <= 0:
        return None
    if ar_tipus == "jelzalog":
        return (kikialtas_ar, kikialtas_ar, kikialtas_ar)
    stage1 = int(kikialtas_ar * 0.90)
    stage2 = int(kikialtas_ar * 0.70)
    stage3 = int(kikialtas_ar * 0.50)
    return (stage1, stage2, stage3)

def format_phase_prices(prices):
    if prices[0] == prices[1] == prices[2]:
        return f"{prices[0]:,} Ft (minden szakaszban)".replace(",", " ")
    return f"{prices[0]:,} Ft / {prices[1]:,} Ft / {prices[2]:,} Ft".replace(",", " ")

def calculate_phase_ft_per_m2(phase_prices, ref_area):
    if not ref_area or ref_area <= 0:
        return None
    return (int(phase_prices[0]/ref_area), int(phase_prices[1]/ref_area), int(phase_prices[2]/ref_area))

def format_phase_ft_per_m2(ft_per_m2):
    return f"{ft_per_m2[0]:,} Ft/m² / {ft_per_m2[1]:,} Ft/m² / {ft_per_m2[2]:,} Ft/m²".replace(",", " ")

def format_phase_remaining_days(phase_ends):
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
        days_list.append("lejárt" if delta < 0 else f"{delta} nap")
    return ", ".join(days_list) if days_list else None

# ── Adatbázis ─────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS properties (
            auction_id      TEXT PRIMARY KEY,
            created_at      TEXT,
            notified_at     TEXT,
            price           INTEGER,
            licit_szam      INTEGER,
            arveres_vege    TEXT,
            current_szakasz INTEGER,
            score           REAL
        )
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(properties)")}
    for col, typedef in [
        ("created_at",      "TEXT"),
        ("notified_at",     "TEXT"),
        ("price",           "INTEGER"),
        ("licit_szam",      "INTEGER"),
        ("arveres_vege",    "TEXT"),
        ("current_szakasz", "INTEGER"),
        ("score",           "REAL"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE properties ADD COLUMN {col} {typedef}")
            log.info("DB séma frissítve: %s oszlop hozzáadva", col)
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

    cim = " ".join(cim_parts) if cim_parts else (
        addr.get("formattedAddress") or g("address", "cim", "fullAddress", "ingatlanCim") or "N/A"
    )

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
    leiras = leiras_full[:1000].rstrip() if leiras_full else ""

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

    telek_api  = parse_area(g("landArea",  "totalArea", "telekmeret", "terulet"))
    epulet_api = parse_area(g("builtArea", "area",      "alapterulet", "livingArea"))

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

    arveres_vege    = g("auctionEndDate", "endDate", "auctionEnd", "deadline", "befejezesDatuma")
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

def passes_filters(data: Dict, score: float) -> bool:
    max_price = CFG.get("filters", {}).get("max_price", 1_000_000)
    min_score = CFG.get("filters", {}).get("min_score", 3.0)

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
    if price is None or price > max_price:
        return False

    if score < min_score:
        log.info("❌ Score túl alacsony: %.2f < %.2f", score, min_score)
        return False

    return True

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(data: Dict, indok: str = "új", score: float = 0.0, score_components: Dict = None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram nincs beállítva")
        return

    score_components = score_components or {}

    cim_raw = data.get("cim", "N/A")
    url_raw = data.get("url", "")

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

    dist_km  = bp_distance_km(telepules, data.get("cim"))
    maps_url = google_maps_url(data.get("cim"))

    def fmt_area(val, forras):
        if val is None:
            return None
        s = f"{val:,.0f} m²".replace(",", " ")
        if forras == "leiras":
            s += " _(leírásból)_"
        return s

    price_str  = f"{price:,} Ft".replace(",", " ")  if price else None
    legh_str   = f"{legh:,} Ft".replace(",", " ")   if legh  else None
    licit_str  = str(licit_n)                        if licit_n > 0 else None
    telek_str  = fmt_area(telek_m, telek_f)
    epulet_str = fmt_area(epulet_m, epulet_f)

    def fmt_date(s):
        if not s or s == "N/A":
            return None
        return s.replace("T", " ")[:16]
    end_str = fmt_date(end)
    dist_str = f"{dist_km:.0f} km" if dist_km is not None else None

    end_gcal_url = None
    if end_str:
        end_gcal_url = generate_gcal_url(
            title=f"MBVK Árverés Vége: {cim_raw}",
            date_str=end_str,
            location=cim_raw,
            details=f"Részletek: {url_raw}",
        )

    timeline = generate_timeline(
        data.get("arveres_kezdete", ""),
        data.get("arveres_vege", ""),
        data.get("kikialtas_ar"),
        data.get("minimum_ar"),
        data.get("phase_end_dates") or None,
    )

    phase_prices     = calculate_phase_prices(data.get("kikialtas_ar"), ar_tipus=data.get("ar_tipus", "altalanos"))
    phase_prices_str = format_phase_prices(phase_prices) if phase_prices else None
    phase_ft_per_m2  = calculate_phase_ft_per_m2(phase_prices, ref_area) if phase_prices and ref_area else None
    phase_ft_per_m2_str = format_phase_ft_per_m2(phase_ft_per_m2) if phase_ft_per_m2 else None
    phase_remaining  = format_phase_remaining_days(data.get("phase_end_dates"))

    INDOK_EMOJI = {
        "új": "🆕", "új licit": "🔔", "új dátum": "📅",
        "árcsökkenés": "📉", "szakaszváltás": "🔄",
    }
    emoji = INDOK_EMOJI.get(indok, "🏠")

    lines = [f"{emoji} *MBVK TALÁLAT – {indok.upper()}*", ""]

    # 0. Értékelés (score)
    score_str = format_score(score, score_components)
    lines.append(f"🎯 *Értékelés:* {score_str}")
    lines.append("")

    # 1. Elhelyezkedés
    megye_str = escape_markdown(data.get("megye", "") or "")
    lines.append("🌍 *1. Elhelyezkedés és Alapadatok*")
    if cim and cim != "N/A":
        lines.append(f"📍 *Cím:* {cim}")
    if megye_str:
        lines.append(f"🏛 *Megye:* {megye_str}")
    if dist_str:
        lines.append(f"🗺 *Budapest-távolság:* {dist_str}")
    lines.append("")

    # 2. Ingatlan jellemzők
    lines.append("🏠 *2. Az Ingatlan és a Telek Jellemzői*")
    if telek_str:
        lines.append(f"🏕 *Telekméret:* {telek_str}")
    if epulet_str:
        lines.append(f"🏚 *Épület alapterülete:* {epulet_str}")
    lines.append(f"🚪 *Beköltözhető:* {data.get('bekoltozheto', 'igen')}")
    lines.append("")

    # 3. Pénzügyi
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

    # 4. Jogi és árverési státusz
    lines.append("⚖️ *4. Jogi és Árverési Státusz*")
    if hanyad:
        lines.append(f"📄 *Tulajdoni hányad:* {hanyad}")
    if phase_remaining:
        lines.append(f"⏳ *Szakaszok vége:* {phase_remaining}")
    if end_str:
        if end_gcal_url:
            lines.append(f"📅 *Árverés vége:* [{end_str}]({end_gcal_url})")
        else:
            lines.append(f"📅 *Árverés vége:* {end_str}")
    if licit_str:
        lines.append(f"🔄 *Licitek száma:* {licit_str}")
    lines.append(f"📊 *Státusz:* {timeline}")
    lines.append("")

    if leiras:
        lines.append(f"📝 *Leírás:*\n_{leiras}_")
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
                "disable_web_page_preview": True,
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
    global CFG
    CFG = load_config()

    load_telepules_map()
    log.info("MBVK Monitor v8.0.0 indítás – %s", datetime.now().isoformat())
    log.info("MAX_PRICE: %s Ft | MIN_SCORE: %s",
             CFG.get("filters", {}).get("max_price", "?"),
             CFG.get("filters", {}).get("min_score", "?"))

    if not GEOPY_OK:
        log.warning("geopy nincs telepítve – Budapest-távolság és távolság-score nem elérhető.")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram beállítások hiányoznak.")

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

        phase_ends = calculate_phase_ends(
            data.get("arveres_kezdete", ""),
            data.get("arveres_vege", ""),
            licit_szam   = data.get("licitek_szama", 0),
            minimum_ar   = data.get("minimum_ar"),
            kikialtas_ar = data.get("kikialtas_ar"),
        )
        data["phase_end_dates"] = phase_ends if phase_ends else []
        if phase_ends:
            data["arveres_vege"] = phase_ends[-1]

        current_szakasz = get_current_stage(data.get("phase_end_dates") or [])

        # Score kiszámítása
        dist_km = bp_distance_km(data.get("telepules"), data.get("cim"))
        score, score_components = calculate_score(data, dist_km)
        data["score"] = score

        log.info(
            "Feldolgozva: %s | %s | ár=%s | telek=%s | score=%.2f",
            auction_id, data.get("cim", "N/A"),
            data.get("price"), data.get("telek_meret"), score,
        )

        existing = conn.execute(
            "SELECT price, licit_szam, arveres_vege, current_szakasz FROM properties WHERE auction_id = ?",
            (auction_id,)
        ).fetchone()

        current_price  = data.get("price")
        current_licits = data.get("licitek_szama", 0)
        current_vege   = data.get("arveres_vege", "")

        is_new = existing is None
        indok: Optional[str] = None

        if is_new:
            indok = "új licit" if current_licits > 0 else "új"
            conn.execute(
                """INSERT INTO properties
                   (auction_id, created_at, price, licit_szam, arveres_vege, current_szakasz, score)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (auction_id, datetime.now().isoformat(),
                 current_price, current_licits, current_vege, current_szakasz, score)
            )
        else:
            prev_price, prev_licits, prev_vege, prev_szakasz = existing
            price_decreased   = prev_price   is not None and current_price  is not None and current_price  < prev_price
            licit_increased   = prev_licits  is not None and current_licits is not None and current_licits > prev_licits
            szakasz_increased = prev_szakasz is not None and current_szakasz > prev_szakasz
            prev_dt = _parse_dt(prev_vege) if prev_vege and prev_vege != "N/A" else None
            curr_dt = _parse_dt(current_vege) if current_vege and current_vege != "N/A" else None
            date_moved_closer = prev_dt and curr_dt and curr_dt < prev_dt

            if szakasz_increased and price_decreased:
                indok = "szakaszváltás"   # a szakaszváltás már magában foglalja az árcsökkenést
            elif szakasz_increased:
                indok = "szakaszváltás"
            elif price_decreased:
                indok = "árcsökkenés"
            elif licit_increased and date_moved_closer:
                indok = "új licit"
            elif date_moved_closer:
                indok = "új dátum"
            elif licit_increased:
                indok = "új licit"

            if price_decreased or licit_increased or date_moved_closer or szakasz_increased:
                conn.execute(
                    """UPDATE properties
                       SET price=?, licit_szam=?, arveres_vege=?, current_szakasz=?, score=?
                       WHERE auction_id=?""",
                    (current_price, current_licits, current_vege, current_szakasz, score, auction_id)
                )

        if passes_filters(data, score) and indok:
            log.info("✅ Értesítés: %s (indok=%s, score=%.2f)", auction_id, indok, score)
            send_telegram(data, indok=indok, score=score, score_components=score_components)
            conn.execute(
                "UPDATE properties SET notified_at=? WHERE auction_id=?",
                (datetime.now().isoformat(), auction_id)
            )
            notified_count += 1
            if is_new:
                new_count += 1
        elif passes_filters(data, score):
            log.info("⚠️ Nincs változás: %s", auction_id)
        else:
            log.info("❌ Kiszűrve: %s (score=%.2f)", auction_id, score)

        time.sleep(1)

    log.info("Kész – Új: %d / Értesítés: %d", new_count, notified_count)
    conn.close()

if __name__ == "__main__":
    run()
