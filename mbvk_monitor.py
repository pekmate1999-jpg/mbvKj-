import os
import json
import requests
import re
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

# === TESZT ÜZEMMÓD ===
# True: Minden talált tételből küld 3 mintát, hogy ellenőrizni tudjuk az adatok tisztaságát.
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
    print("🚀 MBVK Szelektor-Független Globális Monitor elindult...")
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

        # Tágított keresési URL, hogy biztosan legyen tartalom a betöltött HTML-ben
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV"
        print(f"--> URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        
        print("--> Várakozás a dinamikus tartalom generálódására...")
        page.wait_for_timeout(8000)
        
        # Legörgetünk az aljára, hogy az Angular kénytelen legyen mindent legenerálni a memóriába
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        page.wait_for_timeout(4000)

        # Letöltjük a teljes oldal látható szöveges tartalmát egyetlen nagy tömbben
        print("--> Teljes oldal szöveges tartalmának kinyerése...")
        body_text = page.locator("body").inner_text()
        
        browser.close()

    if not body_text:
        print("📭 Az oldal üres forrást adott vissza.")
        send_telegram_message("❌ *MBVK Monitor Teszt:* A böngésző nem tudta beolvasni az oldal szöveges tartalmát.")
        return

    # --- REGEX ALAPÚ ADATBÁNYÁSZAT ---
    # Felbontjuk a teljes oldal szövegét sorokra
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    print(f"📋 Beolvasott nyers sorok száma: {len(lines)}")

    # Megkeressük az összes ügyszámot a szövegben
    all_text_combined = " \n ".join(lines)
    # Keresünk ügyszám mintákat (pl. 123.V.456/2025 vagy 123.V.456)
    ugyszamok = re.findall(r'\d+\.V\.\d+(?:/\d+)?', all_text_combined)
    # Duplikációk kiszűrése a listából
    ugyszamok = list(set(ugyszamok))
    
    print(f"🔹 Talált egyedi ügyszámok a nyers szövegben: {len(ugyszamok)}")

    # Ha találtunk ügyszámokat, megpróbáljuk a környezetükből kihúzni az adatokat
    for ugyszam in ugyszamok:
        try:
            auction_id = ugyszam.replace(".", "_").replace("/", "_")
            
            # Keressük meg, hol szerepel ez az ügyszám a sorok között
            telepules = "MBVK Ingatlan"
            kikialtasi_ar = 0
            
            for i, line in enumerate(lines):
                if ugyszam in line:
                    # Megpróbáljuk a környező sorokból (3 sorral feljebb/lejjebb) kiszedni a várost és az árat
                    környezet = lines[max(0, i-4):min(len(lines), i+6)]
                    
                    # Ár keresése a környezetben
                    for k_line in környezet:
                        if "ft" in k_line.lower() or "kikiáltási" in k_line.lower() or "minimál" in k_line.lower():
                            digits = "".join(filter(str.isdigit, k_line))
                            if digits and 100000 <= int(digits) <= 500000000:
                                kikialtasi_ar = int(digits)
                                break
                    
                    # Település keresése (az első olyan sor a környezetben, ami nem ügyszám, nem ár, nem dátum)
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

    print(f"📊 Sikeresen strukturált ingatlanok száma: {len(arveresek)}")
    new_found_count = 0

    for prop in arveresek:
        auction_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        if kikialtasi_ar <= 2000000 or TESZT_MOD:
            new_found_count += 1
            ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"
            
            üzenet = (
                f"⚠️ *GLOBÁLIS SZÖVEGES TESZT TALÁLAT*\n\n"
                f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                f"📍 *Helyszín/Megnevezés:* {prop['telepules']}\n"
                f"🔹 *Ügyszám:* {prop['ugyszam']}\n"
                f"💰 *Kikiáltási ár:* {ar_kiiras}\n\n"
                f"🔗 [Ugrás a konkrét hirdetményre]({prop['link']})"
            )
            
            send_telegram_message(üzenet)
            
            if new_found_count >= 3:
                break

    if len(arveresek) > 0:
        send_telegram_message("✅ *MBVK Monitor Teszt:* A szelektor-független beolvasás sikeresen lezárult!")
    else:
        # Ha még így sincs semmi, akkor az oldal egyáltalán nem töltődött be a GitHub Actions alatt (pl. Captcha vagy hálózati blokk miatt)
        send_telegram_message("❌ *MBVK Monitor Teszt:* A szöveges elemző sem talált ügyszámot az oldalon. Ellenőrizni kell, hogy az MBVK nem blokkolja-e a GitHub szerverét.")

if __name__ == "__main__":
    main()
