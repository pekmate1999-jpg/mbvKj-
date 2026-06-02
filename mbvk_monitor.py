import os
import json
import requests
from bs4 import BeautifulSoup

# Telegram konfiguráció a GitHub Secrets-ből
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

def load_database():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return []
    return []

def save_database(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload)
        if response.status_code != 200:
            print(f"Telegram hiba: {response.text}")
    except Exception as e:
        print(f"Nem sikerült üzenetet küldeni a Telegramra: {e}")

def scrape_mbvk():
    url = "https://arveres.mbvk.hu/arverezok/index.php"
    
    # Pontos adatok, amiket az MBVK szervere a gombnyomáskor vár
    payload = {
        "nav": "arveres",
        "szures": "1",
        "arveres_allapota": "AKTIV",
        "kategoria": "INGATLAN",
        "tulajdoni_hanyad": "1/1",
        "tehermentes": "IGEN",
        "bekoltozheto": "IGEN",
        "kereses": "Keresés"
    }
    
    # Nagyon fontos: Elhitetjük a szerverrel, hogy egy igazi Windows-os Chrome böngésző vagyunk
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "hu-HU,hu;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://arveres.mbvk.hu",
        "Referer": "https://arveres.mbvk.hu/arverezok/index.php"
    }
    
    print("MBVK ingatlanok lekérése (Böngésző szimuláció)...")
    try:
        response = requests.post(url, data=payload, headers=headers)
        if response.status_code != 200:
            print(f"Hiba az oldal elérésekor: {response.status_code}")
            return []
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Keressük meg a hirdetményeket tartalmazó táblázat sorait
        arveresek = []
        
        # Az MBVK struktúrájában a fő táblázat sorai vagy linkjei:
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            # Ha a link tartalmazza az árverés azonosítóját (hirdetmeny_id vagy hasonlót)
            if "hirdetmeny_id" in href or "arveres" in href:
                # Kiszedjük az egyedi azonosítót a linkből
                # Pl: index.php?nav=arveresadat&hirdetmeny_id=123456
                parts = href.split("hirdetmeny_id=")
                if len(parts) > 1:
                    prop_id = parts[1].split("&")[0]
                else:
                    continue
                    
                # Megpróbáljuk kikapni a szöveget, ami a táblázat sorában van (Ár, Település)
                parent_row = a_tag.find_parent('tr')
                if parent_row:
                    cells = [cell.get_text(strip=True) for cell in parent_row.find_all('td')]
                    
                    # Alapértelmezett értékek, ha nem tudjuk pontosan az oszlopot
                    telepules = "Ismeretlen"
                    kikialtasi_ar = 0
                    
                    # Ha sikerült beolvasni az oszlopokat, megpróbáljuk kinyerni a várost és az árat
                    if len(cells) >= 3:
                        telepules = cells[1] # Általában a 2. oszlop a település
                        
                        # Megkeressük az árat (ahol Ft vagy HUF van, vagy tisztán szám)
                        for cell_text in cells:
                            if "Ft" in cell_text or "HUF" in cell_text or cell_text.replace(" ", "").isdigit():
                                try:
                                    # Kiszűrjük a számokat a szövegből (pl "1 000 000 Ft" -> 1000000)
                                    szam = int(''.join(filter(str.isdigit, cell_text)))
                                    if szam > 10000: # Biztonsági szűrés, hogy ne egy ügyszámot nézzen árnak
                                        kikialtasi_ar = szam
                                        break
                                except:
                                    continue
                    
                    # Ha nem találtunk árat a sorban, megnézzük magát a link szövegét
                    if kikialtasi_ar == 0:
                        link_text = a_tag.get_text(strip=True)
                        if "Ft" in link_text:
                            try:
                                kikialtasi_ar = int(''.join(filter(str.isdigit, link_text)))
                            except:
                                pass

                    arveresek.append({
                        "id": prop_id,
                        "telepules": telepules,
                        "ar": kikialtasi_ar,
                        "link": "https://arveres.mbvk.hu/arverezok/" + href
                    })
                    
        return arveresek
        
    except Exception as e:
        print(f"Hiba a futás során: {e}")
        return []

def main():
    old_records = load_database()
    properties = scrape_mbvk()
    
    print(f"Összesen talált hirdetés a listában: {len(properties)}")
    new_found = False
    
    for prop in properties:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]
        
        # SZŰRÉS: Csak a 2 000 000 Ft alattiak érdekelnek minket!
        # Ha a script nem tudta biztosan beolvasni az árat (0 maradt), 
        # akkor is átengedjük, nehogy lemaradjunk egy jó vételről!
        if kikialtasi_ar <= 2000000 or kikialtasi_ar == 0:
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)
                
                ar_szoveg = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"
                
                üzenet = (
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Település / Info:* {prop['telepules']}\n"
                    f"💰 *Kikiáltási ár:* {ar_szoveg}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                    f"🔗 [Ugrás az MBVK árverési adatlapra]({prop['link']})"
                )
                
                send_telegram_message(üzenet)
                
    if new_found:
        save_database(old_records)
        print("Új találatok elmentve, Telegram üzenetek elküldve.")
    else:
        print("Nem történt új találat.")

if __name__ == "__main__":
    main()
