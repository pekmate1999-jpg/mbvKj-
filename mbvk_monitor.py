import os
import json
import requests
import re
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

# === TESZT MÓD ===
# Most True-ra van állítva, hogy BÁRMILYEN hirdetést elkapjon (autót, gépet is), így ellenőrizni tudjuk a formázást!
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
    print("🚀 MBVK Kikényszerített Teszt Monitor elindult...")
    old_records = load_database()
    
    captured_data = {"items": []}
    backup_auctions = []

    with sync_playwright() as p:
        print("--> Virtuális Chrome indítása...")
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 1200}
        )
        page = context.new_page()

        # A-TERV: Háttér API elcsípése (bármilyen listát elfogadunk a teszthez)
        def on_response(response):
            if "publicapi/auction/list" in response.url:
                try:
                    json_res = response.json()
                    if "items" in json_res and len(json_res["items"]) > 0:
                        captured_data["items"] = json_res["items"]
                        print(f"🎯 SIKER! Elcsípve {len(json_res['items'])} db nyers hirdetés az API-ból!")
                except:
                    pass

        page.on("response", on_response)

        # SZŰRETLEN FŐOLDAL megnyitása a teszthez, hogy biztosan legyen találat a képernyőn
        target_url = "https://arveres.mbvk.hu/"
        print(f"--> Teszt URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="load", timeout=60000)
        
        print("--> Keresés gomb megnyomása a felületen...")
        try:
            # Megnyomjuk a főoldali kereső gombot, hogy az összes aktív hirdetést betöltse a felületre
            page.locator("button:has-text('Keresés'), .search-button, btn-primary").first.click()
            page.wait_for_timeout(5000)
            page.evaluate("window.scrollBy(0, 500);")
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"⚠️ Nem sikerült a gombra kattintani, de görgetünk: {e}")
            page.evaluate("window.scrollBy(0, 500);")
            page.wait_for_timeout(5000)

        # B-TERV: Ha az API elcsípés elcsúszott, közvetlenül a felületi kártyákból szedjük ki a nyers adatokat
        if len(captured_data["items"]) == 0:
            print("🔄 B-TERV: Felületi elemek közvetlen feldolgozása...")
            # Minden létező kártyaszerű elemet megnézünk
            cards = page.locator("mat-card, .card, [role='row'], .mat-row, .auction-item").all()
            print(f"📋 Talált kártya elemek száma a képernyőn: {len(cards)}")
            
            for card in cards:
                try:
                    text_content = card.inner_text()
                    if not text_content or len(text_content.strip()) < 20:
                        continue
                    
                    # Ügyszám keresése
                    ugyszam_match = re.search(r'(\d+\.V\.\d+/\d+)|(\d+\.V\.\d+)', text_content)
                    ugyszam = ugyszam_match.group(0) if ugyszam_match else "Ismeretlen"
                    
                    # Egyedi belső ID generálása a szövegből a teszthez
                    digits = "".join(filter(str.isdigit, text_content))
                    auction_id = digits[:10] if len(digits) > 4 else "12345"

                    # Ár kiszedése
                    kikialtasi_ar = 0
                    for word in text_content.replace(".", "").replace(",", "").split():
                        if word.isdigit() and 4 <= len(word) <= 9:
                            kikialtasi_ar = int(word)
                            break

                    # Település / Megnevezés kiszedése (Az első értelmes sor)
                    lines = [l.strip() for l in text_content.split("\n") if l.strip()]
                    telepules = lines[0][:40] if lines else "MBVK Tétel"

                    backup_auctions.append({
                        "id": auction_id,
                        "caseNumber": ugyszam,
                        "city": telepules,
                        "minBid": kikialtasi_ar
                    })
                except:
                    continue

        browser.close()

    # Összefésülés
    auctions = captured_data["items"] if len(captured_data["items"]) > 0 else backup_auctions
    print(f"📊 Összesen feldolgozható nyers teszt hirdetés száma: {len(auctions)}")

    if not auctions:
        send_telegram_message("🤖 *MBVK Monitor:* A teszt lefutott, de a szűretlen oldalon sem találtam elemet. Kérlek indítsd újra!")
        return

    new_found_count = 0

    for item in auctions:
        try:
            auction_id = str(item.get("id", "0"))
            ugyszam = item.get("caseNumber", "Nincs megadva")
            telepules = item.get("city", "Ismeretlen")
            kikialtasi_ar = int(item.get("minBid", 0))

            # Teszt link generálás (ha van rendes ID, akkor az adatlapra, ha nincs, a főoldalra)
            full_link = f"https://arveres.mbvk.hu/#/reszletek?id={auction_id}" if auction_id != "12345" else "https://arveres.mbvk.hu/"

            new_found_count += 1
            ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"

            üzenet = (
                f"⚠️ *TESZT ÜZEMMÓD (BÁRMILYEN KATEGÓRIA)*\n\n"
                f"🚨 *ÚJ TALÁLAT!*\n\n"
                f"📍 *Megnevezés/Helyszín:* {telepules}\n"
                f"🔹 *Ügyszám:* {ugyszam}\n"
                f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                f"📋 *Feltételek:* Tesztelés alatt\n\n"
                f"🔗 [Ugrás a hirdetményre]({full_link})"
            )
            
            send_telegram_message(üzenet)
            
            # Csak 2 darabot küldünk ki, hogy lásd a formázást
            if new_found_count >= 2:
                break
        except:
            continue

    send_telegram_message("✅ *MBVK Monitor:* A szűretlen teszt futás lezárult. Ha megérkezett a 2 minta üzenet, jelezd vissza!")

if __name__ == "__main__":
    main()
