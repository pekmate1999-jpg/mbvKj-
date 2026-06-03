import os
import json
import requests
import re
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright

# Konfiguráció
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"
CONFIG_FILE = "monitor_config.json"

def load_database():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return []
    return []

def save_database(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"❌ Telegram hiba: {e}")

def main():
    old_records = load_database()
    config = {"keyword": "mind", "max_ar": 1000000}
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"https://licitnaplo.hu/?ar=0-{config['max_ar']}&status=aktiv")
        
        # --- VÉGTELEN GÖRGETÉS LOGIKA ---
        print("--> Görgetés folyamatban...")
        last_height = page.evaluate("document.body.scrollHeight")
        while True:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000) # Várakozás a betöltésre
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
        
        # --- ADATGYŰJTÉS ---
        items = page.locator("a[href*='/ingatlan/']").all()
        found_ids = []
        new_items_count = 0
        
        for item in items:
            href = item.get_attribute("href")
            if not href or href in old_records:
                continue
                
            found_ids.append(href)
            new_items_count += 1
            
            # Értesítés (egyszerűsített)
            send_telegram_message(f"🚨 Új ingatlan megjelent: {href}")
            
        if new_items_count > 0:
            old_records.extend(found_ids)
            save_database(old_records)
            print(f"💾 {new_items_count} új tétel elmentve.")
        else:
            print("😴 Nincs új tétel.")
            
        browser.close()

if __name__ == "__main__":
    main()
