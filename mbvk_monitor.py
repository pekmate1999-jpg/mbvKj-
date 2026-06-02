import os
import json
import requests

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

def get_mbvk_properties():
    # Az MBVK belső kereső végpontja
    api_url = "https://arveres.mbvk.hu/api/v1/arveresek/kereses"
    
    # A kért szűrési feltételek JSON formátumban
    payload = {
        "kategoria": "INGATLAN",
        "arveresAllapota": "AKTIV",
        "tulajdoniHanyad": "1/1",
        "tehermentes": True,
        "bekoltozheto": True,
        "page": 0,
        "size": 50  # Az első 50 legfrissebb elemet nézzük egyszerre
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    
    print("MBVK ingatlanok lekérése...")
    try:
        response = requests.post(api_url, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            # Az MBVK API struktúrája szerint a 'content' kulcs alatt vannak a találatok
            return data.get("content", [])
        else:
            print(f"Hiba az MBVK lekérésnél (Státuszkód: {response.status_code})")
            return []
    except Exception as e:
        print(f"Hálózati hiba az MBVK elérése közben: {e}")
        return []

def main():
    old_records = load_database()
    properties = get_mbvk_properties()
    
    new_found = False
    
    for prop in properties:
        # Az MBVK rendszerében az egyedi azonosító általában az 'id' vagy 'arveresId'
        prop_id = str(prop.get("id") or prop.get("arveresId") or prop.get("ugyszam"))
        if not prop_id:
            continue
            
        # Kikiáltási ár / Minimálár ellenőrzése (az API-ból kinyerve, általában 'kikialtasiAr' vagy 'minAr')
        # Ha nincs megadva, biztonsági okokból 0-nak vesszük, hogy ne hagyjuk ki
        kikialtasi_ar = prop.get("kikialtasiAr") or prop.get("minimalAr") or prop.get("ar", 0)
        
        # Lokáció és ügyszám kinyerése az értesítéshez
        telepules = prop.get("telepules", "Ismeretlen település")
        ugyszam = prop.get("ugyszam", "Nincs megadva")
        
        # Szigorú szűrés a 2 000 000 HUF alatti árakra
        if kikialtasi_ar <= 2000000:
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)
                
                # Egyedi közvetlen link generálása az adatlaphoz
                link = f"https://arveres.mbvk.hu/arveres/{prop_id}"
                
                # Telegram üzenet összeállítása modern Markdown formátumban
                üzenet = (
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Település:* {telepules}\n"
                    f"🔹 *Ügyszám:* {ugyszam}\n"
                    f"💰 *Kikiáltási ár:* {kikialtasi_ar:,} HUF\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                    f"🔗 [Ugrás az MBVK árverési adatlapra]({link})"
                )
                
                send_telegram_message(üzenet)
                
    if new_found:
        save_database(old_records)
        print("Új találatok elmentve, Telegram üzenetek kiküldve.")
    else:
        print("Nem történt új találat a megadott szűrések alapján.")

if __name__ == "__main__":
    main()
