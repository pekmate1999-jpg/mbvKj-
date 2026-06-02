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
    print("🚀 MBVK Univerzális Monitor elindult...")
    # AZONNAL KÜLDÜNK EGY JELZÉST, HOGY LÁSD: ELINDULT A KÓD
    send_telegram_message("🔄 *MBVK Monitor:* A keresés elindult a háttérben, böngésző indítása...")
    
    old_records = load_database()
    arveresek = []

    with sync_playwright() as p:
        print("--> Böngésző indítása...")
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        page = context.new_page()

        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        
        # Adunk az oldalnak bőségesen 12 másodpercet, hogy az Angular minden kártyát lerendeljen
        print("--> Várakozás a dinamikus tartalomra (12 mp)...")
        page.wait_for_timeout(12000)

        # Trükk: Az összes olyan szöveges blokkot megkeressük, amiben van kikiáltási ár vagy ügyszám
        print("--> Hirdetési blokkok beolvasása szöveges alapon...")
        
        # Kivesszük az oldal teljes HTML tartalmát és megnézzük a benne lévő hivatkozásokat másként
        html_content = page.content()
        
        # Az összes belső azonosítót kigyűjtjük, ami az MBVK-n hirdetésre mutathat
        # Az új felületen a link formátuma gyakran rejtett, így a szövegből bányásszuk ki az ügyszámokat
        all_text = page.evaluate("() => document.body.innerText")
        
        # Keressük a mat-card elemeket vagy táblázat sorokat közvetlenül a felületről
        # Kipróbálunk több lehetséges modern szelektort (kártyák, listaelemek, gombok)
        cards = page.locator("mat-card, .mat-row, tr, .card, [role='row']").all()
        print(f"--> Talált potenciális hirdetési blokkok száma: {len(cards)}")

        # Ha a specifikus szelektorok csődöt mondanak, megnézzük a sima div-eket amikben számok vannak
        if len(cards) <= 5:
            cards = page.locator("div").all()

        counter = 0
        for card in cards:
            try:
                text_content = card.inner_text()
                if not text_content or len(text_content.strip()) < 30:
                    continue
                
                text_lower = text_content.lower()

                # Csak olyan blokkok érdekelnek, amikben van kikiáltási ár vagy licit szó
                if "kikiáltási" not in text_lower and "árverés" not in text_lower and "ft" not in text_lower:
                    continue

                # --- KIZÁRÓ SZŰRÉS ---
                tiltott_szavak = ["ingóság", "személygépkocsi", "üzletrész", "ingóságok", "tehergépkocsi", "pótkocsi", "gép ", "eszköz", "részvény", "követelés"]
                if any(tiltott in text_lower for tiltott in tiltott_szavak):
                    continue

                # Ügyszám keresése, ami egyben az egyedi ID-nk lesz
                ugyszam_match = re.search(r'(\d+\.V\.\d+/\d+)|(\d+\.V\.\d+)', text_content)
                if ugyszam_match:
                    prop_id = ugyszam_match.group(0).replace(".", "_").replace("/", "_")
                else:
                    # Ha nincs ügyszám, generálunk egyet a szöveg számaiból, hogy ne hagyjuk el
                    digits = "".join(filter(str.isdigit, text_content))
                    if len(digits) > 5:
                        prop_id = digits[:10]
                    else:
                        continue

                # Ár meghatározása
                kikialtasi_ar = 0
                words = text_content.replace(".", "").replace(",", "").split()
                for word in words:
                    clean_word = "".join(filter(str.isdigit, word))
                    if clean_word and 5 <= len(clean_word) <= 9:
                        kikialtasi_ar = int(clean_word)
                        break

                # Település meghatározása
                telepules = "MBVK Ingatlan"
                lines = [l.strip() for l in text_content.split("\n") if l.strip()]
                for line in lines:
                    if not re.search(r'\d+\.V\.\d+', line) and not line.replace(" ", "").isdigit() and len(line) > 3:
                        if "kikiáltási" not in line.lower() and "árverés" not in line.lower():
                            telepules = line[:40]
                            break

                # Mivel a linket nehéz kiszedni, magára a főoldalra mutatunk, az ügyszám alapján úgyis kikereshető
                full_link = "https://arveres.mbvk.hu/#/kereses"

                if not any(x["id"] == prop_id for x in arveresek):
                    arveresek.append({
                        "id": prop_id,
                        "telepules": telepules,
                        "ar": kikialtasi_ar,
                        "link": full_link,
                        "ugyszam": ugyszam_match.group(0) if ugyszam_match else "Ismeretlen"
                    })
                    counter += 1
                    if counter > 30: # Limitáljuk a belső hurkot a fagyás ellen
                        break
            except:
                continue

        browser.close()

    print(f"📊 Talált egyedi ingatlan hirdetések száma: {len(arveresek)}")
    new_found = False

    for prop in arveresek:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        # --- FIGYELEM: TESZT LIMIT 50 MILLIÓRA ÁLLÍTVA, HOGY BIZTOSAN JÖJJÖN TALÁLAT ---
        if kikialtasi_ar <= 50000000 or kikialtasi_ar == 0:
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)

                ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"

                üzenet = (
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Helyszín:* {prop['telepules']}\n"
                    f"🔹 *Ügyszám:* {prop['ugyszam']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                    f"🔗 [Ugrás az MBVK Keresőre]({prop['link']})"
                )
                send_telegram_message(üzenet)

    if new_found:
        save_database(old_records)
        print("💾 Új találatok elmentve.")
        send_telegram_message(f"✅ *MBVK Monitor:* Sikeres lefutás! Összesen {len(arveresek)} db ingatlant vizsgáltam meg, és találtam újakat.")
    else:
        print("😴 Nincs új találat.")
        send_telegram_message(f"✅ *MBVK Monitor:* Sikeres lefutás! Összesen {len(arveresek)} db ingatlant vizsgáltam meg. Nincs új találat a megadott feltételekkel.")

if __name__ == "__main__":
    main()
