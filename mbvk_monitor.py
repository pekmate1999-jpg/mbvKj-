#!/usr/bin/env python3
"""
MBVK Árverési Monitor - v2
Network interception + HTML scraping + API fallback
"""

import os
import re
import sys
import time
import json
import logging
import sqlite3
import unicodedata
from datetime import datetime
from typing import Optional

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ─── Konfiguráció ─────────────────────────────────────────────────────────────

BASE_URL = "https://arveres.mbvk.hu"
DB_PATH = "mbvk_v7.db"
MAX_PRICE = 1_000_000
DEBUG_HTML = os.environ.get("DEBUG_HTML", "0") == "1"

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

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ─── SQLite ───────────────────────────────────────────────────────────────────

def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS properties (
            auction_id TEXT PRIMARY KEY,
            created    TEXT NOT NULL
        )"""
    )
    conn.commit()
    return conn


def is_new(conn: sqlite3.Connection, auction_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM properties WHERE auction_id = ?", (auction_id,)
    ).fetchone()
    return row is None


def mark_seen(conn: sqlite3.Connection, auction_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO properties (auction_id, created) VALUES (?, ?)",
        (auction_id, datetime.utcnow().isoformat()),
    )
    conn.commit()

# ─── Segédfüggvények ──────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def parse_price(text) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", str(text))
    return int(digits) if digits else None


def extract_area(text) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"(\d[\d\s]*[\.,]?\d*)\s*(?:nm|m\u00b2|m2|n\u00e9gyzet)", str(text), re.I)
    if m:
        raw = m.group(1).replace(" ", "").replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            pass
    return None


def flatten_json(obj, prefix="") -> dict:
    """JSON objektumot lapít ki kulcs=érték párokban."""
    result = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                result.update(flatten_json(v, new_key))
            else:
                result[new_key] = v
                result[k] = v  # Rövidített kulcs is
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            result.update(flatten_json(v, f"{prefix}[{i}]"))
    return result

# ─── Playwright lista ─────────────────────────────────────────────────────────

def get_auction_links(page) -> list[str]:
    links: set[str] = set()

    def on_response(response):
        if response.status != 200:
            return
        url = response.url
        if not ("publicapi" in url or "/api/" in url):
            return
        try:
            body = response.json()
            items = []
            if isinstance(body, list):
                items = body
            elif isinstance(body, dict):
                for key in ("content", "items", "data", "arveresek", "results", "list"):
                    if key in body and isinstance(body[key], list):
                        items = body[key]
                        break
                if not items and "id" in body:
                    items = [body]
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_id = (item.get("id") or item.get("arveresId") or
                           item.get("azonosito") or item.get("arveresAzonosito"))
                exec_id = (item.get("vegrehajtoid") or item.get("vegrehajtoi_id") or
                           item.get("vegrehajtasiUgyszam") or "")
                if item_id:
                    if exec_id:
                        links.add(f"{BASE_URL}/arveres-reszletek/{exec_id}/{item_id}")
                    else:
                        links.add(f"{BASE_URL}/arveres-reszletek/{item_id}")
            if items:
                log.info("Lista API válasz: %s -> %d elem", url, len(items))
        except Exception:
            pass

    page.on("response", on_response)

    for try_url in [
        "https://arveres.mbvk.hu/arveresi-lista",
        "https://arveres.mbvk.hu/",
    ]:
        try:
            log.info("Lista oldal betöltése: %s", try_url)
            page.goto(try_url, timeout=60_000, wait_until="networkidle")
            page.wait_for_timeout(4000)
            for _ in range(5):
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                page.wait_for_timeout(600)
            page.wait_for_timeout(1000)

            html = page.content()
            for href in re.findall(r'href=["\']([^"\']*arveres-reszletek[^"\']*)["\']', html):
                full = href if href.startswith("http") else BASE_URL + ("" if href.startswith("/") else "/") + href
                links.add(full)
            for lnk in page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href)
                    .filter(h => h.includes('arveres-reszletek'))
            """):
                links.add(lnk)

            if links:
                break
        except Exception as exc:
            log.warning("Lista oldal hiba: %s", exc)

    page.remove_listener("response", on_response)
    log.info("Talált linkek száma: %d", len(links))
    return list(links)

# ─── Részletoldal scraping ────────────────────────────────────────────────────

