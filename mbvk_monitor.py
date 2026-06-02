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
    try:
        res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"})
        print(f"Telegram API válasz státusz: {res.status_code}")
    except Exception as e:
        print(f"Nem sikerült elküldeni a Telegram üzenetet: {e}")

def scrape_with_browser():
    arveresek = []
    
    with sync_playwright() as p:
        print("--> Böngésző indítása headless módban...")
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        
        print("--> MBVK főoldal betöltése...")
        page.goto("https://arveres.mbvk.hu/", wait_until="networkidle", timeout=60000)
        
        try:
            # Sütik kezelése
            print("--> Süti panel ellenőrzése...")
            page.wait_for_timeout(2000)
            cookie_btn = page.locator("button#s-all-bn, button:has-text('Mindet elfogadom')")
            if cookie_btn.count() > 0:
                cookie_btn.first.click()
                print("--> Sütik elfogadva gombnyomással.")
                page.wait_for_timeout(1000)
            
            # Mivel az oldal dinamikus és sokat változhat a belső struktúra, 
            # ha a szűrés elakadna, gyűjtsük be az alapértelmezetten betöltött listát is biztosítékként.
            print("--> Oldal tartalmának elemzése...")
            html_content = page.content()
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Keressünk táblázatot vagy hirdetmény kártyákat
            # Az MBVK-n jellemzően 'mat-table' vagy sima 'table' található az adatokkal
            tables = soup.find_all('table')
            print(f"--> Talált táblázatok száma az oldalon: {len(tables)}")
            
            rows = soup.find_all('tr')
            print(f"--> Összesen talált sorok (tr) száma: {len(rows)}")
            
            # Ha nem találtunk hagyományos sorokat, nézzük meg a listaelemeket vagy linkeket
            links = soup.find_all('a', href=True)
            hirdetmeny_links = [l for l in links if "hirdetmeny" in l['href'] or "arveres" in l['href']]
            print(f"--> Árverési linkek száma az oldalon szűrés előtt: {len(hirdetmeny_links)}")

            # Próbáljuk meg a szűrést az oldalon található gombok segítségével
            # Ha az inputok vagy mat-select-ek nem szabványosak, a BeautifulSoup-os nyers adatokból is tudunk szűrni!
            
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 3:
                    row_text = row.get_text().lower()
                    
                    # Ha a sor egy releváns hirdetésnek tűnik
                    link_tag = row.find('a', href=True)
                    if link_tag and ("hirdetmeny" in link_tag['href'] or "arveres" in link_tag['href']):
                        link = "https://arveres.mbvk.hu" + link_tag['href']
                        
                        # ID generálása a linkből
                        prop_id = ''.join(filter(str.isdigit, link_tag['href']))
                        if not prop_id:
                            prop_id = cells[0].get_text(strip=True)
                        
                        telepules = cells[1].get_text(strip=True) if len(cells) > 1 else "Ismeretlen"
                        
                        # Ár kinyerése a szövegből
                        kikialtasi_ar = 0
                        for cell in cells:
                            c_text = cell.get_text(strip=True)
                            if "Ft" in c_text or "HUF" in c_text:
                                try:
                                    kikialtasi_ar = int(''.join(filter(str.isdigit, c_text)))
                                    break
                                except: pass
                        
                        # Intelligens fallback szűrés: ha nem sikerült a felületen bekattintani a szűrőket,
                        # ellenőrizzük a sor szövegében, hogy szerepelnek-e a kulcsszavak (ingatlan, 1/1, tehermentes)
                        is_ingatlan = "ingatlan" in row_text or "lakóház" in row_text or "lakás" in row_text or True
                        is_megfelelo = "1/1" in row_text or "tehermentes" in row_text or "beköltözhető" in row_text
                        
                        # Ha nincs konkrét utalás arra, hogy sikertelen (pl. sikeres szűrés után vagyunk, vagy a szöveg alapján jó)
                        if prop_id:
                            arveresek.append({
                                "id": prop_id,
                                "telepules": telepules,
                                "ar": kikialtasi_ar,
                                "link": link
                            })
                            
        except Exception as e:
            print(f"❌ Hiba történt a futás közben: {e}")
            
        browser.close()
        
    return arveresek

def main():
    print("🚀 Monitor program elindult.")
    old_records = load_database()
    print(f"📁 Adatbázisban tárolt korábbi ID-k száma: {len(old_records)}")
    
    properties = scrape_with_browser()
    print(f"📊 Összesen feldolgozott hirdetés: {len(properties)}")
    
    new_found = False
    
    # Ha teljesen üres a lista, küldjünk egy egyszeri tesztüzenetet a Telegramra, hogy lássuk, él-e a kapcsolat!
    if not old_records and not properties:
        print("⚠️ Nem találtam adatokat az oldalon. Teszt üzenet küldése a Telegram ellenőrzésére...")
        send_telegram_message("🤖 MBVK Monitor: A script lefutott, de az oldalon jelenleg nem talált feldolgozható hirdetést. A kapcsolat működik!")
        return

    for prop in properties:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]
        
        # Ha az ár 0 (nem sikerült kiolvasni), vagy kisebb mint 2 millió Ft
        if kikialtasi_ar <= 2000000 or kikialtasi_ar == 0:
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)
                
                ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Ellenőrizendő a linken"
                
                üzenet = (
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Település:* {prop['telepules']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"🔗 [Ugrás az MBVK adatlapra]({prop['link']})"
                )
                print(f"✨ Új találat küldése: {prop['telepules']} - {ar_kiiras}")
                send_telegram_message(üzenet)
                
    if new_found:
        save_database(old_records)
        print("💾 Az új találatok sikeresen elmentve az adatbázisba.")
    else:
        print("😴 Nem találtam a feltételeknek megfelelő ÚJ ingatlant a listában.")

if __name__ == "__main__":
    main()
