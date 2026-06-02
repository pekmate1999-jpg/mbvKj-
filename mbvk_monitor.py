import os
import json
import requests
import re
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
    print("🚀 MBVK Kikényszerített Monitor elindult...")
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
            viewport={"width": 1280, "height": 1000} # Megnövelt magasság, hogy több kártya látsszon
        )
        page = context.new_page()

        # A-TERV: Hálózati API elcsípése
        def on_response(response):
            if "publicapi/auction/list" in response.url:
                try:
                    json_res = response.json()
                    if "items" in json_res and len(json_res["items"]) > 0:
                        captured_data["items"] = json_res["items"]
                        print(f"🎯 SIKER! Elcsípve {len(json_res['items'])} db hirdetés a hálózatból!")
                except:
                    pass

        page.on("response", on_response)

        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> Oldal megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="load", timeout=60000)
        
        # --- KIKÉNYSZERÍTÉS ---
        # Megvárjuk, amíg a fő tartalmi rész vagy bármilyen kártya/táblázat betöltődik a képernyőre
        print("--> Várakozás a felület elemeire a betöltődés kikényszerítéséhez...")
        try:
            page.wait_for_selector("mat-card, .mat-row, tr, .card, [role='row'], .search-result", timeout=15000)
            # Finom görgetés lefelé, hogy az Angular biztosan aktiválja a Lazy Loadingot
            page.evaluate("window.scrollBy(0, 400);")
            page.wait_for_timeout(5000)
        except Exception as e:
            print(f"⚠️ Várakozási időtúllépés a szelektorra, de folytatjuk: {e}")

        # B-TERV: Ha az API elcsípés csődöt mondana, közvetlenül a DOM elemekből szedjük ki az adatokat
        if len(captured_data["items"]) == 0:
            print("🔄 B-TERV: API nem reagált, adatok kinyerése közvetlenül a felületi kártyákból...")
            cards = page.locator("mat-card, .card, [role='row'], .mat-row").all()
            for card in cards:
                try:
                    text_content = card.inner_text()
                    if not text_content or "ft" not in text_content.lower():
                        continue
                    
                    text_lower = text_content.lower()
                    if any(bad in text_lower for bad in ["ingóság", "személygépkocsi", "üzletrész", "gép "]):
                        continue

                    # Ügyszám keresése
                    ugyszam_match = re.search(r'(\d+\.V\.\d+/\d+)|(\d+\.V\.\d+)', text_content)
                    ugyszam = ugyszam_match.group(0) if ugyszam_match else "Ismeretlen"
                    
                    # Generálunk egy egyedi belső ID-t a kártya tartalmából, ha nincs meg a pontos azonosító
                    digits = "".join(filter(str.isdigit, text_content))
                    auction_id = digits[:12] if len(digits) > 5 else "0"

                    # Ár kiszedése tiszta számként
                    kikialtasi_ar = 0
                    for word in text_content.replace(".", "").replace(",", "").split():
                        if word.isdigit() and 5 <= len(word) <= 9:
                            kikialtasi_ar = int(word)
                            break

                    # Helyszín keresése normálisan (az első sor, ami nem ügyszám és nem ár)
                    telepules = "MBVK Ingatlan"
                    for line in [l.strip() for l in text_content.split("\n") if l.strip()]:
                        if not re.search(r'\d+\.V\.\d+', line) and not line.replace(" ", "").isdigit() and len(line) > 3:
                            if "kikiáltási" not in line.lower() and "árverés" not in line.lower() and "licit" not in line.lower():
                                telepules = line[:35]
                                break

                    backup_auctions.append({
                        "id": auction_id,
                        "caseNumber": ugyszam,
                        "city": telepules,
                        "minBid": kikialtasi_ar,
                        "categoryCode": "INGATLAN"
                    })
                except:
                    continue

        browser.close()

    # Adatok összefésülése (vagy az A-terv vagy a B-terv nyert)
    auctions = captured_data["items"] if len(captured_data["items"]) > 0 else backup_auctions
    print(f"📊 Összesen feldolgozható hirdetés száma: {len(auctions)}")

    if not auctions:
        print("📭 Egyik módszerrel sem sikerült adatot kinyerni.")
        send_telegram_message("🤖 *MBVK Monitor:* A script lefutott, de az oldalon jelenleg nem talált feldolgozható hirdetést. A kapcsolat működik!")
        return

    new_found_count = 0

    for item in auctions:
        try:
            if item.get("categoryCode", "") != "INGATLAN":
                continue

            auction_id = str(item.get("id"))
            ugyszam = item.get("caseNumber", "Nincs megadva")
            telepules = item.get("city", "Ismeretlen")
            kikialtasi_ar = int(item.get("minBid", 0))

            # Ha az ID-nk valós, a gyári linkre mutatunk, ha generált (B-terv), akkor a fő keresőre
            full_link = f"https://arveres.mbvk.hu/#/reszletek?id={auction_id}" if len(auction_id) < 10 else "https://arveres.mbvk.hu/#/kereses"

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
                        f"🔗 [Ugrás az MBVK árverési adatlapra]({full_link})"
                    )
                    
                    send_telegram_message(üzenet)
                    
                    if TESZT_MOD and new_found_count >= 3:
                        break
        except:
            continue

    if TESZT_MOD:
        send_telegram_message("✅ *MBVK Monitor Teszt:* A kettős szűrésű futás lezárult. Ellenőrizd a kapott adatok tisztaságát!")
    else:
        if new_found_count > 0:
            save_database(old_records)
        else:
            send_telegram_message("✅ *MBVK Monitor:* A keresés sikeresen lefutott. Jelenleg nincs új ingatlan 2 000 000 Ft alatt.")

if __name__ == "__main__":
    main()