def scrape_detail(page, url: str) -> Optional[dict]:
    captured_api: dict = {}

    def on_response(response):
        if response.status != 200:
            return
        resp_url = response.url
        if "publicapi" in resp_url or "/api/" in resp_url:
            try:
                body = response.json()
                if isinstance(body, dict) and len(body) > 2:
                    log.info("Részlet API: %s kulcsok=%s", resp_url, list(body.keys())[:12])
                    captured_api.update(flatten_json(body))
            except Exception:
                pass

    page.on("response", on_response)
    try:
        log.info("Részletoldal: %s", url)
        page.goto(url, timeout=60_000, wait_until="networkidle")
        page.wait_for_timeout(3000)
        html = page.content()
    except PlaywrightTimeoutError:
        log.warning("Timeout: %s", url)
        page.remove_listener("response", on_response)
        return None
    except Exception as exc:
        log.warning("Oldal hiba (%s): %s", url, exc)
        page.remove_listener("response", on_response)
        return None
    page.remove_listener("response", on_response)

    if DEBUG_HTML:
        fname = f"debug_{url.split('/')[-1]}.html"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(html)
        log.info("Debug HTML: %s", fname)

    # auction_id
    m = re.search(r"/arveres-reszletek/(?:\d+/)?(\d+)", url)
    auction_id = m.group(1) if m else url.split("/")[-1]

    # JSON-t keresünk a HTML-ben is (Angular transfer state, inline script)
    for script_match in re.finditer(r'<script[^>]*>(.*?)</script>', html, re.S | re.I):
        content = script_match.group(1)
        # Keresünk JSON objektumokat a scriptben
        for json_match in re.finditer(r'(\{(?:[^{}]|(?:\{[^{}]*\}))*\})', content):
            try:
                obj = json.loads(json_match.group(1))
                if isinstance(obj, dict) and len(obj) > 3:
                    captured_api.update(flatten_json(obj))
            except Exception:
                pass

    if captured_api:
        log.info("Összegyűjtött API mezők: %s",
                 [k for k in captured_api.keys() if not k.startswith("[") and "." not in k][:20])

    # ── Kinyerő függvények ──
    def jget(*keys: str) -> Optional[str]:
        for k in keys:
            v = captured_api.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    def hget(*patterns: str) -> Optional[str]:
        for pat in patterns:
            found = re.search(pat, html, re.I | re.S)
            if found:
                raw = found.group(1).strip()
                raw = re.sub(r"<[^>]+>", " ", raw)
                raw = re.sub(r"\s+", " ", raw).strip()
                if raw:
                    return raw
        return None

    # ── Mezők ──
    megye = jget("megye", "county", "varmegye", "vármegyeNeve", "helyszinMegye",
                 "ingatlan.megye", "arveres.megye") or hget(
        r'[Mm]egye["\s:>]+(?:<[^>]+>)*([A-Z\u00c1\u00c9\u00cd\u00d3\u00d6\u0150\u00da\u00dc\u0170a-z\u00e1\u00e9\u00ed\u00f3\u00f6\u0151\u00fa\u00fc\u0171][^\n<]{2,40}?)(?:\s*<)',
        r'"megye"\s*:\s*"([^"]+)"',
        r'Megye\s*[:\-]\s*([^\n<]{2,40})',
    )

    telepules = jget("telepules", "settlement", "varos", "helyszinTelepules",
                     "ingatlan.telepules", "arveres.telepules") or hget(
        r'[Tt]elepül[eé]s["\s:>]+(?:<[^>]+>)*([A-Z\u00c1\u00c9\u00cd\u00d3\u00d6\u0150\u00da\u00dc\u0170a-z\u00e1\u00e9\u00ed\u00f3\u00f6\u0151\u00fa\u00fc\u0171][^\n<]{2,40}?)(?:\s*<)',
        r'"telepules"\s*:\s*"([^"]+)"',
    )

    cim = jget("cim", "address", "helyszin", "ingatlanCim", "teljesCim",
               "ingatlan.cim", "arveres.cim") or hget(
        r'[Cc][ií]m["\s:>]+(?:<[^>]+>)*([^\n<]{5,100}?)(?:\s*<)',
        r'"cim"\s*:\s*"([^"]+)"',
        r'"address"\s*:\s*"([^"]+)"',
        r'<h1[^>]*>\s*([^<]{5,100})\s*</h1>',
    ) or "N/A"

    tulajdoni_hanyad = jget("tulajdoniHanyad", "ownershipShare", "hanyad",
                            "tulajdonihanyad", "ingatlan.tulajdoniHanyad") or hget(
        r'[Tt]ulajdoni?\s*h[aá]nyad["\s:>]+(?:<[^>]+>)*\s*(\d+/\d+(?:\s*[+]\s*\d+/\d+)*)',
        r'"tulajdoniHanyad"\s*:\s*"([^"]+)"',
        r'[Tt]ulajdoni?\s*h[aá]nyad\s*[:\-]\s*([^\n<]{1,30})',
        r'(\d+/\d+(?:\s*\+\s*\d+/\d+)+)',
    )

    bek_raw = jget("bekoltözheto", "bekoltözhető", "bekoltozheto", "movable",
                   "szabad", "ingatlan.bekoltözhető") or hget(
        r'[Bb]ek[oö]lt[oö]zh[eé]t[oő]["\s:>]+(?:<[^>]+>)*\s*(igen|nem|true|false)',
        r'"bekoltozheto[e]?"\s*:\s*"?(igen|nem|true|false)"?',
        r'"movable"\s*:\s*(true|false)',
        r'[Bb]ek[oö]lt[oö]zh[eé]t[oő]\s*[:\-]\s*(igen|nem)',
    )
    if bek_raw:
        bl = bek_raw.lower().strip()
        bekoltözhető = "igen" if bl in ("igen", "true", "1", "yes") else ("nem" if bl in ("nem", "false", "0", "no") else bl)
    else:
        bekoltözhető = None

    kikialtas_ar = parse_price(
        jget("kikialtasiAr", "startPrice", "kikialtasi_ar") or
        hget(r'[Kk]iki[aá]lt[aá]si\s*[aá]r["\s:>]+(?:<[^>]+>)*\s*([\d\s\.]+\s*(?:Ft|forint)?)',
             r'"kikialtasiAr"\s*:\s*(\d+)')
    )

    minimum_ar = parse_price(
        jget("minimumAr", "minPrice", "minimumLicit", "legkisebbLicit") or
        hget(r'[Mm]inimum\s*[aá]r["\s:>]+(?:<[^>]+>)*\s*([\d\s\.]+)',
             r'"minimumAr"\s*:\s*(\d+)')
    )

    legmagasabb_licit = parse_price(
        jget("legmagasabbLicit", "highestBid", "currentBid", "maxLicit") or
        hget(r'[Ll]egmagasabb\s*(?:[eé]rv[eé]nyes\s*)?licit["\s:>]+(?:<[^>]+>)*\s*([\d\s\.]+)',
             r'"legmagasabbLicit"\s*:\s*(\d+)',
             r'"highestBid"\s*:\s*(\d+)')
    )

    lsz_raw = jget("licitekSzama", "bidCount", "licitSzam") or hget(
        r'[Ll]icitek?\s*sz[aá]ma["\s:>]+(?:<[^>]+>)*\s*(\d+)',
        r'"licitekSzama"\s*:\s*(\d+)',
        r'"bidCount"\s*:\s*(\d+)',
    )
    licitek_szama = int(lsz_raw) if lsz_raw and str(lsz_raw).strip().isdigit() else 0

    arveres_vege = jget("arveresVege", "auctionEnd", "vegeDatuma", "lezarasDatuma",
                        "zarasDatum") or hget(
        r'[Áá]rver[eé]s\s*(?:v[eé]ge|z[aá]rul)["\s:>]+(?:<[^>]+>)*\s*(\d{4}[.\-]\d{2}[.\-]\d{2}[^\n<]{0,25})',
        r'"arveresVege"\s*:\s*"([^"]+)"',
        r'"auctionEnd"\s*:\s*"([^"]+)"',
        r'(\d{4}\.\s*\d{2}\.\s*\d{2}\.?\s*\d{0,2}:\d{0,2})',
    )

    telek_raw = jget("telekMeret", "plotSize", "terulet", "telekTerulet") or hget(
        r'[Tt]elek(?:m[eé]ret|ter[uü]let)["\s:>]+(?:<[^>]+>)*\s*([\d\s,\.]+\s*(?:nm|m\u00b2|m2))',
        r'"telekMeret"\s*:\s*"?([^",}\n]+)"?',
        r'[Tt]elek\s*[:\-]\s*([\d\s,\.]+\s*(?:nm|m\u00b2|m2))',
    )
    telekmeret = extract_area(telek_raw) if telek_raw else None
    if telekmeret is None and telek_raw:
        telekmeret = parse_price(telek_raw)
    if telekmeret is None:
        nm_all = re.findall(r'(\d[\d\s]{0,5})\s*(?:nm|m\u00b2|m2)\b', html)
        if nm_all:
            try:
                telekmeret = float(nm_all[0].replace(" ", ""))
            except ValueError:
                pass

    ep_raw = jget("epuletMeret", "buildingSize", "alapterulet", "hasznalatiTerulet") or hget(
        r'[Éé]p[uü]let(?:m[eé]ret|alapterület)?["\s:>]+(?:<[^>]+>)*\s*([\d\s,\.]+\s*(?:nm|m\u00b2|m2))',
        r'"epuletMeret"\s*:\s*"?([^",}\n]+)"?',
    )
    epulet_meret = extract_area(ep_raw) if ep_raw else None

    price = legmagasabb_licit or minimum_ar
    ft_per_m2 = round(price / telekmeret) if price and telekmeret and telekmeret > 0 else None

    return {
        "auction_id": auction_id,
        "url": url,
        "cim": cim,
        "megye": megye,
        "telepules": telepules,
        "tulajdoni_hanyad": tulajdoni_hanyad,
        "bekoltözhető": bekoltözhető,
        "kikialtas_ar": kikialtas_ar,
        "minimum_ar": minimum_ar,
        "legmagasabb_licit": legmagasabb_licit,
        "licitek_szama": licitek_szama,
        "arveres_vege": arveres_vege,
        "telekmeret": telekmeret,
        "epulet_meret": epulet_meret,
        "price": price,
        "ft_per_m2": ft_per_m2,
    }

