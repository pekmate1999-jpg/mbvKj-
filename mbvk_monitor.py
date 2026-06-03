import os
import json
import requests
import re
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
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"❌ Telegram küldési hiba: {e}")

def main():
    print("🚀 MBVK DOM-Alapú Precíziós Monitor elindult...")
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
            viewport={"width": 1400, "height": 3000}
        )
        page = context.new_page()

        # Éles szűrt URL (1/1, tehermentes, beköltözhető)
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(6000)
        
        # Dinamikus tartalom görgetése
        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2);")
        page.wait_for_timeout(2000)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        page.wait_for_timeout(3000)

        # Az új Angular felületen a sorok '.mat-mdc-row' vagy 'tr' elemekben laknak
        rows = page.locator("tr, .mat-mdc-row, .mat-row, mat-row").all()
        print(f"📋 Talált nyers sorok száma a DOM-ban: {len(rows)}")

        for row in rows:
            try:
                # Kinyerjük a sor teljes szövegét és HTML-jét az egyedi adatokhoz
                row_text = row.inner_text()
                row_html = row.inner_html()
                
                if not row_text or "ft" not in row_text.lower():
                    continue

                # --- 1. KÖZVETLEN ADATLAP LINK BÁNYÁSZAT ---
                # Megkeressük a sorban lévő részletek gombot vagy linket, amiben benne van az egyedi azonosító
                id_match = re.search(r'id=(\d+)', row_html)
                if not id_match:
                    # B-terv: hátha a href paraméterben van benne a részletes link
                    id_match = re.search(r'href="[^"]*\/reszletek\/(\d+)"', row_html)
                
                auction_id = id_match.group(1) if id_match else None
                
                # --- 2. ÜGYSZÁM KINYERÉSE ---
                ugyszam_match = re.search(r'\d+\.V\.\d+(?:/\d+)?', row_text)
                ugyszam = ugyszam_match.group(0) if ugyszam_match else "Lásd a hirdetményben"

                # Ha nincs meg az ID, az ügyszámból csinálunk egyedit, hogy ne hagyjuk el a tételt
                if not auction_id:
                    if ugyszam != "Lásd a hirdetményben":
                        auction_id = ugyszam.replace(".", "_").replace("/", "_")
                    else:
                        continue

                # Gyári, közvetlen link összeállítása az egyedi ID alapján
                if id_match:
                    full_link = f"https://arveres.mbvk.hu/#/reszletek/{auction_id}"
                else:
                    full_link = f"https://arveres.mbvk.hu/#/kereses" # Végső B-terv

                # --- 3. ÁR PONTOS KINYERÉSE ---
                kikialtasi_ar = 0
                # Megkeressük az összes összeget a sorban, ami Ft előtt van
                prices = [int("".join(filter(str.isdigit, w))) for w in row_text.replace(".", " ").split() if "".join(filter(str.isdigit, w)).isdigit()]
                valid_prices = [p for p in prices if 100000 <= p <= 800000000]
                if valid_prices:
                    kikialtasi_ar = min(valid_prices) # A legkisebb értelmes összeg a kikiáltási ár/minimálár

                # --- 4. HELYSZÍN TŰPONTOS KINYERÉSE ---
                # A kártya sorait külön listázzuk, az első 1-2 sor tartalmazza a települést/utcát
                sub_lines = [sl.strip() for sl in row_text.split("\n") if sl.strip()]
                telepules = "MBVK Ingatlan"
                
                for sl in sub_lines:
                    # Az első olyan hosszabb sor, ami nem tartalmaz árat, dátumot vagy technikai szót
                    if (len(sl) > 5 and 
                        "ft" not in sl.lower() and 
                        "ügyszám" not in sl.lower() and 
                        "kikiáltási" not in sl.lower() and
                        not re.search(r'\d{4}\.\d{2}\.\d{2}', sl) and
                        not re.search(r'\d+\.V\.\d+', sl)):
                        telepules = sl
                        break

                if not any(x["id"] == auction_id for x in arveresek):
                    arveresek.append({
                        "id": auction_id,
                        "ugyszam": ugyszam,
                        "telepules": telepules,
                        "ar": kikialtasi_ar,
                        "link": full_link
                    })
            except Exception as e:
                print(f"⚠️ Sor feldolgozási hiba: {e}")
                continue

        browser.close()

    print(f"📊 Összesen talált és strukturált ingatlanok száma: {len(arveresek)}")
    new_found_count = 0

    for prop in arveresek:
        auction_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        # Beállított éles limit (2 000 000 Ft)
        if 0 < kikialtasi_ar <= 2000000:
            if auction_id not in old_records:
                new_found_count += 1
                old_records.append(auction_id)

                ar_kiiras = f"{kikialtasi_ar:,} HUF"
                üzenet = (
                    f"🚨 *ÚJ OLCSÓ INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Helyszín:* {prop['telepules']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n"
                    f"🔹 *Ügyszám:* `{prop['ugyszam']}`\n\n"
                    f"🔗 [Ugrás a konkrét hirdetmény adatlapjára]({prop['link']})"
                )
                send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új ingatlan elmentve.")
    else:
        print("😴 Nincs új, feltételnek megfelelő ingatlan.")

if __name__ == "__main__":
    main()
