import os
import json
import requests
import re
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

def scrape_direct_url():
    arveresek = []
    
    with sync_playwright() as p:
        print("--> Virtuális Chrome indítása...")
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()
        
        # Közvetlen szűrt URL megnyitása az MBVK-n
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        
        print(f"--> URL megnyitása: {target_url}")
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        
        # Várunk, amíg az Angular teljesen felépíti a kártyákat a képernyőre
        print("--> Várakozás a hirdetések betöltődésére...")
        page.wait_for_timeout(8000)
        
        html_content = page.content()
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Kigyűjtjük az összes linket
        links = soup.find_all('a', href=True)
        print(f"--> Összesen talált nyers link: {len(links)}")
        
        for link_tag in links:
            href = str(link_tag['href'])
            
            # --- CRITICAL: Csak az MBVK belső részletes adatlapjait engedjük át! ---
            # Kidobjuk a google, support, cookie és egyéb külső linkeket
            if "google" in href or "support" in href or "cookie" in href or "javascript" in href:
                continue
                
            # Az MBVK belső részletes linkjei általában 'reszletek', 'arveres' vagy konkrét azonosítót tartalmaznak a hash után
            if "reszletek" in href or "arveres" in href or any(char.isdigit() for char in href):
                
                # Kiszedjük a tiszta számot (ID-t) a linkből
                prop_id = ''.join(filter(str.isdigit, href))
                if not prop_id or len(prop_id) < 4: # Ha nincs benne rendes azonosító, átugorjuk
                    continue
                    
                parent = link_tag.find_parent(['div', 'tr', 'mat-card'])
                parent_text = parent.get_text(separator=" ", strip=True) if parent else link_tag.get_text(strip=True)
                
                # Ár kinyerése intelligensen
                kikialtasi_ar = 0
                clean_text = parent_text.replace(" ", "").replace(".", "").replace(",", "")
                szamok = re.findall(r'\d+', clean_text)
                for szam in szamok:
                    if 6 <= len(szam) <= 9: # Reális ingatlan ár 100.000 és 999.000.000 Ft között
                        kikialtasi_ar = int(szam)
                        break
                
                # Település meghatározása a szöveg elejéből
                telepules = "MBVK Ingatlan"
                vonalak = [v.strip() for v in parent_text.split("\n") if v.strip()]
                if vonalak:
                    telepules = vonalak[0][:40] # Általában a kártya legelső sora a helyszín

                # Normalizáljuk a link formátumát
                if href.startswith("#"):
                    full_link = f"https://arveres.mbvk.hu/{href}"
                elif href.startswith("/"):
                    full_link = f"https://arveres.mbvk.hu{href}"
                else:
                    full_link = href

                if not any(x['id'] == prop_id for x in arveresek):
                    arveresek.append({
                        "id": prop_id,
                        "telepules": telepules,
                        "ar": kikialtasi_ar,
                        "link": full_link
                    })
                    
        browser.close()
        
    return arveresek

def main():
    print("🚀 MBVK Tisztított URL Monitor elindult.")
    old_records = load_database()
    
    properties = scrape_direct_url()
    print(f"📊 Talált valódi hirdetések száma szűrés után: {len(properties)}")
    
    new_found = False
    
    for prop in properties:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]
        
        # --- ÁR SZŰRÉS: Max 2 000 000 HUF ---
        if kikialtasi_ar <= 2000000 or kikialtasi_ar == 0:
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)
                
                ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"
                
                üzenet = (
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Infó:* {prop['telepules']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                    f"🔗 [Ugrás az MBVK árverési adatlapra]({prop['link']})"
                )
                print(f"✨ Értesítés küldése: ID {prop_id}")
                send_telegram_message(üzenet)
                
    if new_found:
        save_database(old_records)
        print("💾 Új rekordok sikeresen elmentve.")
    else:
        print("😴 Nem találtam a feltételeknek megfelelő új ingatlant.")

if __name__ == "__main__":
    main()
