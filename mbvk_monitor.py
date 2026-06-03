import os
import json
import requests
import re
import html
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

def load_database():
    """Betölti az adatbázist, Set-ként tér vissza a villámgyors keresésért."""
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try: 
                return set(json.load(f))
            except Exception: 
                return set()
    return set()

def save_database(data_set):
    """Visszaalakítja listává a Set-et, és elmenti JSON-be."""
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(list(data_set), f, ensure_ascii=False, indent=4)

def send_telegram_message(text):
    """Elküldi az üzenetet HTML parse móddal, hogy elkerülje a Markdown-hibákat."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Hiányzó Telegram token vagy Chat ID!")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, 
        "text": text, 
        "parse_mode": "HTML",
        "disable_web_page_preview": True # Ne generáljon zavaró méretű link-előnézetet
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"❌ Telegram küldési hiba: {e}")

def get_property_details(page, url):
    """Bejárja az adatlapot és robusztusan kinyeri a kért adatokat."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        raw_html = page.content()
        body_text = page.inner_text("body")
        
        # 1. Cím kinyerése (Első körben H1 címsor, ha nincs, akkor URL-ből)
        cim = "Ismeretlen ingatlan"
        h1_element = page.locator("h1").first
        if h1_element.count() > 0:
            cim = h1_element.inner_text().strip()
        else:
            cim = url.split('/')[-1].replace('-', ' ').title()

        # 2. Kikiáltási ár kinyerése a rejtett strukturált (JSON-LD) adatokból
        ar = "Új találat (Ár nem olvasható)"
        price_match = re.search(r'"price"\s*:\s*(\d+)', raw_html)
        if price_match:
            # Szám formázása, pl: 1200000 -> 1 200 000 Ft
            formazott_ar = f"{int(price_match.group(1)):,} Ft".replace(",", " ")
            ar = formazott_ar

        # 3. Régi regex keresések a body_text-en
        telekm_match = re.search(r"(?:Telekméret|Telek területe|Teleknagyság)[:\s]+([\d\s]+)\s*(?:nm|m2|négyzetméter)", body_text, re.IGNORECASE)
        leiras_match = re.search(r"(?:Leírás|Megjegyzés)[:\s]+(.+?)(?:\n|$)", body_text, re.IGNORECASE)
        nm_ar_match = re.search(r"(?:Négyzetméterár|Ft/m2)[:\s]+([\d\s]+)\s*Ft", body_text, re.IGNORECASE)
        
        # Leírás levágása 200 karakternél (hogy ne spammelje tele a Telegramot)
        leiras = leiras_match.group(1).strip() if leiras_match else "Nincs leírás."
        if len(leiras) > 200:
            leiras = leiras[:200] + "..."

        return {
            "cim": cim,
            "ar": ar,
            "telekméret": telekm_match.group(1).strip() if telekm_match else "Nincs megadva",
            "leiras": leiras,
            "nm_ar": nm_ar_match.group(1).strip() if nm_ar_match else "N/A"
        }
        
    except Exception as e:
        print(f"⚠️ Hiba az adatlap olvasásakor ({url}): {e}")
        return {"cim": "Hiba", "ar": "Hiba", "telekméret": "Hiba", "leiras": "Nem olvasható", "nm_ar": "N/A"}

def main():
    old_records = load_database()
    target_url = "https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&ar=0-1000000&epuletTipus=&besorolas=&forras=&status="
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = context.new_page()
        
        try:
            page.goto(target_url, wait_until="networkidle", timeout=60000)
            
            # Releváns linkek begyűjtése
            links = page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href)")
            unique_links = list(set([l for l in links if "licitnaplo.hu/ingatlan/" in l and not any(x in l for x in ["status=", "ar="])]))

            for link in unique_links:
                clean_id = "".join(filter(str.isalnum, link.split("/")[-1]))
                
                # Ugrás a következőre, ha már láttuk
                if clean_id in old_records: 
                    continue
                
                print(f"🔍 Új ingatlan feldolgozása: {link}")
                details = get_property_details(page, link)
                
                # Cím keresése Google Térképen (hivatalos Google Maps Search API paraméterezés)
                maps_url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(details['cim'])}"
                
                # Vizuálisan megegyezik a képernyőfotóddal, de HTML (<b> tag) alapon megy, ami bombabiztos
                üzenet = (
                    f"🏠 <b>Cím:</b> {html.escape(details['cim'])}\n"
                    f"💰 <b>Kikiáltási ár:</b> {html.escape(details['ar'])}\n"
                    f"📏 <b>Telekméret:</b> {html.escape(details['telekméret'])} nm\n"
                    f"💵 <b>Négyzetméterár:</b> {html.escape(details['nm_ar'])} Ft/nm\n"
                    f"📝 <b>Leírás:</b> {html.escape(details['leiras'])}\n\n"
                    f"🗺️ <a href='{maps_url}'>Megtekintés Google Maps-en</a>\n"
                    f"🔗 <a href='{link}'>Ugrás az ingatlan adatlapjára</a>"
                )
                
                send_telegram_message(üzenet)
                old_records.add(clean_id)
                
        except Exception as e:
            print(f"❌ Végzetes hiba futás közben: {e}")
        finally:
            browser.close()
    
    save_database(old_records)

if __name__ == "__main__":
    main()
