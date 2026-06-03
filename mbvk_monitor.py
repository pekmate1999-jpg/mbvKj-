import os
import re
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
        self.conn = sqlite3.connect("mbvk_v5.db")
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS properties(
            url TEXT PRIMARY KEY,
            created TEXT
        )
        """)
        self.conn.commit()

    def exists(self, url):
        cur = self.conn.execute(
            "SELECT 1 FROM properties WHERE url=?",
            (url,)
        )
        return cur.fetchone() is not None

    def add(self, url):
        self.conn.execute(
            "INSERT OR REPLACE INTO properties VALUES(?,?)",
            (url, datetime.now().isoformat())
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


def extract_price(text):
    matches = re.findall(r'([\d\s\xa0]+)\s*Ft', text)
    for m in matches:
        try:
            return int(m.replace(" ", "").replace("\xa0", ""))
        except:
            pass
    return None


def extract_land_size(text):

    patterns = [
        r'([\d\s]+)\s*m²',
        r'([\d\s]+)\s*m2',
        r'([\d\s]+)\s*nm',
        r'Telekméret[:\s]+([\d\s]+)',
        r'Telek területe[:\s]+([\d\s]+)'
    ]

    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            try:
                return int(
                    m.group(1)
                    .replace(" ", "")
                    .replace("\xa0", "")
                )
            except:
                pass

    return None


def allowed_county(text):
    text = text.lower()
    return any(c in text for c in COUNTIES)


def collect_links(page):

    page.goto(
        TARGET_URL,
        wait_until="domcontentloaded",
        timeout=60000
    )

    prev = 0
    same = 0

    while True:

        page.evaluate(
            "window.scrollTo(0, document.body.scrollHeight)"
        )

        page.wait_for_timeout(1500)

        links = page.evaluate(
            "() => Array.from(document.querySelectorAll('a')).map(a=>a.href)"
        )

        links = [
            x for x in links
            if "/ingatlan/" in x
        ]

        current = len(set(links))

        if current == prev:
            same += 1
            if same >= 3:
                break
        else:
            same = 0
            prev = current

    return sorted(list(set(links)))


def scrape(page, url):

    page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=30000
    )

    try:
        title = page.locator("h1").first.inner_text()
    except:
        title = url

    body = page.inner_text("body")

    return {
        "title": title,
        "body": body,
        "price": extract_price(body),
        "land_size": extract_land_size(body)
    }


def main():

    db = DB()

    stats_new = 0

    telegram("🚀 MBVK V5 futás indult")

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox"]
        )

        ctx = browser.new_context()

        page = ctx.new_page()

        links = collect_links(page)

        for url in links:

            if db.exists(url):
                continue

            detail = ctx.new_page()

            try:

                data = scrape(detail, url)

                if not data["price"]:
                    continue

                if data["price"] > MAX_PRICE:
                    continue

                if not allowed_county(data["body"]):
                    continue

                sqm_price = "N/A"

                if data["land_size"] and data["land_size"] > 0:
                    sqm = round(
                        data["price"] / data["land_size"]
                    )
                    sqm_price = f"{sqm:,} Ft/m²"

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

                db.add(url)
                stats_new += 1

            finally:
                detail.close()

        browser.close()

    if stats_new == 0:
        telegram("✅ Nem találtam új, feltételeknek megfelelő ingatlant.")

    telegram(f"📊 Futás vége. Új találatok: {stats_new}")


if __name__ == "__main__":
    main()
