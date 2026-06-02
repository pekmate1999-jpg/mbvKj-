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
        print("Böngésző indítása...")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        # Megnyitjuk az MBVK-t
        page.goto("https://arveres.mbvk.hu/", wait_until="networkidle")
        
        print("Szűrők beállítása a felületen...")
        # Kiválasztjuk az Ingatlan kategóriát (az elküldött HTML alapján a szelektorok)
        try:
            # Megvárjuk, amíg a szűrőpanel vagy a gombok betöltenek
            page.wait_for_selector("input, select, button")
            
            # TODO / TESZT: Először szűrés nélkül kérjük le a hirdetményeket, 
            # hogy lássuk, egyáltalán bejönnek-e az adatok a táblázatba!
            # Megnyomjuk a kereső gombot (ha van fix gomb, azonosítjuk az osztálya vagy szövege alapján)
            # Az elküldött HTML-ben a kereső gombra kattintunk:
            search_button = page.locator("button:has-text('Keresés'), input[type='submit']")
            if search_button.count() > 0:
                search_button.first.click()
                page.wait_for_timeout(3000) # Várunk 3 másodpercet a találatokra
            
            # Kivesszük a teljes betöltött HTML-t
            html_content = page.content()
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Megkeressük az összes hirdetmény kártyát vagy táblázat sort
            # Az Angularos MBVK-n ezek általában div-ek vagy 'tr' elemek
            for row in soup.find_all(['tr', 'div'], class_=lambda x: x and ('adat-sor' in x or 'arveres' in x or 'card' in x)):
                text = row.get_text()
                if "Ft" in text or "HUF" in text:
                    # Megpróbálunk linket találni hozzá
                    link_tag = row.find('a', href=True)
                    link = "https://arveres.mbvk.hu" + link_tag['href'] if link_tag else "https://arveres.mbvk.hu"
                    
                    # Kiszedjük az ügyszámot vagy egyedi azonosítót (ha van benne szám)
                    prop_id = ''.join(filter(str.isdigit, link)) if link_tag else str(hash(text))
                    
                    # Ár kinyerése
                    kikialtasi_ar = 0
                    for word in text.split():
                        if any(c.isdigit() for c in word) and ("000" in word or len(word) >= 6):
                            try:
                                szam = int(''.join(filter(str.isdigit, word)))
                                if szam > 100000:
                                    kikialtasi_ar = szam
                                    break
                            except: pass

                    arveresek.append({
                        "id": prop_id,
                        "telepules": text[:50].replace("\n", " "),
                        "ar": kikialtasi_ar,
                        "link": link
                    })
                    
            # Ha a fenti nem talált semmit, de vannak linkek az oldalon, mentsük el azokat
            if not arveresek:
                for a_tag in soup.find_all('a', href=True):
                    if "hirdetmeny" in a_tag['href'] or "adat" in a_tag['href']:
                        arveresek.append({
                            "id": ''.join(filter(str.isdigit, a_tag['href'])),
                            "telepules": a_tag.get_text(strip=True),
                            "ar": 0,
                            "link": "https://arveres.mbvk.hu" + a_tag['href']
                        })
                        
        except Exception as e:
            print(f"Hiba a böngésző szimuláció közben: {e}")
            
        browser.close()
    return arveresek

def main():
    old_records = load_database()
    properties = scrape_with_browser()
    
    print(f"Összesen talált hirdetés a listában: {len(properties)}")
    new_found = False
    
    for prop in properties:
        prop_id = prop["id"]
        if not prop_id: continue
        
        # Első körben minden 20 millió alatti (vagy ismeretlen áru) elemet átengedünk tesztnek!
        if prop["ar"] <= 20000000:
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)
                
                üzenet = (
                    f"🚨 *MBVK TESZT TALÁLAT!*\n\n"
                    f"ℹ️ *Infó:* {prop['telepules']}\n"
                    f"💰 *Becsült ár:* {prop['ar']:,} HUF\n\n"
                    f"🔗 [Adatlap megnyitása]({prop['link']})"
                )
                send_telegram_message(üzenet)
                
    if new_found:
        save_database(old_records)
    else:
        print("Nem találtam új hirdetést.")

if __name__ == "__main__":
    main()