# ─── Szűrés ───────────────────────────────────────────────────────────────────

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

# ─── Telegram ─────────────────────────────────────────────────────────────────

def send_telegram(data: dict) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram token/chat_id nincs beállítva")
        return

    price = data.get("price")
    price_str = f"{price:,} Ft".replace(",", " ") if price else "N/A"
    legh = data.get("legmagasabb_licit")
    legh_str = f"{legh:,} Ft".replace(",", " ") if legh else "nincs"
    telek = data.get("telekmeret")
    telek_str = f"{telek:.0f} m\u00b2" if telek else "N/A"
    ft_m2 = data.get("ft_per_m2")
    ft_m2_str = f"{ft_m2:,} Ft/m\u00b2".replace(",", " ") if ft_m2 else "N/A"

    text = (
        "\U0001f3e0 *\u00daj MBVK TAL\u00c1LAT*\n\n"
        f"\U0001f4cd *C\u00edm:*\n{data.get('cim', 'N/A')}\n\n"
        f"\U0001f4b0 *\u00c1r:*\n{price_str}\n\n"
        f"\U0001f4c8 *Legmagasabb licit:*\n{legh_str}\n\n"
        f"\U0001f4ca *Licitek sz\u00e1ma:*\n{data.get('licitek_szama', 0)}\n\n"
        f"\U0001f4d0 *Telek:*\n{telek_str}\n\n"
        f"\U0001f4b5 *Ft/m\u00b2:*\n{ft_m2_str}\n\n"
        f"\U0001f3d8 *Bek\u00f6lt\u00f6zh\u00e9t\u0151:*\nigen\n\n"
        f"\U0001f4dc *Tulajdon:*\n{data.get('tulajdoni_hanyad', 'N/A')}\n\n"
        f"\u23f0 *\u00c1rver\u00e9s v\u00e9ge:*\n{data.get('arveres_vege', 'N/A')}\n\n"
        f"\U0001f517 *Link:*\n{data.get('url', '')}"
    )

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("Telegram elküldve: %s", data.get("auction_id"))
        else:
            log.error("Telegram hiba: %s %s", resp.status_code, resp.text[:200])
    except Exception as exc:
        log.error("Telegram hiba: %s", exc)

