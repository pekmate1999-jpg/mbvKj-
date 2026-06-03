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
    print("🚀 Licitnapló Precíziós Monitor elindult...")
    old_records = load_database()
    
    # Készre paraméterezett Licitnapló URL (0 - 2 000 000 Ft, 1/1, beköltözhető, tehermentes, aktív)
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
    
    # A Licitnapló oldalon minden árverési kártyának 'auction-card' vagy hasonló osztálya van, 
    # de a legbiztosabb, ha a hirdetményeket tartalmazó linkes konténereket fogjuk meg.
    # Az oldalon a hirdetések az 'auction-item' vagy simán az aukciókra mutató linkek alapján azonosíthatók.
    hirdetesek = soup.find_all("a", href=re.compile(r"/arveres/"))
    
    print(f"📋 Talált nyers hirdetés linkek száma: {len(hirdetesek)}")
    
    new_found_count = 0
    feldolgozott_idk = set()

    for hirdetes in hirdetesek:
        try:
            href = hirdetes.get("href")
            # Példából kiszedjük az egyedi Licitnapló ID-t (pl. /arveres/12345 -> 12345)
            auction_id_match = re.search(r"/arveres/(\d+)", href)
            if not auction_id_match:
                continue
                
            auction_id = f"ln_{auction_id_match.group(1)}"
            
            # Duplikációk kiszűrése egy futáson belül
            if auction_id in feldolgozott_idk:
                continue
            feldolgozott_idk.add(auction_id)

            # --- ADATOK KINYERÉSE A KÁRTYÁBÓL ---
            # A Licitnapló struktúrájában a kártyán belüli szövegeket szedjük szét
            card_text = hirdetes.get_text(separator="\n")
            lines = [l.strip() for l in card_text.split("\n") if l.strip()]
            
            if not lines:
                continue

            # 1. Helyszín: Általában a kártya legelső sora a cím/település
            telepules = lines[0]
            
            # 2. Ár keresése
            kikialtasi_ar = 0
            for line in lines:
                if "ft" in line.lower():
                    digits = "".join(filter(str.isdigit, line))
                    if digits and 100000 <= int(digits) <= 2000000:
                        kikialtasi_ar = int(digits)
                        break
            
            # 3. Ügyszám keresése (hátha szerepel a kártyán, ha nem, a linkből azonosítunk)
            ugyszam = "Lásd az adatlapon"
            for line in lines:
                ugyszam_match = re.search(r'\d+\.V\.\d+(?:/\d+)?', line)
                if ugyszam_match:
                    ugyszam = ugyszam_match.group(0)
                    break

            full_link = f"https://licitnaplo.hu{href}"

            # --- ÉLES SZŰRÉS ÉS TELEGRAM KÜLDÉS ---
            if auction_id not in old_records:
                new_found_count += 1
                old_records.append(auction_id)

                ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"
                
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
            print(f"⚠️ Hiba egy kártya feldolgozásakor: {e}")
            continue

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új tétel elmentve az adatbázisba.")
    else:
        print("😴 Nem találtam új, feltételeknek megfelelő ingatlant.")

if __name__ == "__main__":
    main()
