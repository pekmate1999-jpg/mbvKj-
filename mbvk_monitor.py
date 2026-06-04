#!/usr/bin/env python3
"""
MBVK Árverési Monitor
Figyeli az https://arveres.mbvk.hu/ oldalt és Telegram értesítést küld
az új, szűrési feltételeknek megfelelő árverésekről.
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

# ─── Konfiguráció ────────────────────────────────────────────────────────────

BASE_URL = "https://arveres.mbvk.hu"
LIST_URL = "https://arveres.mbvk.hu/arveresi-lista"
DB_PATH = "mbvk_v7.db"
MAX_PRICE = 1_000_000

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

ACCEPTED_SHARES = [
    {"1/1"},
    {"1/2", "1/2"},
]

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── Logging ─────────────────────────────────────────────────────────────────

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

# ─── Segédfüggvények ─────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """Kisbetűs, ékezet nélküli szöveg."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def parse_price(text: str) -> Optional[int]:
    """'1 234 567 Ft' → 1234567"""
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def extract_area(text: str) -> Optional[float]:
    """'636 nm' vagy '636 m²' → 636.0"""
    m = re.search(r"(\d[\d\s]*[\.,]?\d*)\s*(?:nm|m²|m2|négyzetméter)", text, re.I)
    if m:
        raw = m.group(1).replace(" ", "").replace(",", ".")
        try:
            return float(raw)
        except ValueError:
            pass
    return None

# ─── Playwright scraper ──────────────────────────────────────────────────────

