import os
import csv
import html
import sqlite3
import logging
import requests
from datetime import datetime
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright

DB = "mbvk.db"
LOG = "mbvk.log"
CSV_EXPORT = "ingatlanok.csv"

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TARGET_URL = "https://licitnaplo.hu/"

logging.basicConfig(
    filename=LOG,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)


class Storage:

    def __init__(self):
        self.conn = sqlite3.connect(DB)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS properties(
            id TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            price TEXT,
            first_seen TEXT,
            last_seen TEXT
        )
        """)
        self.conn.commit()

    def exists(self, pid):
        cur = self.conn.execute(
            "SELECT 1 FROM properties WHERE id=?",
            (pid,)
        )
        return cur.fetchone() is not None

    def add(self, pid, url, title, price):
        now = datetime.now().isoformat()

        self.conn.execute(
            """
            INSERT OR REPLACE INTO properties
            VALUES(?,?,?,?,?,?)
            """,
            (pid, url, title, price, now, now)
        )
        self.conn.commit()

    def close(self):
        self.conn.close()


def telegram(msg):

    if not TOKEN or not CHAT_ID:
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=30
        )

        return r.status_code == 200

    except Exception as e:
        logging.exception(e)
        return False


def export_csv(rows):

    with open(CSV_EXPORT, "w", newline="", encoding="utf-8") as f:

        writer = csv.writer(f)

        writer.writerow([
            "title",
            "price",
            "url"
        ])

        writer.writerows(rows)


def scrape_property(page, url):

    data = {
        "title": "Ismeretlen",
        "price": "N/A"
    }

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000
        )

        try:
            data["title"] = page.locator("h1").first.inner_text()
        except:
            pass

        body = page.inner_text("body")

        for line in body.splitlines():
            if "Ft" in line:
                data["price"] = line.strip()
                break

    except Exception as e:
        logging.exception(e)

    return data


def collect_links(page):

    page.goto(
        TARGET_URL,
        wait_until="domcontentloaded"
    )

    previous = 0
    retries = 0

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

        if current == previous:
            retries += 1

            if retries >= 3:
                break
        else:
            retries = 0
            previous = current

    return sorted(list(set(links)))


def main():

    telegram("🚀 MBVK PRO V3 indult")

    db = Storage()

    exported = []

    stats = {
        "new": 0,
        "checked": 0,
        "errors": 0
    }

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox"]
        )

        ctx = browser.new_context()

        try:

            page = ctx.new_page()

            links = collect_links(page)

            for url in links:

                stats["checked"] += 1

                pid = url

                if db.exists(pid):
                    continue

                detail_page = ctx.new_page()

                data = scrape_property(
                    detail_page,
                    url
                )

                detail_page.close()

                db.add(
                    pid,
                    url,
                    data["title"],
                    data["price"]
                )

                exported.append([
                    data["title"],
                    data["price"],
                    url
                ])

                maps = (
                    "https://www.google.com/maps/search/?api=1&query="
                    + quote_plus(data["title"])
                )

                telegram(
                    f"🏠 <b>{html.escape(data['title'])}</b>\n\n"
                    f"💰 {html.escape(data['price'])}\n\n"
                    f"🗺️ <a href='{maps}'>Térkép</a>\n"
                    f"🔗 <a href='{url}'>Adatlap</a>"
                )

                stats["new"] += 1

            export_csv(exported)

        except Exception as e:

            stats["errors"] += 1

            telegram(
                f"❌ Hiba:\n{html.escape(str(e))}"
            )

            logging.exception(e)

        finally:

            browser.close()
            db.close()

    telegram(
        f"📊 Összesítő\n\n"
        f"Ellenőrzött: {stats['checked']}\n"
        f"Új: {stats['new']}\n"
        f"Hibák: {stats['errors']}"
    )

    if stats["new"] == 0:
        telegram("✅ Nem találtam új ingatlant.")


if __name__ == "__main__":
    main()
