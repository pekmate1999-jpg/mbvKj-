import os
import re
import json
import html
import sqlite3
import requests
from datetime import datetime
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright

MAX_PRICE = 1_000_000

TARGET_URL = (
    "https://licitnaplo.hu/"
    "?bekoltozheto=true"
    "&tulajdoniHanyad=true"
    "&ar=0-1000000"
)

COUNTIES = [
    "veszprém", "zala", "somogy", "pest",
    "komárom", "fejér", "nógrád",
    "bács-kiskun", "jász-nagykun"
]

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

class DB:
    def __init__(self):
        self.conn = sqlite3.connect("mbvk_v6.db")
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS properties(
            auction_id TEXT PRIMARY KEY,
            created TEXT
        )
        """)
        self.conn.commit()

    def exists(self, auction_id):
        cur = self.conn.execute(
            "SELECT 1 FROM properties WHERE auction_id=?",
            (auction_id,)
        )
        return cur.fetchone() is not None

    def add(self, auction_id):
        self.conn.execute(
            "INSERT OR REPLACE INTO properties VALUES(?,?)",
            (auction_id, datetime.now().isoformat())
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


def telegram(msg):
    if not TOKEN or not CHAT_ID:
        return

    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        },
        timeout=30
    )


def allowed_county(text):
    text = text.lower()
    return any(c in text for c in COUNTIES)


def collect_links(page):
    page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)

    prev = 0
    same = 0

    while True:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)

        links = page.evaluate(
            "() => Array.from(document.querySelectorAll('a')).map(a=>a.href)"
        )

        links = [x for x in links if "/ingatlan/" in x]

        current = len(set(links))

        if current == prev:
            same += 1
            if same >= 3:
                break
        else:
            same = 0
            prev = current

    return sorted(list(set(links)))


def extract_json_data(page):
    scripts = page.locator("script[type='application/ld+json']")

    for i in range(scripts.count()):
        try:
            txt = scripts.nth(i).inner_text()

            if "arveresId" in txt:
                return json.loads(txt)
        except:
            pass

    content = page.content()

    patterns = [
        r'"arveresId"\s*:\s*(\d+)',
        r'"kikialtasiAr"\s*:\s*(\d+)',
    ]

    result = {}

    for p in patterns:
        m = re.search(p, content)
        if m:
            result[p] = m.group(1)

    return result


def scrape(page, url):

    page.goto(url, wait_until="domcontentloaded", timeout=30000)

    body = page.inner_text("body")

    title = ""

    try:
        title = page.locator("h1").first.inner_text().strip()
    except:
        title = url

    title = re.sub(
        r'^Ingatlan\s+árverés\s+',
        '',
        title,
        flags=re.IGNORECASE
    )

    json_data = extract_json_data(page)

    auction_id = None

    m = re.search(r'"arveresId"\s*:\s*(\d+)', page.content())
    if m:
        auction_id = m.group(1)

    price = None
    m = re.search(r'"kikialtasiAr"\s*:\s*(\d+)', page.content())
    if m:
        price = int(m.group(1))

    land_size = None

    land_patterns = [
        r'Telekméret[:\s]+([\d\s]+)',
        r'Telek területe[:\s]+([\d\s]+)',
        r'([\d\s]+)\s*m²'
    ]

    for p in land_patterns:
        m = re.search(p, body, re.IGNORECASE)
        if m:
            try:
                land_size = int(
                    m.group(1).replace(" ", "")
                )
                break
            except:
                pass

    return {
        "auction_id": auction_id,
        "title": title,
        "body": body,
        "price": price,
        "land_size": land_size
    }


def main():

    db = DB()

    new_count = 0

    telegram("🚀 MBVK V6 futás indult")

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox"]
        )

        ctx = browser.new_context()

        page = ctx.new_page()

        links = collect_links(page)

        for url in links:

            detail = ctx.new_page()

            try:

                data = scrape(detail, url)

                if not data["auction_id"]:
                    continue

                if db.exists(data["auction_id"]):
                    continue

                if not data["price"]:
                    continue

                if data["price"] > MAX_PRICE:
                    continue

                if not allowed_county(data["body"]):
                    continue

                sqm_price = "N/A"

                if data["land_size"] and data["land_size"] > 0:
                    sqm_price = f"{round(data['price']/data['land_size']):,} Ft/m²"

                maps_url = (
                    "https://www.google.com/maps/search/?api=1&query="
                    + quote_plus(data["title"])
                )

                telegram(
                    f"🏠 <b>{html.escape(data['title'])}</b>\n\n"
                    f"💰 Ár: {data['price']:,} Ft\n"
                    f"📏 Telekméret: {data['land_size'] or 'N/A'} m²\n"
                    f"📐 Négyzetméterár: {sqm_price}\n\n"
                    f"🗺️ <a href='{maps_url}'>Google Maps</a>\n"
                    f"🔗 <a href='{url}'>Adatlap</a>"
                )

                db.add(data["auction_id"])
                new_count += 1

            finally:
                detail.close()

        browser.close()

    if new_count == 0:
        telegram("✅ Nem találtam új, feltételeknek megfelelő ingatlant.")

    telegram(f"📊 MBVK V6 futás vége. Új találatok: {new_count}")


if __name__ == "__main__":
    main()
