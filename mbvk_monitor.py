#!/usr/bin/env python3
"""
MBVK Árverési Monitor – Playwright + BeautifulSoup
"""

import re
import os
import sys
import time
import sqlite3
import logging
import urllib.parse
from datetime import datetime
from typing import Optional, List, Dict

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ── Konfiguráció ──────────────────────────────────────────────────────────────
BASE_URL = "https://arveres.mbvk.hu"
DB_PATH = "mbvk_v11.db"
MAX_PRICE = 1_000_000
COUNTIES = []  # pl. ["Békés"]

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── SQLite ────────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS properties (auction_id TEXT PRIMARY KEY, created TEXT)")
    conn.commit()
    return conn

def is_new(conn, auction_id):
    return conn.execute("SELECT 1 FROM properties WHERE auction_id = ?", (auction_id,)).fetchone() is None

def mark_seen(conn, auction_id):
    conn.execute("INSERT OR IGNORE INTO properties (auction_id, created) VALUES (?, ?)",
                 (auction_id, datetime.utcnow().isoformat()))
    conn.commit()

# ── Adatok kinyerése a renderelt HTML-ből ─────────────────────────────────────
def extract_from_rendered_html(html: str, url: str) -> Dict:
    soup = BeautifulSoup(html, 'html.parser')

    # Cím
    cim_elem = soup.select_one("li.location p")
    cim = cim_elem.get_text(strip=True) if cim_elem else "N/A"

    # Telekméret (description-ből)
    desc_elem = soup.select_one("div.description")
    desc = desc_elem.get_text() if desc_elem else ""
    area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*m[²2]", desc, re.IGNORECASE)
    telek = float(area_match.group(1).replace(",", ".")) if area_match else None

    # Árverési adatok
    min_price_elem = soup.select_one("li.min-price span:last-child")
    min_price = parse_price(min_price_elem.get_text()) if min_price_elem else None

    starting_price_elem = soup.select_one("li.starting-price span:last-child")
    starting_price = parse_price(starting_price_elem.get_text()) if starting_price_elem else None

    bid_step_elem = soup.select_one("li.bidding-ladder span:last-child")
    bid_step = parse_price(bid_step_elem.get_text()) if bid_step_elem else None

    down_pay_elem = soup.select_one("li.down-payment span:last-child")
    down_pay = parse_price(down_pay_elem.get_text()) if down_pay_elem else None

    end_date_elem = soup.select_one("li.end-date p")
    end_date = end_date_elem.get_text(strip=True) if end_date_elem else None

    # Tulajdoni hányad (li.data-row)
    ownership = None
    for li in soup.select("li.data-row"):
        spans = li.find_all("span")
        if len(spans) >= 2 and "tulajdoni hányad" in spans[0].get_text().lower():
            ownership = spans[1].get_text(strip=True)
            break

    # Licit adatok
    bid_count = len(soup.select(".table-wrapper tbody tr"))
    highest_bid_elem = soup.select_one(".table-wrapper tbody tr td:nth-child(2) strong")
    highest_bid = parse_price(highest_bid_elem.get_text()) if highest_bid_elem else None

    # Képek
    images = []
    for img in soup.select(".desktop-gallery .img-button img, .mobile-gallery img"):
        src = img.get("src") or img.get("data-src")
        if src and src.startswith("http"):
            images.append(src)

    # Ügyszám
    case_elem = soup.select_one("h1")
    case_number = ""
    if case_elem:
        match = re.search(r"Ügyszám:\s*([^\<]+)", case_elem.get_text())
        if match:
            case_number = match.group(1).strip()

    # Város kinyerése a címből (pl. "5530 Vésztő, ...")
    telepules = ""
    if cim != "N/A":
        parts = cim.split(",")
        if parts:
            telepules = parts[0].split()[-1] if len(parts[0].split()) > 1 else ""

    return {
        "url": url,
        "case_number": case_number,
        "cim": cim,
        "telepules": telepules,
        "min_price": min_price,
        "starting_price": starting_price,
        "current_price": highest_bid or min_price or starting_price,
        "bid_step": bid_step,
        "down_pay": down_pay,
        "bid_count": bid_count,
        "ownership_share": ownership,
        "end_date": end_date,
        "land_area": telek,
        "images": images,
    }

def parse_price(val: str) -> Optional[int]:
    if not val:
        return None
    digits = re.sub(r"[^\d]", "", str(val))
    return int(digits) if digits else None

# ── Szűrés ───────────────────────────────────────────────────────────────────
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
    if COUNTIES:
        # megye nincs a HTML-ben, ezért ezt a szűrőt kihagyjuk, vagy később bővíthető
        pass
    if data.get("end_date"):
        try:
            end = datetime.strptime(data["end_date"], "%Y.%m.%d. %H:%M:%S")
            if end < datetime.now():
                return False
        except:
            pass
    if not share_accepted(data.get("ownership_share")):
        return False
    price = data.get("current_price")
    if price is None or price > MAX_PRICE:
        return False
    return True

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram_photo(photo_url: str, caption: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        img_data = requests.get(photo_url, timeout=10).content
        files = {'photo': img_data}
        data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': caption}
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files)
    except Exception as e:
        log.error("Kép küldés hiba: %s", e)

