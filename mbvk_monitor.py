import os
import json
import requests
from bs4 import BeautifulSoup
import re

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
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"❌ Telegram küldési hiba: {e}")

def main():
    print("🚀 Licitnapló Univerzális Kártya Monitor elindult...")
    old_records = load_database()
    
    # Készre szűrt Licitnapló URL (0 - 2 000 000 Ft, 1/1, beköltözhető, tehermentes, aktív)
    target_url = "https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-2000000&status=aktiv"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        response = requests.get(target_url, headers=headers, timeout=30)
        if response.status_code != 200:
            print(f"❌ Licitnapló hiba: {response.status_code}")
            return
    except Exception as e:
        print(f"❌ Hálózati hiba: {e}")
        return

    soup = BeautifulSoup(response.text, "html.parser")
    
    # Megkeressük az összes olyan div-et, ami hirdetési kártyaként funkcionál az oldalon
    cards = soup.find_all("div", class_=lambda x: x and 'card' in x.lower())
    print(f"📋 Talált nyers kártyák száma a HTML-ben: {len(cards)}")
    
    new_found_count = 0
    feldolgozott_idk = set()

    for card in cards:
        try:
            # Megkeressük a kártyához tartozó linket
            link_element = card.find("a")
            if not link_element:
                continue
                
            href = link_element.get("href")
            if not href or href == "#" or "javascript" in href:
                continue

            # Generálunk egy egyedi azonosítót a linkből a duplikációk ellen
            clean_id = "".join(filter(str.isalnum, href))
            if not clean_id or clean_id in feldolgozott_idk:
                continue
            feldolgozott_idk.add(clean_id)

            card_text = card.get_text(separator="\n")
            lines = [l.strip() for l in card_text.split("\n") if l.strip()]
            
            if not lines:
                continue

            # 1. Helyszín meghatározása: a kártya legelső értelmes sorát vesszük alapul
            telepules = lines[0]
            
            # 2. Ár kinyerése a kártya szövegéből
            kikialtasi_ar = 0
            for line in lines:
                if "ft" in line.lower():
                    digits = "".join(filter(str.isdigit, line))
                    if digits and 50000 <= int(digits) <= 2500000:
                        kikialtasi_ar = int(digits)
                        break

            # Ha a kártyán belül nem találtunk árat, vagy az kiesett a tartományból, biztonsági okokból átugorjuk
            if kikialtasi_ar == 0:
                continue

            # 3. Ügyszám keresése
            ugyszam = "Lásd az adatlapon"
            for line in lines:
                ugyszam_match = re.search(r'\d+\.V\.\d+(?:/\d+)?', line)
                if ugyszam_match:
                    ugyszam = ugyszam_match.group(0)
                    break

            # Teljes elérés létrehozása
            full_link = href if href.startswith("http") else f"https://licitnaplo.hu{href}"
            auction_id = f"ln_{clean_id}"

            # --- SZŰRÉS ÉS TELEGRAM ÉRTESÍTÉS ---
            if auction_id not in old_records:
                new_found_count += 1
                old_records.append(auction_id)

                ar_kiiras = f"{kikialtasi_ar:,} HUF"
                
                üzenet = (
                    f"🚨 *ÚJ OLCSÓ INGATLAN TALÁLAT!* (Licitnapló)\n\n"
                    f"📍 *Helyszín:* {telepules}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n"
                    f"🔹 *Ügyszám:* `{ugyszam}`\n\n"
                    f"🔗 [Ugrás a konkrét hirdetmény adatlapjára]({full_link})"
                )
                
                send_telegram_message(üzenet)
                
        except Exception as e:
            continue

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új ingatlan elmentve.")
    else:
        print("😴 Nincs új találat.")
        # Ezt a sort tesztelés után ki lehet venni, de most bent hagyjuk, hogy lásd: lefutott a kód!
        send_telegram_message("✅ *Licitnapló Monitor:* A keresés sikeresen lefutott, de jelenleg nincs a feltételeknek megfelelő új ingatlan 2 000 000 Ft alatt.")

if __name__ == "__main__":
    main()
