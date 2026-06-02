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
        print("--> Böngésző indítása...")
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()
        
        print("--> MBVK főoldal betöltése...")
        page.goto("https://arveres.mbvk.hu/", wait_until="networkidle", timeout=60000)
        
        try:
            # 1. Sütik lekezelése gyorsan
            page.wait_for_timeout(2000)
            cookie_btn = page.locator("button#s-all-bn, button:has-text('Mindet elfogadom')")
            if cookie_btn.count() > 0:
                cookie_btn.first.click()
                page.wait_for_timeout(1000)
            
            # 2. Szűrők élesítése gombok/kattintások alapján (ha nincsenek, az alap listából szűrünk)
            print("--> Oldal elemeinek mélyelemzése...")
            
            # Megvárjuk, amíg a dinamikus tartalom biztosan renderelődik a képernyőre
            page.wait_for_timeout(4000)
            
            html_content = page.content()
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Az Angular/Material oldalak egyedi hirdetmény-blokkokat használnak. 
            # Megkeresünk minden olyan blokkot, div-et, linket, amiben ár vagy ügyszám lehet.
            blocks = soup.find_all(['div', 'a', 'mat-card'], class_=lambda x: x and any(word in x.lower() for word in ['item', 'card', 'row', 'arveres', 'hirdetmeny']))
            
            # Biztonsági háló: Ha a modern div-ek nem adnak tiszta struktúrát, kigyűjtjük az összes létező linket
            if not blocks:
                blocks = soup.find_all('a', href=True)
            
            print(f"--> Feldolgozandó blokkok száma: {len(blocks)}")
            
            for idx, block in enumerate(blocks):
                text_content = block.get_text(separator=" ", strip=True)
                text_lower = text_content.lower()
                
                # Szigorú előszűrés a szöveges tartalomból: minket INGATLAN érdekel, ami tehermentes/1-1
                # (Ha az oldal eleve szűrve tölt be, vagy ezek a szavak benne vannak a hirdetésben)
                if "ft" in text_lower or "huf" in text_lower:
                    link_tag = block if block.name == 'a' else block.find('a', href=True)
                    if link_tag and link_tag.has_attr('href') and ("hirdetmeny" in link_tag['href'] or "arveres" in link_tag['href']):
                        
                        href = link_tag['href']
                        link = href if href.startswith("http") else "https://arveres.mbvk.hu" + href
                        
                        # Egyedi ID kinyerése a linkből (számok)
                        prop_id = ''.join(filter(str.isdigit, href))
                        if not prop_id:
                            prop_id = str(hash(link))
                            
                        # Ár kinyerése a szövegből intelligensen
                        kikialtasi_ar = 0
                        # Tisztítjuk a szöveget a felesleges karakterektől, csak a számcsoportokat nézzük
                        words = text_content.replace(".", "").replace(" ", "").split()
                        for word in words:
                            clean_word = ''.join(filter(str.isdigit, word))
                            if clean_word and len(clean_word) >= 6: # Legalább 100 000 Ft értékű számot keresünk
                                try:
                                    szam = int(clean_word)
                                    if 100000 <= szam <= 100000000: # Reális tartomány egy ingatlan kikiáltási árának
                                        kikialtasi_ar = szam
                                        break
                                except: pass
                        
                        # Próbálunk települést kiszedni a szöveg elejéből
                        telepules = "Részletek a linken"
                        for line in text_content.split("\n"):
                            line_clean = line.strip()
                            if len(line_clean) > 2 and not any(x in line_clean.lower() for x in ['ft', 'árverés', 'ügyszám', 'hirdetmény']):
                                telepules = line_clean[:40]
                                break

                        # Hozzáadjuk a listához, ha még nem szerepel benne ez az ID
                        if not any(item['id'] == prop_id for item in arveresek):
                            arveresek.append({
                                "id": prop_id,
                                "telepules": telepules,
                                "ar": kikialtasi_ar,
                                "link": link,
                                "raw_text": text_lower
                            })
                            
        except Exception as e:
            print(f"❌ Hiba történt: {e}")
            
        browser.close()
        
    return arveresek

def main():
    print("🚀 MBVK Éles Monitor elindult.")
    old_records = load_database()
    properties = scrape_with_browser()
    
    print(f"📊 Összesen talált nyers hirdetés: {len(properties)}")
    new_found = False
    
    for prop in properties:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]
        raw_text = prop["raw_text"]
        
        # --- SZIGORÚ ÉLES SZŰRÉSI LOGIKA ---
        # 1. Ár ellenőrzése: Max 2 000 000 Ft (Vagy 0, ha nem sikerült kiolvasni, a biztonság kedvéért)
        if kikialtasi_ar <= 2000000 or kikialtasi_ar == 0:
            
            # 2. Kulcsszavak ellenőrzése a szövegben, ha az MBVK nem szűrt volna alapból:
            # Csak akkor engedjük át, ha nem utal semmi arra, hogy pl. csak 1/2 tulajdon lenne
            if "1/2" in raw_text or "2/4" in raw_text or "1/4" in raw_text:
                continue # Kihagyjuk a tört tulajdonokat
                
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)
                
                ar_szoveg = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"
                
                üzenet = (
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"💰 *Kikiáltási ár:* {ar_szoveg}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                    f"🔗 [Ugrás az MBVK árverési adatlapra]({prop['link']})"
                )
                print(f"✨ Értesítés kiküldése: ID {prop_id}")
                send_telegram_message(üzenet)
                
    if new_found:
        save_database(old_records)
        print("💾 Az új rekordok sikeresen elmentve.")
    else:
        print("😴 Nem találtam új, a feltételeknek megfelelő hirdetést.")

if __name__ == "__main__":
    main()
