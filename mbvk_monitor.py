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
    print("🚀 Licitnapló Blokkelemző Monitor elindult...")
    old_records = load_database()
    
    # A képeden látható pontos, készre szűrt URL
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
    
    # A Licitnapló kártyái egyszerű szöveges blokkként is tökéletesen elkülöníthetők a bennük lévő ' Ft' jelölés miatt.
    # Megkeressük az összes olyan div elemet, aminek a szövegében szerepel az ár és a cím.
    # A Bootstrap alapú elrendezés miatt a 'col-' osztályú konténereket fogjuk meg, amik a kártyákat tartják.
    containers = soup.find_all("div", class_=lambda x: x and ('col-' in x or 'card' in x))
    
    arveresek = []
    feldolgozott_szovegek = set()

    for container in containers:
        # Csak azokat a blokkokat nézzük, amikben van konkrét ár (Ft) és nem a lábléc vagy fejléc részei
        text = container.get_text(separator="\n").strip()
        if "000 Ft" not in text or "Keresel" in text or "Kapcsolat" in text:
            continue
            
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        if len(lines) < 2:
            continue
            
        # Egyedi szűrés, hogy egy hirdetést ne olvassunk be többször a DOM egymásba ágyazottsága miatt
        tiszta_blokk = " | ".join(lines[:4])
        if tiszta_blokk in feldolgozott_szovegek:
            continue
        feldolgozott_szovegek.add(tiszta_blokk)

        try:
            # --- ADATBÁNYÁSZAT A KÁRTYÁBÓL ---
            telepules = lines[0]
            cim = lines[1] if len(lines) > 1 else telepules
            
            # Összegyűjtjük az árat
            kikialtasi_ar = 0
            for line in lines:
                if "ft" in line.lower():
                    digits = "".join(filter(str.isdigit, line))
                    if digits and 50000 <= int(digits) <= 2000000:
                        kikialtasi_ar = int(digits)
                        break
            
            if kikialtasi_ar == 0:
                continue

            # Mivel nincs fix azonosító a HTML-ben, a címből és az árból képzünk egyedi ID-t az adatbázisnak
            auction_id = "ln_" + "".join(filter(str.isalnum, cim)) + f"_{kikialtasi_ar}"

            arveresek.append({
                "id": auction_id,
                "telepules": telepules,
                "cim": cim,
                "ar": kikialtasi_ar
            })
        except:
            continue

    print(f"📊 Megtalált és tisztított ingatlanok száma: {len(arveresek)}")
    new_found_count = 0

    for prop in arveresek:
        auction_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        if auction_id not in old_records:
            new_found_count += 1
            old_records.append(auction_id)

            ar_kiiras = f"{kikialtasi_ar:,} HUF"
            üzenet = (
                f"🚨 *ÚJ OLCSÓ INGATLAN TALÁLAT!* (Licitnapló)\n\n"
                f"📍 *Település:* {prop['telepules']}\n"
                f"🏠 *Pontos cím:* {prop['cim']}\n"
                f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                f"🔗 [Megnyitás a Licitnapló Keresőben]({target_url})"
            )
            send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új ingatlan elmentve.")
    else:
        print("😴 Nincs új találat.")

if __name__ == "__main__":
    main()
