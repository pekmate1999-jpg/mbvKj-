import os
import json
import requests
import re
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

# === TESZT ÜZEMMÓD ===
# True: Kiküld 3 mintát az adatok és linkek ellenőrzéséhez. 
# Ha a kapott üzenet tökéletes, állítsd át False-ra az éles működéshez!
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
    print("🚀 MBVK Vizuális Precíziós Monitor elindult...")
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

        # Közvetlen, előre szűrt ingatlanos URL
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        
        print("--> Várakozás a hirdetési kártyák beágyazódására...")
        try:
            # Megvárjuk, amíg a modern Angular kártyaelemek fizikailag kirajzolódnak
            page.wait_for_selector("mat-card, .mat-row, tr, .card", timeout=20000)
            page.evaluate("window.scrollBy(0, 400);")
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"⚠️ Nem jelentek meg a kártyák időben: {e}")

        # Kiválasztjuk az összes hirdetési blokkot
        cards = page.locator("mat-card, .mat-row, tr, .card").all()
        print(f"📋 Képernyőn talált nyers blokkok száma: {len(cards)}")

        for card in cards:
            try:
                text_content = card.inner_text()
                if not text_content or "ft" not in text_content.lower():
                    continue

                # Kiszűrjük az ingóságokat a biztonság kedvéért
                text_lower = text_content.lower()
                if any(bad in text_lower for bad in ["személygépkocsi", "tehergépkocsi", "üzletrész", "ingóság"]):
                    continue

                # --- 1. ÜGYSZÁM ÉS ID BÁNYÁSZAT ---
                # Megkeressük a végrehajtói ügyszámot (pl. 123.V.456/2026)
                ugyszam_match = re.search(r'(\d+\.V\.\d+/\d+)|(\d+\.V\.\d+)', text_content)
                if ugyszam_match:
                    ugyszam = ugyszam_match.group(0)
                    # Az ID-t az ügyszámból képezzük, így tiszta és stabil marad
                    auction_id = ugyszam.replace(".", "_").replace("/", "_")
                else:
                    continue

                # --- 2. ÁR MEGHATÁROZÁSA ---
                kikialtasi_ar = 0
                # Megkeressük a kikiáltási ár után álló számot
                ar_match = re.findall(r'(?:kikiáltási\s+ár|minimálár|ár):\s*([\d\s\.]+)\s*Ft', text_lower)
                if ar_match:
                    kikialtasi_ar = int("".join(filter(str.isdigit, ar_match[0])))
                else:
                    # B-terv árkeresésre: a legnagyobb szám a blokkban
                    digits_list = [int("".join(filter(str.isdigit, w))) for w in text_content.replace(".", " ").split() if "".join(filter(str.isdigit, w)).isdigit()]
                    prices = [d for d in digits_list if 100000 <= d <= 500000000]
                    if prices:
                        kikialtasi_ar = max(prices)

                # --- 3. HELYSZÍN PONTOS MEGHATÁROZÁSA ---
                telepules = "MBVK Ingatlan"
                lines = [l.strip() for l in text_content.split("\n") if l.strip()]
                for line in lines:
                    # A helyszín az a sor, ami nem tartalmaz dátumot, nem ügyszám, nem tartalmaz árat és elég hosszú
                    if (len(line) > 4 and 
                        not re.search(r'\d{4}\.\d{2}\.\d{2}', line) and 
                        not re.search(r'\d+\.V\.\d+', line) and 
                        "ft" not in line.lower() and 
                        "kikiáltási" not in line.lower() and
                        "minimál" not in line.lower() and
                        "licit" not in line.lower()):
                        telepules = line
                        break

                # --- 4. GYÁRI KÖZVETLEN LINK ---
                # Mivel az ügyszám megvan, az MBVK gyári keresője az ügyszám paraméterrel közvetlenül az adatlapra ugrik!
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

        # Élesben 2M HUF limit, teszt módban mindent átengedünk
        if kikialtasi_ar <= 2000000 or TESZT_MOD:
            if auction_id not in old_records or TESZT_MOD:
                new_found_count += 1
                
                if not TESZT_MOD:
                    old_records.append(auction_id)

                ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"
                teszt_prefix = "⚠️ *TESZT ÜZEMMÓD TALÁLAT*\n" if TESZT_MOD else ""

                üzenet = (
                    f"{teszt_prefix}"
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Helyszín:* {prop['telepules']}\n"
                    f"🔹 *Ügyszám:* {prop['ugyszam']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                    f"🔗 [Ugrás a konkrét hirdetményre]({prop['link']})"
                )
                
                send_telegram_message(üzenet)
                
                if TESZT_MOD and new_found_count >= 3:
                    break

    if TESZT_MOD:
        if len(arveresek) > 0:
            send_telegram_message("✅ *MBVK Monitor Teszt:* Sikeres vizuális beolvasás! Ha a fenti 3 üzenet adatai és linkjei tökéletesek, állítsd át a kódban: `TESZT_MOD = False`!")
        else:
            send_telegram_message("⚠️ *MBVK Monitor Teszt:* A kód lefutott, de az MBVK oldala jelenleg teljesen üres erre a szűrésre. Próbáld meg kicsit később!")
    else:
        if new_found_count > 0:
            save_database(old_records)
        else:
            send_telegram_message("✅ *MBVK Monitor:* A keresés sikeresen lefutott. Jelenleg nincs új tiszta ingatlan 2 000 000 Ft alatt.")

if __name__ == "__main__":
    main()