def send_telegram(data: Dict):
    lines = ["🏠 *ÚJ MBVK ÁRVERÉS*", ""]
    if data.get("cim"):
        lines.append(f"📍 *Cím:* {data['cim']}")
    if data.get("case_number"):
        lines.append(f"📑 *Ügyszám:* {data['case_number']}")
    if data.get("starting_price"):
        lines.append(f"💰 *Kikiáltási ár:* {data['starting_price']:,} Ft".replace(",", " "))
    if data.get("min_price"):
        lines.append(f"📉 *Minimum ár:* {data['min_price']:,} Ft".replace(",", " "))
    if data.get("current_price"):
        lines.append(f"💵 *Aktuális ár:* {data['current_price']:,} Ft".replace(",", " "))
    if data.get("bid_step"):
        lines.append(f"📈 *Licitlépcső:* {data['bid_step']:,} Ft".replace(",", " "))
    if data.get("down_pay"):
        lines.append(f"💸 *Előleg:* {data['down_pay']:,} Ft".replace(",", " "))
    if data.get("bid_count", 0) > 0:
        lines.append(f"🔄 *Licitek száma:* {data['bid_count']}")
    if data.get("land_area"):
        lines.append(f"📐 *Telek:* {data['land_area']:.0f} m²")
    if data.get("ownership_share"):
        lines.append(f"📄 *Tulajdoni hányad:* {data['ownership_share']}")
    if data.get("end_date"):
        lines.append(f"⏳ *Vége:* {data['end_date']}")
    if data.get("cim") and data["cim"] != "N/A":
        encoded = urllib.parse.quote(data["cim"])
        lines.append(f"🗺️ [Térkép](https://www.google.com/maps/search/?api=1&query={encoded})")
    lines.append(f"🔗 [Részletek]({data['url']})")

    text = "\n".join(lines)
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        log.error("Telegram hiba: %s", e)

    for i, img in enumerate(data.get("images", [])[:3]):
        send_telegram_photo(img, f"Kép {i+1}")

# ── Főprogram – listát a böngésző segítségével szerezzük ─────────────────────
def get_auction_list_from_page(page, base_url):
    """Lekéri az összes beköltözhető árverés linkjét az első oldalról."""
    page.goto(f"{base_url}/arveresi-hirdetmenyek?moveln=true&phaseCode=normal_ingatlan_2021")
    page.wait_for_selector("div.auction-item, .auction-list-item", timeout=10000)
    html = page.content()
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for a in soup.select("a[href*='/arveres-reszletek/']"):
        href = a.get("href")
        if href and href not in links:
            links.append(href)
    return [f"{base_url}{link}" if link.startswith("/") else link for link in links]

def run():
    log.info("MBVK Monitor indítás (Playwright) – %s", datetime.now().isoformat())
    conn = init_db()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # Beállítjuk a fejléceket, hogy úgy nézzen ki, mint egy böngésző
        page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://arveres.mbvk.hu/",
            "X-Requested-With": "XMLHttpRequest"
        })

        # Lekérjük az összes árverés linkjét (egyszerűen az első oldalról)
        # Valós monitorozáshoz lapozni kellene, de itt most csak az első 50-100 linket nézzük
        urls = get_auction_list_from_page(page, BASE_URL)
        log.info("Talált %d árverési link", len(urls))

        new_count = notified_count = 0
        for url in urls[:100]:  # korlátozzuk a teszteléshez
            # Kivesszük az auction_id-t az URL-ből (az utolsó szám)
            match = re.search(r"/(\d+)$", url)
            auction_id = match.group(1) if match else None
            if not auction_id or not is_new(conn, auction_id):
                continue

            new_count += 1
            log.info("Feldolgozás: %s", auction_id)
            page.goto(url, timeout=30000)
            # Várjuk, hogy megjelenjen a cím (legalább egy .location elem)
            try:
                page.wait_for_selector("li.location", timeout=10000)
            except:
                log.warning("Nem töltött be a tartalom: %s", url)
                mark_seen(conn, auction_id)
                continue

            html = page.content()
            data = extract_from_rendered_html(html, url)

            log.info("Cím: %s, ár: %s, tul.hányad: %s", data["cim"], data["current_price"], data["ownership_share"])

            if passes_filters(data):
                log.info("✅ Értesítés küldése")
                send_telegram(data)
                notified_count += 1
            else:
                log.info("❌ Nem felel meg a szűrőknek")

            mark_seen(conn, auction_id)
            time.sleep(2)

        browser.close()

    log.info("Kész. Új: %d, értesítve: %d", new_count, notified_count)

if __name__ == "__main__":
    run()
