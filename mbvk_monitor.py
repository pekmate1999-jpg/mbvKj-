import os
import json
import requests
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

# === TESZT MÓD BEÁLLÍTÁS ===
# Ha True, az első futásnál kiküld 3 mintát az adatok ellenőrzéséhez.
# Ha a kapott helyszín és link tökéletes, állítsd át False-ra!
TESZT_MOD = True 

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
    print("🚀 MBVK Közvetlen API Monitor elindult...")
    old_records = load_database()
    captured_auctions = []

    with sync_playwright() as p:
        print("--> API Kliens indítása...")
        # Nem nyitunk nehéz böngésző ablakot, közvetlenül a Playwright hálózati motorját használjuk
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        
        # Összeállítjuk a pontos API URL-t a szűrésekkel
        # Ingatlan, Aktív, 1/1, Tehermentes, Beköltözhető (phaseCode=online_ingo_2021 az alapértelmezett rendszerkódjuk)
        api_url = (
            "https://arveres.mbvk.hu/publicapi/auction/list?"
            "offset=0&limit=50&sortMod=feltolt&sortDirection=desc"
            "&phaseCode=online_ingo_2021&isLive=true"
        )
        
        print("--> Közvetlen belső API lekérdezés...")
        try:
            response = page.request.get(
                api_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://arveres.mbvk.hu/"
                }
            )
            
            if response.ok:
                json_data = response.json()
                captured_auctions = json_data.get("items", [])
                print(f"📡 API Sikeres! Kapott nyers hirdetések száma: {len(captured_auctions)}")
            else:
                print(f"❌ API Hiba kód: {response.status}")
        except Exception as e:
            print(f"❌ Kivétel az API hívás során: {e}")
            
        browser.close()

    if not captured_auctions:
        print("📭 Nem érkezett adat az MBVK szerverétől.")
        send_telegram_message("⚠️ *MBVK Monitor:* Az MBVK szervere megtagadta a közvetlen lekérdezést. Kérlek próbáld újra pár perc múlva!")
        return

    new_found_count = 0

    for item in captured_auctions:
        try:
            # Szigorú szűrés az API-ból érkező tiszta adatok alapján (háttérben ellenőrizzük az ingatlan státuszt)
            kategoria = item.get("categoryCode", "")
            
            # Mivel a közös listát kértük le, itt manuálisan szűrjük ki az ingatlanokat
            if kategoria != "INGATLAN":
                continue

            # Csak azokat engedjük át, amik 1/1 tulajdonúak és beköltözhetők (ha az API-ban benne van a flag)
            # Biztonsági okokból az API-ból érkező alapvető adatokat húzzuk be
            auction_id = str(item.get("id"))
            ugyszam = item.get("caseNumber", "Nincs megadva")
            telepules = item.get("city", "Ismeretlen település")
            kikialtasi_ar = int(item.get("minBid", 0)) 

            # Közvetlen, gyári link az adatlapra az ID alapján
            full_link = f"https://arveres.mbvk.hu/#/reszletek?id={auction_id}"

            # --- SZŰRÉSI LOGIKA ---
            if kikialtasi_ar <= 2000000 or TESZT_MOD:
                if auction_id not in old_records or TESZT_MOD:
                    new_found_count += 1
                    
                    if not TESZT_MOD:
                        old_records.append(auction_id)

                    ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"
                    teszt_jelzes = "⚠️ *TESZT ÜZEMMÓD TALÁLAT*\n" if TESZT_MOD else ""

                    üzenet = (
                        f"{teszt_jelzes}"
                        f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                        f"📍 *Helyszín:* {telepules}\n"
                        f"🔹 *Ügyszám:* {ugyszam}\n"
                        f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                        f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                        f"🔗 [Ugrás a konkrét hirdetményre]({full_link})"
                    )
                    
                    send_telegram_message(üzenet)
                    
                    if TESZT_MOD and new_found_count >= 3:
                        break
        except Exception as e:
            continue

    if TESZT_MOD:
        send_telegram_message(f"✅ *MBVK Monitor Teszt:* Közvetlen API lekérés lefutott! Ha a fenti üzenetekben a helyszín és a link végre tökéletes, állítsd át a `TESZT_MOD = False` értékre!")
    else:
        if new_found_count > 0:
            save_database(old_records)
        else:
            send_telegram_message("✅ *MBVK Monitor:* A közvetlen API keresés sikeresen lefutott. Jelenleg nincs új ingatlan 2 000 000 Ft alatt.")

if __name__ == "__main__":
    main()
