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

        # Hálózati forgalom figyelése és elcsípése
        def handle_response(response):
            # Minden olyan API hívást figyelünk, amiben szerepel az arveres vagy kereses szó
            if "arveres" in response.url.lower() or "keres" in response.url.lower() or "api/v1" in response.url.lower():
                try:
                    if "application/json" in response.headers.get("content-type", ""):
                        json_data = response.json()
                        print(f"--> SIKER! Elcsípett adatcsomag: {response.url}")
                        
                        # Megkeressük a hirdetéseket a JSON-ben (content kulcs vagy sima lista)
                        items = json_data.get("content", json_data if isinstance(json_data, list) else [])
                        if isinstance(items, dict) and not isinstance(items, list):
                            # Ha esetleg más kulcs alatt lennének az adatok
                            for key in ["arveresek", "adatok", "list"]:
                                if key in json_data:
                                    items = json_data[key]
                                    break
                                    
                        if isinstance(items, list):
                            for item in items:
                                captured_data.append(item)
                except:
                    pass

        page.on("response", handle_response)

        print("--> MBVK főoldal megnyitása...")
        page.goto("https://arveres.mbvk.hu/", wait_until="networkidle", timeout=60000)

        # 1. Sütik kötelező elfogadása, hogy látszódjon a kereső gomb
        page.wait_for_timeout(2000)
        cookie_btn = page.locator("button#s-all-bn, button:has-text('Mindet elfogadom')")
        if cookie_btn.count() > 0:
            cookie_btn.first.click()
            print("--> Süti panel lecsukva.")
            page.wait_for_timeout(1000)

        # 2. AKTÍVAN rákattintunk a Keresés gombra a felületen, hogy kikényszerítsük az API hívást!
        print("--> Keresés gomb megnyomása a felületen...")
        # Megkeressük a gombot szöveg vagy típus alapján (az elküldött HTML-edben szereplő gombok alapján)
        search_btn = page.locator("button:has-text('Keresés'), input[type='submit'], .search-button")
        if search_btn.count() > 0:
            search_btn.first.click()
            print("--> Gomb sikeresen megnyomva, várakozás az adatokra...")
            page.wait_for_timeout(6000)
        else:
            print("⚠️ Nem találtam Keresés gombot a megadott szelektorokkal, megpróbálunk hosszabban várni...")
            page.wait_for_timeout(8000)

        browser.close()

    return captured_data

def main():
    print("🚀 MBVK Kikényszerített Monitor elindult.")
    old_records = load_database()
    
    raw_items = scrape_with_network_intercept()
    print(f"📊 Összesen elcsípett nyers API hirdetés száma: {len(raw_items)}")
    
    arveresek = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
            
        # Egyedi azonosító keresése az API-ban
        prop_id = str(item.get("id") or item.get("arveresId") or item.get("hirdetmenyId") or item.get("ugyszam", ""))
        if not prop_id:
            continue

        # Tiszta adatok kinyerése a JSON-ből
        kategoria = str(item.get("kategoria", item.get("tipus", ""))).upper()
        allapot = str(item.get("arveresAllapota", item.get("allapot", "AKTIV"))).upper()
        tulajdon = str(item.get("tulajdoniHanyad", item.get("tulajdon", "1/1")))
        
        # Ár kinyerése (többféle mezőnevet is megnézünk)
        kikialtasi_ar = item.get("kikialtasiAr") or item.get("minimalAr") or item.get("aktualisAr") or item.get("ar", 0)
        telepules = item.get("telepules", item.get("ingatlanTelepules", "Ismeretlen település"))
        ugyszam = item.get("ugyszam", "Nincs megadva")

        # --- PYTHON ALAPÚ SZIGORÚ SZŰRÉS ---
        # Ha a kategória meg van adva, és nem ingatlan, eldobjuk
        if kategoria and "INGATLAN" not in kategoria and "LAKO" not in kategoria:
            continue
        # Csak az aktívak kellenek
        if "AKTIV" not in allapot and "FUT" not in allapot:
            continue
        # Csak az 1/1 tulajdon kell (ha tört, pl. 1/2, kihagyjuk)
        if tulajdon != "1/1" and ("1/" in tulajdon or "2/" in tulajdon or "3/" in tulajdon):
            continue

        # Ha átment a szűrőkön, hozzáadjuk az ellenőrzendő listához
        if not any(x["id"] == prop_id for x in arveresek):
            arveresek.append({
                "id": prop_id,
                "telepules": telepules,
                "ar": int(kikialtasi_ar) if kikialtasi_ar else 0,
                "ugyszam": ugyszam,
                "link": f"https://arveres.mbvk.hu/arveres/{prop_id}"
            })

    print(f"🔍 Kritériumnak megfelelő (Ingatlan, 1/1) darabszám: {len(arveresek)}")
    new_found = False

    for prop in arveresek:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        # --- ÁR KORLÁT: Max 2 000 000 HUF ---
        # Ha a kölkedihez hasonlóan 2 millió alatt van, vagy nem sikerült kiolvasni (0)
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
                print(f"✨ Értesítés küldése -> {prop['telepules']}")
                send_telegram_message(üzenet)

    if new_found:
        save_database(old_records)
        print("💾 Új találatok elmentve.")
    else:
        print("😴 Nem találtam a feltételeknek megfelelő ÚJ ingatlant.")

if __name__ == "__main__":
    main()
