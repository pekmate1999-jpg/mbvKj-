import os
import json
import re
import requests
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

DB_FILE = "mbvk_adatbazis.json"
MAX_AR = 2_000_000


def load_database():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_database(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }

    requests.post(url, json=payload, timeout=20)


def extract_size(text):
    m = re.search(r"(\\d+[\\d\\s]*)\\s*(m²|m2|nm)", text.lower())
    if not m:
        return None

    try:
        return int("".join(filter(str.isdigit, m.group(1))))
    except Exception:
        return None


def main():
    print("Monitor indul...")

    db = load_database()

    target_url = (
        "https://licitnaplo.hu/"
        "?bekoltozheto=true"
        "&tulajdoniHanyad=true"
        "&tehermentes=true"
        "&ar=0-5000000"
        "&status=aktiv"
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        page = browser.new_page()
        page.goto(target_url, wait_until="networkidle", timeout=60000)

        last_height = 0

        while True:
            page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )

            page.wait_for_timeout(2000)

            new_height = page.evaluate(
                "document.body.scrollHeight"
            )

            if new_height == last_height:
                break

            last_height = new_height

        links = page.evaluate("""
        () => Array.from(document.querySelectorAll('a'))
        .map(a => ({
            href: a.href,
            text: a.innerText || ''
        }))
        """)

        browser.close()

    found = 0

    for item in links:
        href = item.get("href", "")
        text = item.get("text", "").strip()

        if "ingatlan" not in href:
            continue

        price_match = re.search(
            r"(\\d[\\d\\s\\.]*)\\s*Ft",
            text,
            re.I
        )

        if not price_match:
            continue

        try:
            price = int(
                re.sub(r"\\D", "", price_match.group(1))
            )
        except Exception:
            continue

        if price > MAX_AR:
            continue

        auction_id = href.split("/")[-1]

        size = extract_size(text)
        nm_ar = ""

        if size and size > 0:
            nm_ar = f"{round(price / size):,} Ft/m²"

        if auction_id not in db:

            db[auction_id] = {
                "price": price
            }

            maps_url = (
                "https://www.google.com/maps/search/?api=1&query="
                + quote_plus(text[:100])
            )

            message = (
                f"🚨 <b>ÚJ 2 MILLIÓ ALATTI INGATLAN!</b>\\n\\n"
                f"💰 <b>Kikiáltási ár:</b> {price:,} Ft\\n"
                f"📐 <b>Méret:</b> {size if size else 'N/A'}\\n"
                f"🧮 <b>Nm ár:</b> {nm_ar if nm_ar else 'N/A'}\\n\\n"
                f"🗺️ <a href='{maps_url}'>Google Maps</a>\\n"
                f"🔗 <a href='{href}'>Ingatlan adatlap</a>"
            )

            send_telegram_message(message)
            found += 1

    save_database(db)

    if found == 0:
        send_telegram_message(
            "✅ Futtatás sikeres. Nem érkezett új 2 millió Ft alatti ingatlan."
        )

    print(f"Új találatok: {found}")


if __name__ == "__main__":
    main()
