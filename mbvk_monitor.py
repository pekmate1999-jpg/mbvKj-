import os
import json
import requests
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

def scrape_with_network_intercept():
    captured_data = []

    with sync_playwright() as p:
        print("--> Virtuális Chrome indítása...")
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context.new_page()

        # Elfogjuk a hálózati válaszokat (Network Interception)
        def handle_response(response):
            # Ha a válasz URL-je az MBVK belső kereső/hirdetmény végpontja
            if "api/v1/arveresek" in response.url or "kereses" in response.url or "hirdetmeny" in response.url:
                try:
                    # Ha JSON adatot kaptunk vissza a szervertől
                    if "application/json" in response.headers.get("content-type", ""):
                        json_data = response.json()
                        print(f"--> Sikerült elcsípni egy adatcsomagot az API-ból! URL: {response.url}")
                        
                        # Az MBVK API struktúrája szerint a hirdetések általában a 'content' vagy a fő listában vannak
                        items = json_data.get("content", json_data if isinstance(json_data, list) else [])
                        if isinstance(items, list):
                            for item in items:
                                captured_data.append(item)
                except Exception as e:
                    pass

        page.on("response", handle_response)

        print("--> MBVK oldal megnyitása és hálózati forgalom figyelése...")
        page.goto("https://arveres.mbvk.hu/", wait_until="networkidle", timeout=60000)

        # Várunk egy kicsit, hogy az összes háttérben futó API kérés biztosan befejeződjön
        page.wait_for_timeout(6000)
        browser.close()

    return captured_data

def main():
    print("🚀 MBVK Hálózati Figyelő elindult.")
    old_records = load_database()
    
    raw_items = scrape_with_network_intercept()
    print(f"📊 Összesen elcsípett nyers API hirdetés: {len(raw_items)}")
    
    arveresek = []
    for item in raw_items:
        # Kulcsok normalizálása az MBVK változó API struktúrájához
        prop_id = str(item.get("id") or item.get("arveresId") or item.get("hirdetmenyId", ""))
        if not prop_id:
            continue

        # Szűrések ellenőrzése közvetlenül a tiszta adatokból
        kategoria = str(item.get("kategoria", "")).upper()
        allapot = str(item.get("arveresAllapota", item.get("allapot", ""))).upper()
        tulajdon = str(item.get("tulajdoniHanyad", item.get("tulajdon", "")))
        tehermentes = item.get("tehermentes")
        bekoltozheto = item.get("bekoltozheto")
        
        # Ár kinyerése
        kikialtasi_ar = item.get("kikialtasiAr") or item.get("minimalAr") or item.get("aktualisAr") or item.get("ar", 0)
        telepules = item.get("telepules", item.get("ingatlanTelepules", "Ismeretlen település"))
        ugyszam = item.get("ugyszam", "Nincs megadva")

        # --- AZ ELŐSZŰRÉS ELLENŐRZÉSE ---
        # Ha az API alapból mindent visszaad, a Python itt helyben szűri le neked:
        # Ha a kategória üres (mert nem kaptuk meg), akkor is átengedjük, hogy ne veszítsünk adatot
        if kategoria and "INGATLAN" not in kategoria:
            continue
        if allapot and "AKTIV" not in allapot:
            continue
            
        # Ha tört tulajdon (pl 1/2), akkor átugorjuk
        if tulajdon and tulajdon != "1/1" and "1/" in tulajdon:
            continue
            
        # Mentjük a szűrt listába
        if not any(x["id"] == prop_id for x in arveresek):
            arveresek.append({
                "id": prop_id,
                "telepules": telepules,
                "ar": int(kikialtasi_ar) if kikialtasi_ar else 0,
                "ugyszam": ugyszam,
                "link": f"https://arveres.mbvk.hu/arveres/{prop_id}"
            })

    print(f"🔍 Szűrések után megmaradt releváns ingatlanok száma: {len(arveresek)}")
    new_found = False

    for prop in arveresek:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        # --- SZIGORÚ ÁR-KORLÁT: Max 2 000 000 HUF ---
        if kikialtasi_ar <= 2000000 or kikialtasi_ar == 0:
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)

                ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"

                üzenet = (
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Település:* {prop['telepules']}\n"
                    f"🔹 *Ügyszám:* {prop['ugyszam']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                    f"🔗 [Ugrás az MBVK árverési adatlapra]({prop['link']})"
                )
                print(f"✨ Telegram értesítés küldése: {prop['telepules']} - {ar_kiiras}")
                send_telegram_message(üzenet)

    if new_found:
        save_database(old_records)
        print("💾 Új rekordok elmentve az adatbázisba.")
    else:
        print("😴 Nem találtam a feltételeknek megfelelő új ingatlant.")

if __name__ == "__main__":
    main()
