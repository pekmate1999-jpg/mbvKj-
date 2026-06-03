import os
import json
import requests
import re
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

# === ÉLES ÜZEMMÓD ===
# Kikapcsolva a teszt, mostantól CSAK a 2M Ft alatti új ingatlanoknál fog riasztani!
TESZT_MOD = False 

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
    print("🚀 MBVK Éles Monitor elindult...")
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

        # ÉLES, SZIGORÚAN SZŰRT URL (1/1, beköltözhető, tehermentes ingatlanok)
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        
        print("--> Várakozás a dinamikus tartalom generálódására...")
        page.wait_for_timeout(8000)
        
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        page.wait_for_timeout(4000)

        print("--> Szöveges tartalom kinyerése...")
        body_text = page.locator("body").inner_text()
        
        browser.close()

    if not body_text:
        print("📭 Az oldal üres forrást adott vissza.")
        return

    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    all_text_combined = " \n ".join(lines)
    
    # Ügyszámok kikeresése
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
                    
                    # Ár bányászat
                    for k_line in környezet:
                        if "ft" in k_line.lower() or "kikiáltási" in k_line.lower() or "minimál" in k_line.lower():
                            digits = "".join(filter(str.isdigit, k_line))
                            if digits and 100000 <= int(digits) <= 500000000:
                                kikialtasi_ar = int(digits)
                                break
                    
                    # Település bányászat
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

    new_found_count = 0

    for prop in arveresek:
        auction_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        # Szigorú szűrés: Csak a 2 millió Ft alattiak és csak az újak!
        if kikialtasi_ar <= 2000000 and kikialtasi_ar > 0:
            if auction_id not in old_records:
                new_found_count += 1
                old_records.append(auction_id)

                ar_kiiras = f"{kikialtasi_ar:,} HUF"

                # `ügyszám` -> Monospace formázás. Mobilról rákattintva AZONNAL vágólapra másolja!
                üzenet = (
                    f"🚨 *ÚJ OLCSÓ INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Helyszín:* {prop['telepules']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n"
                    f"🔹 *Másolható ügyszám (kattints rá):* `{prop['ugyszam']}`\n\n"
                    f"🔗 [Megnyitás az MBVK Keresőben](https://arveres.mbvk.hu/#/kereses)"
                )
                
                send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új ingatlan elmentve az adatbázisba.")
    else:
        print("😴 Nincs új, feltételeknek megfelelő olcsó ingatlan.")


if __name__ == "__main__":
    main()
