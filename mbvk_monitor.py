import os
import json
import requests
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

# === TESZT MÓD BEÁLLÍTÁS ===
# Ha True, akkor az első futásnál minden talált ingatlant kiküld a Telegramra, függetlenül az ártól és az adatbázistól!
# Ha megbizonyosodtál róla, hogy jó, állítsd át False-ra!
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
    print("🚀 MBVK API-Alapú Precíziós Monitor elindult...")
    old_records = load_database()
    captured_auctions = []

    with sync_playwright() as p:
        print("--> Virtuális böngésző indítása...")
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        # Elcsípjük a háttérben futó JSON válaszokat
        def handle_response(response):
            if "publicapi/auction/list" in response.url:
                try:
                    json_data = response.json()
                    if "items" in json_data:
                        captured_auctions.extend(json_data["items"])
                        print(f"📡 API: Sikeresen elcsípve {len(json_data['items'])} db nyers hirdetés!")
                except Exception as e:
                    print(f"❌ Nem sikerült parsolni a JSON-t: {e}")

        page.on("response", handle_response)

        # Közvetlen keresési link (Ingatlan, Aktív, 1/1, tehermentes, beköltözhető)
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(5000) # Várunk egy kicsit az API válasz beérkezésére

        browser.close()

    if not captured_auctions:
        print("📭 Az API nem adott vissza adatokat.")
        send_telegram_message("⚠️ *MBVK Monitor:* Nem sikerült adatokat kinyerni az MBVK API-ból. Kérlek indítsd újra a tesztet!")
        return

    print(f"📊 Összesen feldolgozható hirdetés az API-ból: {len(captured_auctions)}")
    new_found_count = 0

    for item in captured_auctions:
        try:
            # Szigorú szűrés az API-ból érkező tiszta adatok alapján
            kategoria = item.get("categoryCode", "")
            if kategoria != "INGATLAN":
                continue

            # Egyedi azonosító és adatok kinyerése
            auction_id = str(item.get("id"))
            ugyszam = item.get("caseNumber", "Nincs megadva")
            telepules = item.get("city", "Ismeretlen település")
            
            # Ár kezelése (kikiáltási ár)
            kikialtasi_ar = int(item.get("minBid", 0)) 

            # Közvetlen, gyári link az adatlapra az ID alapján!
            full_link = f"https://arveres.mbvk.hu/#/reszletek?id={auction_id}"

            # --- SZŰRÉSI LOGIKA ---
            # Alapértelmezett korlát: 2 000 000 HUF
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
                    
                    print(f"✨ Telegram értesítés küldése: {telepules} ({ar_kiiras})")
                    send_telegram_message(üzenet)
                    
                    # Teszt módban csak az első 3 találatot küldjük ki, hogy ne spameljük szét a csatornát
                    if TESZT_MOD and new_found_count >= 3:
                        print("🛑 Teszt limit elérve (3 db).")
                        break
        except Exception as e:
            print(f"❌ Hiba az egyik elem feldolgozásakor: {e}")
            continue

    if TESZT_MOD:
        send_telegram_message(f"✅ *MBVK Monitor Teszt:* Sikeresen lefutott teszt módban! Talált ingatlanok száma: {len(captured_auctions)}. Ha a fenti 3 mintának jó a linkje és a helyszíne, állítsd át a `TESZT_MOD = False` értékre a kódban!")
    else:
        if new_found_count > 0:
            save_database(old_records)
            print("💾 Új találatok elmentve.")
        else:
            print("😴 Nincs új találat.")
            send_telegram_message("✅ *MBVK Monitor:* A keresés sikeresen lefutott. Jelenleg nincs a feltételeknek megfelelő új ingatlan 2 000 000 Ft alatt.")

if __name__ == "__main__":
    main()