# ─── Főprogram ────────────────────────────────────────────────────────────────

def run():
    log.info("MBVK Monitor indítás – %s", datetime.now().isoformat())
    conn = init_db()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            locale="hu-HU",
        )
        page = context.new_page()

        # Cookie elfogadás
        try:
            page.goto(BASE_URL, timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            for sel in ["button#s-all-bn", "button:has-text('Mindet elfogadom')",
                        "button:has-text('Elfogadom')"]:
                try:
                    page.click(sel, timeout=3000)
                    log.info("Cookie elfogadva")
                    break
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Cookie hiba: %s", exc)

        auction_links = get_auction_links(page)
        log.info("Összesen %d árverési link találva", len(auction_links))

        if not auction_links:
            log.warning("Nem találtunk árverési linkeket – kilépés")
            browser.close()
            conn.close()
            return

        new_count = notified_count = 0

        for url in auction_links:
            m = re.search(r"/arveres-reszletek/(?:\d+/)?(\d+)", url)
            auction_id = m.group(1) if m else url.split("/")[-1]

            if not is_new(conn, auction_id):
                log.debug("Már ismert: %s", auction_id)
                continue

            new_count += 1
            data = scrape_detail(page, url)
            if not data:
                continue

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
            time.sleep(1)

        log.info("Kész – Új: %d / Értesítés: %d / Összes link: %d",
                 new_count, notified_count, len(auction_links))
        browser.close()
    conn.close()


if __name__ == "__main__":
    run()
