import os
import json
import requests
import re
from urllib.parse import quote_plus
import html
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

def load_database():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try: 
                # Halmazként (set) térünk vissza a gyorsabb keresésért
                return set(json.load(f))
            except Exception as e:
                print(f"⚠️ Hiba az adatbázis betöltésekor: {e}")
                return set()
    return set()

def save_database(data_set):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        # A JSON mentéshez vissza kell alakítani listává
        json.dump(list(data_set), f, ensure_ascii=False, indent=4)

def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Hiányzó Telegram hitelesítő adatok!")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        # HTML formátum használata a biztonságosabb formázásért
        response = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, 
            "text": text, 
            "parse_mode": "HTML",
            "disable_web_page_preview": True # Ne generáljon hatalmas előnézeteket
        }, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"❌ Telegram küldési hiba: {e}")

def get_property_details(page, url):
    """Bejárja az adatlapot és kinyeri a kért adatokat."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # PRO TIPP: Cseréld le a 'body'-t arra a specifikus div-re, amiben az adatok vannak!
        # page.wait_for_selector(".listing-details-container", timeout=5000)
        content = page.inner_text("body")
        
        telekm_match = re.search(r"(?:Telekméret|Telek területe|Teleknagyság)[:\s]+([\d\s]+)\s*(?:nm|m2|négyzetméter)", content, re.IGNORECASE)
        leiras_match = re.search(r"(?:Leírás|Megjegyzés)[:\s]+(.+?)(?:\n|$)", content, re.IGNORECASE)
        nm_ar_match = re.search(r"(?:Négyzetméterár|Ft/m2)[:\s]+([\d\s]+)\s*Ft", content, re.IGNORECASE)
        
        return {
            "telekméret": telekm_match.group(1).strip() if telekm_match else "Nincs megadva",
            "leiras": (leiras_match.group(1).strip()[:150] + "...") if leiras_match else "Nincs leírás.",
            "nm_ar": nm_ar_match.group(1).strip() if nm_ar_match else "N/A"
        }
    except Exception as e:
        print(f"⚠️ Hiba az adatlap olvasásakor ({url}): {e}")
        return {"telekméret": "Hiba", "leiras": "Nem olvasható", "nm_ar": "N/A"}

def main():
    old_records = load_database()
    target_url = "https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&ar=0-1000000&epuletTipus=&besorolas=&forras=&status="
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        page = context.new_page()
        
        try:
            page.goto(target_url, wait_until="networkidle", timeout=60000)
            
            # Csak azokat a linkeket gyűjtjük be, amik tényleg adatlapra mutatnak
            # Ezt a CSS szelektort ('a') érdemes pontosítani, pl. '.card-title a'
            links = page.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href)")
            unique_links = list(set([l for l in links if "licitnaplo.hu/" in l and not any(x in l for x in ["status=", "ar="])]))

            for link in unique_links:
                clean_id = "".join(filter(str.isalnum, link.split("/")[-1]))
                
                # Gyors (O(1)) keresés a Set-ben
                if clean_id in old_records: 
                    continue
                
                print(f"🔍 Új találat feldolgozása: {link}")
                details = get_property_details(page, link)
                
                # Cím kinyerése és escape-elése a HTML formátumhoz
                raw_title = link.split('/')[-1].replace('-', ' ').title()
                safe_title = html.escape(raw_title)
                safe_leiras = html.escape(details['leiras'])
                
                # Valódi Google Maps kereső link
                maps_url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(raw_title)}"
                
                # HTML formázott üzenet
                üzenet = (
                    f"🏠 <b>Cím:</b> {safe_title}\n"
                    f"💰 <b>Kikiáltási ár:</b> Új találat\n"
                    f"📏 <b>Telekméret:</b> {details['telekméret']} nm\n"
                    f"💵 <b>Négyzetméterár:</b> {details['nm_ar']} Ft/nm\n"
                    f"📝 <b>Leírás:</b> {safe_leiras}\n\n"
                    f"🗺️ <a href='{maps_url}'>Megtekintés Google Maps-en</a>\n"
                    f"🔗 <a href='{link}'>Ugrás az ingatlan adatlapjára</a>"
                )
                
                send_telegram_message(üzenet)
                old_records.add(clean_id) # Set-hez adjuk hozzá
                
        except Exception as e:
            print(f"❌ Végzetes hiba futás közben: {e}")
        finally:
            browser.close()
    
    save_database(old_records)

if __name__ == "__main__":
    main()
