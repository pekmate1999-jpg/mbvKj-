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
        res = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
        print(f"📡 Telegram küldés státusz: {res.status_code}")
    except Exception as e:
        print(f"❌ Telegram küldési hiba: {e}")

def main():
    print("🚀 MBVK Debug Monitor elindult...")
    # 1. TESZT: Küldünk egy azonnali jelet a Telegramra, hogy él-e a kapcsolat
    send_telegram_message("🤖 *MBVK Monitor:* A futás elindult, megkezdem az oldal beolvasását...")

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
            viewport={"width": 1280, "height": 1600}
        )
        page = context.new_page()

        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> URL megnyitása: {target_url}")
        
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            print("--> Oldal alapvetően betöltődött. Várunk a JavaScriptre (10 mp)...")
            page.wait_for_timeout(10000)
            
            # Kényszerített görgetés, hogy az Angular felébredjen
            page.evaluate("window.scrollTo(0, 300);")
            page.wait_for_timeout(3000)
            
            print("--> Szöveges tartalom kinyerése...")
            body_text = page.locator("body").inner_text()
            
            print(f"📝 Kinyert szöveg hossza: {len(body_text) if body_text else 0} karakter.")
            
            if not body_text or len(body_text.strip()) < 100:
                print("⚠️ A kinyert szöveg túl rövid vagy üres! Képernyőkép mentése...")
                page.screenshot(path="screenshot.png")
                send_telegram_message("⚠️ *MBVK Monitor:* Az oldal szövege üresen jött vissza. Mentettem egy `screenshot.png`-t a munkafolyamatba.")
                
        except Exception as e:
            print(f"❌ Hiba a Playwright futása közben: {e}")
            page.screenshot(path="error_screenshot.png")
            body_text = ""
        
        browser.close()

    if not body_text:
        print("📭 Nincs feldolgozható szöveg, leállás.")
        return

    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    all_text_combined = " \n ".join(lines)
    
    ugyszamok = list(set(re.findall(r'\d+\.V\.\d+(?:/\d+)?', all_text_combined)))
    print(f"🔹 Talált egyedi ügyszámok száma: {len(ugyszamok)}")

    for ugyszam in ugyszamok:
        try:
            auction_id = ugyszam.replace(".", "_").replace("/", "_")
            telepules = "MBVK Ingatlan"
            kikialtasi_ar = 0
            
            for i, line in enumerate(lines):
                if ugyszam in line:
                    környezet = lines[max(0, i-4):min(len(lines), i+6)]
                    
                    for k_line in környezet:
                        if "ft" in k_line.lower() or "kikiáltási" in k_line.lower() or "minimál" in k_line.lower():
                            digits = "".join(filter(str.isdigit, k_line))
                            if digits and 100000 <= int(digits) <= 500000000:
                                kikialtasi_ar = int(digits)
                                break
                    
                    for k_line in környezet:
                        if (len(k_line) > 4 and 
                            not re.search(r'\d{4}\.\d{2}\.\d{2}', k_line) and 
                            not re.search(r'\d+\.V\.\d+', k_line) and 
                            "ft" not in k_line.lower() and 
                            "kikiáltási" not in k_line.lower() and 
                            "minimál" not in k_line.lower() and 
                            "licit" not in k_line.lower() and
                            "ügyszám" not in k_line.lower()):
                            telepules = k_line
                            break
                    break

            if not any(x["id"] == auction_id for x in arveresek):
                arveresek.append({
                    "id": auction_id,
                    "ugyszam": ugyszam,
                    "telepules": telepules,
                    "ar": kikialtasi_ar
                })
        except:
            continue

    print(f"📊 Strukturált ingatlanok száma: {len(arveresek)}")
    new_found_count = 0

    for prop in arveresek:
        auction_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        if kikialtasi_ar <= 2000000 and kikialtasi_ar > 0:
            if auction_id not in old_records:
                new_found_count += 1
                old_records.append(auction_id)

                ar_kiiras = f"{kikialtasi_ar:,} HUF"
                üzenet = (
                    f"🚨 *ÚJ OLCSÓ INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Helyszín:* {prop['telepules']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n"
                    f"🔹 *Másolható ügyszám:* `{prop['ugyszam']}`\n\n"
                    f"🔗 [Megnyitás az MBVK Keresőben](https://arveres.mbvk.hu/#/kereses)"
                )
                send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új ingatlan elmentve.")
    else:
        print("😴 Nincs új találat.")
        # Ezt most visszatesszük, hogy lássuk, eljut-e a kód a legvégéig hiba nélkül
        send_telegram_message("✅ *MBVK Monitor:* A keresés sikeresen lefutott, de jelenleg nincs a feltételeknek megfelelő új ingatlan 2 000 000 Ft alatt.")

if __name__ == "__main__":
    main()
