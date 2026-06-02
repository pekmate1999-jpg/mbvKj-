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

def scrape_direct_url():
    arveresek = []
    
    with sync_playwright() as p:
        print("--> Virtuális Chrome indítása...")
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        
        # Valódi felhasználói környezet emulálása (User-Agent trükk)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()
        
        # Közvetlen szűrt URL megnyitása (Ingatlan, Aktív, Tehermentes, Beköltözhető, 1/1)
        # Az MBVK paraméterezése alapján az URL-be ágyazzuk a kérést
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        
        print(f"--> Közvetlen URL megnyitása: {target_url}")
        page.goto(target_url, wait_until="load", timeout=60000)
        
        # Várunk, amíg az Angular befejezi a táblázat vagy a kártyák kirajzolását
        print("--> Várakozás a tartalom lerendelésére...")
        page.wait_for_timeout(8000)
        
        # Kivesszük a kész HTML-t
        html_content = page.content()
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Keressük meg az összes linket, ami az árverési adatlapokra mutat
        # (Pl: /arveres/123456 vagy hasonló struktúra)
        links = soup.find_all('a', href=True)
        print(f"--> Összesen talált linkek száma az oldalon: {len(links)}")
        
        for link_tag in links:
            href = link_tag['href']
            # Megnézzük, hogy a link egy árverési hirdetményre mutat-e
            if "arveres" in href or "hirdetmeny" in href or "adatlap" in href:
                text_content = link_tag.get_text(separator=" ", strip=True).lower()
                
                # Próbáljuk megkeresni a szülő elemet (kártyát/sort), amiben az ár is benne van
                parent = link_tag.find_parent(['div', 'tr', 'mat-card'])
                parent_text = parent.get_text(separator=" ", strip=True) if parent else link_tag.get_text(strip=True)
                
                # Egyedi azonosító kinyerése a linkből
                prop_id = ''.join(filter(str.isdigit, href))
                if not prop_id:
                    continue
                    
                # Ár kinyerése a szövegből (pl. "1 000 000" vagy "1.000.000")
                kikialtasi_ar = 0
                clean_text = parent_text.replace(" ", "").replace(".", "").replace(",", "")
                
                # Keressük a számcsoportokat a tisztított szövegben
                import re
                szamok = re.findall(r'\d+', clean_text)
                for szam in szamok:
                    if len(szam) >= 6 and len(szam) <= 9: # Reális ingatlan árak (100.000 és 999.000.000 között)
                        valaszthato_ar = int(szam)
                        # Ha a szám után ott volt a 'ft' vagy 'huf' a szövegben, akkor ez lesz az ár
                        if "ft" in clean_text or "huf" in clean_text:
                            kikialtasi_ar = valaszthato_ar
                            break
                
                # Település kinyerése (egyszerűsített verzió a szövegből)
                telepules = "Részletek az adatlapon"
                words = parent_text.split()
                if len(words) > 0:
                    telepules = " ".join(words[:4]) # Az első pár szó általában tartalmazza a helyszínt

                full_link = href if href.startswith("http") else "https://arveres.mbvk.hu" + href

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
    print("🚀 MBVK URL-Alapú Monitor elindult.")
    old_records = load_database()
    
    properties = scrape_direct_url()
    print(f"📊 Összesen feldolgozott releváns hirdetés: {len(properties)}")
    
    new_found = False
    
    for prop in properties:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]
        
        # --- ÁR SZŰRÉS: Max 2 000 000 HUF ---
        # Ha 0 maradt az ár (nem sikerült kivágni a szövegből), átengedjük, hogy ne maradj le róla!
        if kikialtasi_ar <= 2000000 or kikialtasi_ar == 0:
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)
                
                ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"
                
                üzenet = (
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                    f"🔗 [Ugrás az MBVK árverési adatlapra]({prop['link']})"
                )
                print(f"✨ Találat kiküldése: ID {prop_id}")
                send_telegram_message(üzenet)
                
    if new_found:
        save_database(old_records)
        print("💾 Új rekordok elmentve.")
    else:
        print("😴 Nem találtam a feltételeknek megfelelő új ingatlant.")

if __name__ == "__main__":
    main()
