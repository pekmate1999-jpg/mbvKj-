import os
import json
import requests
import re
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

# === TESZT ÜZEMMÓD ===
# True: Kiküld 3 mintát a tágabb listából az adatok ellenőrzéséhez.
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
    print("🚀 MBVK Tágított Vizuális Monitor elindult...")
    old_records = load_database()
    arveresek = []

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

        # KITÁGÍTOTT URL TESZTHEZ: Csak az AKTIV és INGATLAN feltétel maradt, hogy biztosan legyen találat!
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV"
        print(f"--> Teszt URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        
        print("--> Várakozás a hirdetések betöltődésére...")
        try:
            page.wait_for_selector("mat-card, .mat-row, tr, .card", timeout=20000)
            page.evaluate("window.scrollBy(0, 400);")
            page.wait_for_timeout(4000)
        except Exception as e:
            print(f"⚠️ Szelektor hiba: {e}")

        cards = page.locator("mat-card, .mat-row, tr, .card").all()
        print(f"📋 Képernyőn talált nyers blokkok száma: {len(cards)}")

        for card in cards:
            try:
                text_content = card.inner_text()
                if not text_content or "ft" not in text_content.lower():
                    continue

                text_lower = text_content.lower()
                if any(bad in text_lower for bad in ["személygépkocsi", "tehergépkocsi", "üzletrész", "ingóság"]):
                    continue

                # 1. Ügyszám és egyedi ID bányászat
                ugyszam_match = re.search(r'(\d+\.V\.\d+/\d+)|(\d+\.V\.\d+)', text_content)
                if ugyszam_match:
                    ugyszam = ugyszam_match.group(0)
                    auction_id = ugyszam.replace(".", "_").replace("/", "_")
                else:
                    continue

                # 2. Ár kiszedése (tisztított regex az új formátumhoz)
                kikialtasi_ar = 0
                lines_lower = [l.strip() for l in text_lower.split("\n") if l.strip()]
                for line in lines_lower:
                    if "kikiáltási" in line or "minimál" in line or "ár" in line:
                        digits = "".join(filter(str.isdigit, line))
                        if digits:
                            kikialtasi_ar = int(digits)
                            break
                
                if kikialtasi_ar == 0:
                    # B-terv: a legnagyobb értelmes szám a blokkból
                    all_nums = [int("".join(filter(str.isdigit, w))) for w in text_content.replace(".", " ").split() if "".join(filter(str.isdigit, w)).isdigit()]
                    prices = [n for n in all_nums if 100000 <= n <= 700000000]
                    if prices:
                        kikialtasi_ar = max(prices)

                # 3. Helyszín precíz bányászata (kizárva a hibás sorokat)
                telepules = "MBVK Ingatlan"
                lines = [l.strip() for l in text_content.split("\n") if l.strip()]
                for line in lines:
                    if (len(line) > 4 and 
                        not re.search(r'\d{4}\.\d{2}\.\d{2}', line) and 
                        not re.search(r'\d+\.V\.\d+', line) and 
                        "ft" not in line.lower() and 
                        "kikiáltási" not in line.lower() and
                        "minimál" not in line.lower() and
                        "becsérték" not in line.lower() and
                        "licit" not in line.lower()):
                        telepules = line
                        break

                # 4. Gyári közvetlen link generálása az ügyszám alapján
                full_link = f"https://arveres.mbvk.hu/#/reszletek?ugyszam={ugyszam}"

                if not any(x["id"] == auction_id for x in arveresek):
                    arveresek.append({
                        "id": auction_id,
                        "ugyszam": ugyszam,
                        "telepules": telepules,
                        "ar": kikialtasi_ar,
                        "link": full_link
                    })
            except:
                continue

        browser.close()

    print(f"📊 Feldolgozott ingatlanok száma: {len(arveresek)}")
    new_found_count = 0

    for prop in arveresek:
        auction_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        if kikialtasi_ar <= 2000000 or TESZT_MOD:
            if auction_id not in old_records or TESZT_MOD:
                new_found_count += 1
                
                ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"
                teszt_prefix = "⚠️ *MINTA INGATLAN TESZTHEZ*\n"

                üzenet = (
                    f"{teszt_prefix}"
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Helyszín:* {prop['telepules']}\n"
                    f"🔹 *Ügyszám:* {prop['ugyszam']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* Ellenőrzés alatt\n\n"
                    f"🔗 [Ugrás a konkrét hirdetményre]({prop['link']})"
                )
                
                send_telegram_message(üzenet)
                
                if new_found_count >= 3:
                    break

    if len(arveresek) > 0:
        send_telegram_message("✅ *MBVK Monitor Teszt:* A tágított listás beolvasás sikeres! Nézd meg a kapott 3 üzenetet!")
    else:
        send_telegram_message("❌ *MBVK Monitor Teszt:* Még a tágított listával sem találtam kártyát. Ellenőrizni kell a szelektorokat.")

if __name__ == "__main__":
    main()
