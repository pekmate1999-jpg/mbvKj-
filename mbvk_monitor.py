import os
import json
import requests
import time
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

# === TESZT MÓD ===
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
    print("🚀 MBVK Hibrid Adat-Elcsípő Monitor elindult...")
    old_records = load_database()
    
    # Ebbe a listába mentjük el, amit a hálózatból elkapunk
    captured_data = {"items": []}

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

        # Eseménykezelő: Figyeljük a háttérben futó hálózati válaszokat
        def on_response(response):
            if "publicapi/auction/list" in response.url:
                try:
                    json_res = response.json()
                    if "items" in json_res and len(json_res["items"]) > 0:
                        captured_data["items"] = json_res["items"]
                        print(f"🎯 SIKER! Elcsípve {len(json_res['items'])} db hirdetés a hálózati forgalomból!")
                except Exception as e:
                    print(f"❌ Hiba a háttér-JSON olvasásakor: {e}")

        page.on("response", on_response)

        # Megnyitjuk a rendes szűrt felületet
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> Oldal megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="load", timeout=60000)
        
        # Aktív várakozási ciklus: maximum 20 másodpercig várunk, hogy az elcsípő funkció megteljen adattval
        print("--> Várakozás az API csomag beérkezésére...")
        for _ in range(20):
            if len(captured_data["items"]) > 0:
                break
            page.wait_for_timeout(1000)

        browser.close()

    # Ellenőrizzük, hogy sikerült-e az elcsípés
    auctions = captured_data["items"]
    if not auctions:
        print("📭 Sikertelen elcsípés, a lista üres maradt.")
        send_telegram_message("⚠️ *MBVK Monitor:* Nem sikerült elcsípni az adatcsomagot a böngészőből sem. Kérlek indítsd újra a futtatást!")
        return

    print(f"📊 Összesen feldolgozható tiszta API hirdetés száma: {len(auctions)}")
    new_found_count = 0

    for item in auctions:
        try:
            # Csak az INGATLAN kategóriát engedjük át (biztonsági szűrés, ha becsúszna más)
            if item.get("categoryCode", "") != "INGATLAN":
                continue

            auction_id = str(item.get("id"))
            ugyszam = item.get("caseNumber", "Nincs megadva")
            telepules = item.get("city", "Ismeretlen")
            kikialtasi_ar = int(item.get("minBid", 0))

            # Az MBVK új belső részletes adatlap URL mintája az ID alapján!
            full_link = f"https://arveres.mbvk.hu/#/reszletek?id={auction_id}"

            # --- SZŰRÉS (2 000 000 Ft vagy Teszt üzemmód) ---
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
            print(f"❌ Hiba az egyik elem feldolgozásánál: {e}")
            continue

    if TESZT_MOD:
        send_telegram_message(f"✅ *MBVK Monitor Teszt:* Sikeresen elcsípve a hálózatból! Ha a fenti üzenetekben a helyszín, ügyszám és a link is makulátlan, állítsd át a `TESZT_MOD = False` értékre!")
    else:
        if new_found_count > 0:
            save_database(old_records)
            print("💾 Új találatok elmentve.")
        else:
            print("😴 Nincs új találat.")
            send_telegram_message("✅ *MBVK Monitor:* A keresés sikeresen lefutott. Jelenleg nincs a feltételeknek megfelelő új ingatlan 2 000 000 Ft alatt.")

if __name__ == "__main__":
    main()