def get_auction_links(page) -> list[str]:
    """
    Visszaadja az összes /arveres-reszletek/ linket a listázó oldalakról.
    Az Angular SPA-t megvárja, amíg betölt.
    """
    links: set[str] = set()

    urls_to_try = [
        "https://arveres.mbvk.hu/arveresi-lista",
        "https://arveres.mbvk.hu/",
        "https://arveres.mbvk.hu/kereses",
    ]

    for url in urls_to_try:
        try:
            log.info("Lista oldal betöltése: %s", url)
            page.goto(url, timeout=60_000, wait_until="networkidle")
            page.wait_for_timeout(3000)

            # Scroll to load lazy content
            for _ in range(5):
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                page.wait_for_timeout(800)

            html = page.content()
            found = re.findall(r'href=["\']([^"\']*arveres-reszletek[^"\']*)["\']', html)
            for href in found:
                if href.startswith("http"):
                    links.add(href)
                else:
                    links.add(BASE_URL + ("" if href.startswith("/") else "/") + href)

            # Also try extracting from anchor tags via JS
            js_links = page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href)
                    .filter(h => h.includes('arveres-reszletek'))
            """)
            for lnk in js_links:
                links.add(lnk)

            if links:
                log.info("Talált linkek száma: %d", len(links))
                break
        except Exception as exc:
            log.warning("Lista oldal hiba (%s): %s", url, exc)

    # Fallback: try paginated API if available
    if not links:
        log.info("Fallback: API próba")
        try:
            api_links = try_api_fallback()
            links.update(api_links)
        except Exception as exc:
            log.warning("API fallback hiba: %s", exc)

    return list(links)


def try_api_fallback() -> list[str]:
    """Próbálja a publikus API-t (ha van)."""
    links = []
    api_urls = [
        "https://arveres.mbvk.hu/publicapi/arveresek",
        "https://arveres.mbvk.hu/api/arveresek",
        "https://arveres.mbvk.hu/publicapi/v1/arveresek",
    ]
    for url in api_urls:
        try:
            r = requests.get(url, timeout=15, headers={"Accept": "application/json"})
            if r.status_code == 200:
                data = r.json()
                # Különböző lehetséges struktúrák
                items = data if isinstance(data, list) else data.get("items", data.get("data", []))
                for item in items:
                    if isinstance(item, dict):
                        item_id = item.get("id") or item.get("arveresId") or item.get("azonosito")
                        if item_id:
                            # Próbáljuk rekonstruálni az URL-t
                            # Szükség lehet a végrehajtói azonosítóra is
                            exec_id = item.get("vegrehajtoid") or item.get("vegrehajtoi_id") or ""
                            links.append(f"{BASE_URL}/arveres-reszletek/{exec_id}/{item_id}")
        except Exception:
            continue
    return links


def scrape_detail(page, url: str) -> Optional[dict]:
    """Feldolgoz egy árverési részletoldalt."""
    try:
        log.info("Részletoldal: %s", url)
        page.goto(url, timeout=60_000, wait_until="networkidle")
        page.wait_for_timeout(2000)
        html = page.content()
    except PlaywrightTimeoutError:
        log.warning("Timeout: %s", url)
        return None
    except Exception as exc:
        log.warning("Oldal hiba (%s): %s", url, exc)
        return None

    # auction_id kinyerése az URL-ből
    m = re.search(r"/arveres-reszletek/(\d+)/(\d+)", url)
    if not m:
        m2 = re.search(r"/arveres-reszletek/(\d+)", url)
        auction_id = m2.group(1) if m2 else url.split("/")[-1]
    else:
        auction_id = m.group(2)

    data = {"auction_id": auction_id, "url": url}

    # ── Általános szövegkinyerő helper ──
    def find_field(*patterns: str) -> Optional[str]:
        for pat in patterns:
            found = re.search(pat, html, re.I | re.S)
            if found:
                raw = found.group(1).strip()
                # HTML tagek eltávolítása
                raw = re.sub(r"<[^>]+>", " ", raw)
                raw = re.sub(r"\s+", " ", raw).strip()
                return raw
        return None

    # ── Cím ──
    data["cim"] = (
        find_field(
            r'(?:cím|ingatlan\s*cím)["\s:>]*<[^>]*>([^<]+)',
            r'class="[^"]*cim[^"]*"[^>]*>([^<]{10,})',
            r'<h1[^>]*>([^<]{10,})</h1>',
            r'<h2[^>]*>([^<]{10,})</h2>',
            r'"address"\s*:\s*"([^"]+)"',
            r'Cím[^:]*:\s*</[^>]+>\s*<[^>]+>([^<]+)',
        )
        or "N/A"
    )

    # ── Megye ──
    data["megye"] = (
        find_field(
            r'[Mm]egye["\s:>]*(?:<[^>]+>)*\s*([A-ZÁÉÍÓÖŐÚÜŰa-záéíóöőúüű][\w\s\-]+?)(?:\s*</|\s*$)',
            r'"megye"\s*:\s*"([^"]+)"',
            r'Megye[^:]*:\s*</[^>]+>\s*<[^>]+>([^<]+)',
            r'class="[^"]*megye[^"]*"[^>]*>([^<]+)',
        )
    )

    # ── Település ──
    data["telepules"] = (
        find_field(
            r'[Tt]elepülés["\s:>]*(?:<[^>]+>)*\s*([A-ZÁÉÍÓÖŐÚÜŰa-záéíóöőúüű][\w\s\-\.]+?)(?:\s*</)',
            r'"telepules"\s*:\s*"([^"]+)"',
            r'"settlement"\s*:\s*"([^"]+)"',
            r'Település[^:]*:\s*</[^>]+>\s*<[^>]+>([^<]+)',
        )
    )

    # ── Tulajdoni hányad ──
    data["tulajdoni_hanyad"] = (
        find_field(
            r'[Tt]ulajdoni?\s*hányad["\s:>]*(?:<[^>]+>)*\s*(\d+/\d+(?:\s*\+\s*\d+/\d+)*)',
            r'"tulajdoniHanyad"\s*:\s*"([^"]+)"',
            r'"ownership[Ss]hare"\s*:\s*"([^"]+)"',
            r'Tulajdoni?\s*hányad[^:]*:\s*</[^>]+>\s*<[^>]+>([^<]+)',
        )
    )

    # ── Beköltözhető ──
    bekoltözhető_raw = find_field(
        r'[Bb]eköltözhető["\s:>]*(?:<[^>]+>)*\s*(igen|nem)',
        r'"bekoltözheto"\s*:\s*"([^"]+)"',
        r'"movable"\s*:\s*(true|false)',
        r'Beköltözhető[^:]*:\s*</[^>]+>\s*<[^>]+>([^<]+)',
    )
    data["bekoltözhető"] = bekoltözhető_raw.lower() if bekoltözhető_raw else None

    # ── Kikiáltási ár ──
    kikialtas_raw = find_field(
        r'[Kk]ikiáltási\s*ár["\s:>]*(?:<[^>]+>)*\s*([\d\s]+(?:Ft|forint)?)',
        r'"kikialtasiAr"\s*:\s*([\d]+)',
        r'Kikiáltási\s*ár[^:]*:\s*</[^>]+>\s*<[^>]+>([\d\s]+)',
    )
    data["kikialtas_ar"] = parse_price(kikialtas_raw) if kikialtas_raw else None

    # ── Minimum ár ──
    min_ar_raw = find_field(
        r'[Mm]inimum\s*ár["\s:>]*(?:<[^>]+>)*\s*([\d\s]+(?:Ft|forint)?)',
        r'[Ll]egkisebb\s*(?:érvényes\s*)?licit["\s:>]*(?:<[^>]+>)*\s*([\d\s]+)',
        r'"minimumAr"\s*:\s*([\d]+)',
        r'Minimum\s*ár[^:]*:\s*</[^>]+>\s*<[^>]+>([\d\s]+)',
    )
    data["minimum_ar"] = parse_price(min_ar_raw) if min_ar_raw else None

    # ── Legmagasabb licit ──
    legh_raw = find_field(
        r'[Ll]egmagasabb\s*(?:érvényes\s*)?licit["\s:>]*(?:<[^>]+>)*\s*([\d\s]+(?:Ft|forint)?)',
        r'"legmagasabbLicit"\s*:\s*([\d]+)',
        r'"highestBid"\s*:\s*([\d]+)',
        r'Legmagasabb\s*licit[^:]*:\s*</[^>]+>\s*<[^>]+>([\d\s]+)',
        r'Jelenlegi\s*licit[^:]*:\s*</[^>]+>\s*<[^>]+>([\d\s]+)',
    )
    data["legmagasabb_licit"] = parse_price(legh_raw) if legh_raw else None

    # ── Licitek száma ──
    licit_szam_raw = find_field(
        r'[Ll]icitek?\s*száma["\s:>]*(?:<[^>]+>)*\s*(\d+)',
        r'"licitekSzama"\s*:\s*(\d+)',
        r'"bidCount"\s*:\s*(\d+)',
        r'Licitek?\s*száma[^:]*:\s*</[^>]+>\s*<[^>]+>(\d+)',
    )
    data["licitek_szama"] = int(licit_szam_raw) if licit_szam_raw and licit_szam_raw.isdigit() else 0

    # ── Árverés vége ──
    data["arveres_vege"] = find_field(
        r'[Áá]rverés\s*(?:vége|zárul)["\s:>]*(?:<[^>]+>)*\s*(\d{4}[.\-/]\d{2}[.\-/]\d{2}[^<]{0,30})',
        r'"arveresVege"\s*:\s*"([^"]+)"',
        r'"auctionEnd"\s*:\s*"([^"]+)"',
        r'Árverés\s*vége[^:]*:\s*</[^>]+>\s*<[^>]+>([^<]+)',
        r'Zárul[^:]*:\s*</[^>]+>\s*<[^>]+>([^<]+)',
    )

    # ── Telekméret ──
    telek_raw = find_field(
        r'[Tt]elek(?:méret|terület|területe)?["\s:>]*(?:<[^>]+>)*\s*([\d\s,\.]+\s*(?:nm|m²|m2|négyzetméter))',
        r'"telekMeret"\s*:\s*"?([^",}]+)"?',
        r'"plotSize"\s*:\s*"?([^",}]+)"?',
        r'Alapterület[^:]*:\s*</[^>]+>\s*<[^>]+>([\d\s,\.]+\s*(?:nm|m²|m2))',
    )
    if telek_raw:
        data["telekmeret"] = extract_area(telek_raw) or parse_price(telek_raw)
    else:
        # Keres általánosan nm/m² értékeket
        nm_match = re.search(r'(\d[\d\s]*)\s*(?:nm|m²|m2)\b', html)
        data["telekmeret"] = float(nm_match.group(1).replace(" ", "")) if nm_match else None

    # ── Épület méret ──
    epulet_raw = find_field(
        r'[Éé]pület(?:méret|alapterület)?["\s:>]*(?:<[^>]+>)*\s*([\d\s,\.]+\s*(?:nm|m²|m2|négyzetméter))',
        r'"epuletMeret"\s*:\s*"?([^",}]+)"?',
        r'"buildingSize"\s*:\s*"?([^",}]+)"?',
    )
    data["epulet_meret"] = extract_area(epulet_raw) if epulet_raw else None

    # ── Ár meghatározása szűréshez ──
    price = data["legmagasabb_licit"] if data["legmagasabb_licit"] else data["minimum_ar"]
    data["price"] = price

    # ── Ft/m² ──
    if price and data["telekmeret"] and data["telekmeret"] > 0:
        data["ft_per_m2"] = round(price / data["telekmeret"])
    else:
        data["ft_per_m2"] = None

    return data

# ─── Szűrés ──────────────────────────────────────────────────────────────────

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
    # Szétbontjuk a "+" mentén
    parts = {p.strip() for p in hanyad.split("+")}
    if parts == {"1/1"}:
        return True
    if parts == {"1/2"}:  # két db 1/2
        count = hanyad.count("1/2")
        if count >= 2:
            return True
    return False


def passes_filters(data: dict) -> bool:
    # 1. Megye
    if not county_matches(data.get("megye")):
        log.debug("Szűrve (megye): %s", data.get("megye"))
        return False
    # 2. Beköltözhető
    if data.get("bekoltözhető") not in ("igen", "true", "1"):
        log.debug("Szűrve (beköltözhető): %s", data.get("bekoltözhető"))
        return False
    # 3. Tulajdoni hányad
    if not share_accepted(data.get("tulajdoni_hanyad")):
        log.debug("Szűrve (hányad): %s", data.get("tulajdoni_hanyad"))
        return False
    # 4. Ár
    price = data.get("price")
    if price is None or price > MAX_PRICE:
        log.debug("Szűrve (ár): %s", price)
        return False
    return True

# ─── Telegram ────────────────────────────────────────────────────────────────

def send_telegram(data: dict) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram token/chat_id nincs beállítva – kihagyva")
        return

    price = data.get("price")
    price_str = f"{price:,} Ft".replace(",", " ") if price else "N/A"

    legh = data.get("legmagasabb_licit")
    legh_str = f"{legh:,} Ft".replace(",", " ") if legh else "nincs"

    telek = data.get("telekmeret")
    telek_str = f"{telek:.0f} m²" if telek else "N/A"

    ft_m2 = data.get("ft_per_m2")
    ft_m2_str = f"{ft_m2:,} Ft/m²".replace(",", " ") if ft_m2 else "N/A"

    text = (
        "🏠 *ÚJ MBVK TALÁLAT*\n\n"
        f"📍 *Cím:*\n{data.get('cim', 'N/A')}\n\n"
        f"💰 *Ár:*\n{price_str}\n\n"
        f"📈 *Legmagasabb licit:*\n{legh_str}\n\n"
        f"📊 *Licitek száma:*\n{data.get('licitek_szama', 0)}\n\n"
        f"📐 *Telekméret:*\n{telek_str}\n\n"
        f"💵 *Ft/m²:*\n{ft_m2_str}\n\n"
        f"🏘 *Beköltözhető:*\nigen\n\n"
        f"📜 *Tulajdon:*\n{data.get('tulajdoni_hanyad', 'N/A')}\n\n"
        f"⏰ *Árverés vége:*\n{data.get('arveres_vege', 'N/A')}\n\n"
        f"🔗 *Link:*\n{data.get('url', '')}"
    )

    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            api_url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        if resp.status_code == 200:
            log.info("Telegram üzenet elküldve: %s", data.get("auction_id"))
        else:
            log.error("Telegram hiba: %s %s", resp.status_code, resp.text)
    except Exception as exc:
        log.error("Telegram küldési hiba: %s", exc)

# ─── Főprogram ────────────────────────────────────────────────────────────────

def run():
    log.info("MBVK Monitor indítás – %s", datetime.now().isoformat())
    conn = init_db()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="hu-HU",
        )
        page = context.new_page()

        # ── Cookie elfogadás ──
        try:
            page.goto(BASE_URL, timeout=30_000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            for btn_sel in [
                "button#s-all-bn",
                "button:has-text('Mindet elfogadom')",
                "button:has-text('Elfogadom')",
                "[id*='accept']",
            ]:
                try:
                    page.click(btn_sel, timeout=3000)
                    log.info("Cookie elfogadva: %s", btn_sel)
                    break
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Cookie elfogadás hiba: %s", exc)

        # ── Linkek gyűjtése ──
        auction_links = get_auction_links(page)
        log.info("Összesen %d árverési link találva", len(auction_links))

        if not auction_links:
            log.warning("Nem találtunk árverési linkeket – kilépés")
            browser.close()
            conn.close()
            return

        new_count = 0
        notified_count = 0

        for url in auction_links:
            # auction_id az URL-ből
            m = re.search(r"/arveres-reszletek/(\d+)/(\d+)", url)
            if m:
                auction_id = m.group(2)
            else:
                m2 = re.search(r"/arveres-reszletek/(\d+)", url)
                auction_id = m2.group(1) if m2 else url.split("/")[-1]

            if not is_new(conn, auction_id):
                log.debug("Már ismert: %s", auction_id)
                continue

            new_count += 1
            data = scrape_detail(page, url)
            if not data:
                continue

            log.info(
                "Feldolgozva: %s | megye=%s | hányad=%s | bekolt=%s | ár=%s",
                auction_id,
                data.get("megye"),
                data.get("tulajdoni_hanyad"),
                data.get("bekoltözhető"),
                data.get("price"),
            )

            if passes_filters(data):
                log.info("✅ SZŰRŐN ÁTMENT: %s", auction_id)
                send_telegram(data)
                notified_count += 1

            mark_seen(conn, auction_id)
            time.sleep(1)  # udvarias késleltetés

        log.info(
            "Kész – Új: %d / Értesítés: %d / Összes link: %d",
            new_count,
            notified_count,
            len(auction_links),
        )

        browser.close()
    conn.close()


if __name__ == "__main__":
    run()
