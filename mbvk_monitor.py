import os
import json
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

def load_database():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: return []
    return []

def save_database(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"})

def scrape_with_browser():
    arveresek = []
    
    with sync_playwright() as p:
        print("Böngésző indítása sandbox nélkül...")
        # Aargs hozzáadása a GitHub Actions kompatibilitás miatt
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        
        # Beállítunk egy fix ablakméretet, hogy minden gomb látható legyen
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        
        print("MBVK oldal betöltése...")
        page.goto("https://arveres.mbvk.hu/", wait_until="networkidle", timeout=60000)
        
        try:
            # 1. Sütik elfogadása (ha felugrik a panel, rákattintunk a 'Mindet elfogadom' gombra)
            cookie_btn = page.locator("button#s-all-bn, button:has-text('elfogadom'), button:has-text('Accept')")
            if cookie_btn.count() > 0:
                cookie_btn.first.click()
                print("Sütik elfogadva.")
                page.wait_for_timeout(1000)

            # 2. Szűrők beállítása
            print("Szűrési feltételek beállítása...")
            
            # Kategória -> INGATLAN
            # Megkeressük a legördülő menüt vagy gombot a kategóriához
            page.locator("select[name='kategoria'], select:has-text('Ingatlan')").select_option(label="Ingatlan")
            
            # Árverés állapota -> AKTÍV
            page.locator("select[name='arveres_allapota']").select_option(value="AKTIV")
            
            # Tulajdoni hányad -> 1/1
            page.locator("select[name='tulajdoni_hanyad']").select_option(value="1/1")
            
            # Tehermentes -> IGEN
            page.locator("select[name='tehermentes']").select_option(value="IGEN")
            
            # Beköltözhető -> IGEN
            page.locator("select[name='bekoltozheto']").select_option(value="IGEN")
            
            print("Keresés indítása...")
            # Megnyomjuk a kereső gombot
            search_button = page.locator("button:has-text('Keresés'), input[type='submit']")
            search_button.first.click()
            
            # Megvárjuk, amíg a hálózati forgalom elcsendesedik és betölt a táblázat
            page.wait_for_timeout(5000)
            
            # Kivesszük a lerendelt oldal HTML tartalmát
            html_content = page.content()
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Megkeressük a táblázat sorait (tr) vagy kártyákat
            rows = soup.find_all('tr')
            print(f"Vizsgált HTML sorok száma: {len(rows)}")
            
            for row in rows:
                cells = row.find_all('td')
                if len(cells) >= 4:
                    text_content = row.get_text()
                    
                    # Ha a sor tartalmaz linket az árverésre
                    link_tag = row.find('a', href=True)
                    if link_tag and ("hirdetmeny" in link_tag['href'] or "arveres" in link_tag['href']):
                        link = "https://arveres.mbvk.hu" + link_tag['href']
                        
                        # Ügyszám / ID kiszedése a linkből vagy az első oszlopból
                        prop_id = ''.join(filter(str.isdigit, link_tag['href']))
                        if not prop_id:
                            prop_id = cells[0].get_text(strip=True)
                            
                        telepules = cells[1].get_text(strip=True)
                        
                        # Ár kiszedése (megkeressük azt a cellát, amiben a 'Ft' vagy 'HUF' van)
                        kikialtasi_ar = 0
                        for cell in cells:
                            cell_text = cell.get_text(strip=True)
                            if "Ft" in cell_text or "HUF" in cell_text:
                                try:
                                    kikialtasi_ar = int(''.join(filter(str.isdigit, cell_text)))
                                    break
                                except: pass
                        
                        if prop_id:
                            arveresek.append({
                                "id": prop_id,
                                "telepules": telepules,
                                "ar": kikialtasi_ar,
                                "link": link
                            })
                            
        except Exception as e:
            print(f"Hiba a szűrés vagy adatkinyerés közben: {e}")
            
        browser.close()
        
    return arveresek

def main():
    old_records = load_database()
    properties = scrape_with_browser()
    
    print(f"Összesen talált szűrt hirdetés: {len(properties)}")
    new_found = False
    
    for prop in properties:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]
        
        # SZIGORÚ SZŰRÉS: Max 2 000 000 HUF kikiáltási ár
        # (Ha valamiért 0 maradt az ár a kaparás miatt, átengedjük, hogy manuálisan ellenőrizhesd)
        if kikialtasi_ar <= 2000000 or kikialtasi_ar == 0:
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)
                
                ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Ellenőrizendő az adatlapon"
                
                üzenet = (
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Település:* {prop['telepules']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                    f"🔗 [Ugrás az MBVK adatlapra]({prop['link']})"
                )
                send_telegram_message(üzenet)
                
    if new_found:
        save_database(old_records)
        print("Az új találatok sikeresen kiküldve és elmentve.")
    else:
        print("Nem találtam a feltételeknek megfelelő új ingatlant.")

if __name__ == "__main__":
    main()
