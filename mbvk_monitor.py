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
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"})

def main():
    print("🚀 MBVK Szigorított Ingatlan Monitor elindult...")
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

        # Megnyitjuk a keresőt
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="load", timeout=45000)
        page.wait_for_timeout(7000)

        print("--> Hirdetési elemek szűrése...")
        links = page.locator("a").all()

        for link in links:
            try:
                href = link.get_attribute("href")
                if href and ("reszletek" in href or "arveres" in href or any(c.isdigit() for c in href)):
                    if any(bad in href for bad in ["google", "support", "cookie", "analytics", "privacy"]):
                        continue

                    prop_id = "".join(filter(str.isdigit, href))
                    if not prop_id or len(prop_id) < 4:
                        continue

                    text_content = link.inner_text()
                    if not text_content:
                        continue
                    
                    text_lower = text_content.lower()

                    # --- SZIGORÚ INGATLAN SZŰRÉS ---
                    # Ha a szövegben benne van bármi, ami ingóságra utal, vagy NINCS benne az ingatlan/lakóház szó, eldobjuk
                    tiltott_szavak = ["ingóság", "személygépkocsi", "üzletrész", "ingóságok", "tehergépkocsi", "pótkocsi", "gép", "eszköz"]
                    if any(tiltott in text_lower for tiltott in tiltott_szavak):
                        continue
                    
                    # Ha az oldal figyelmen kívül hagyta az URL szűrőt, itt manuálisan megköveteljük az ingatlan kulcsszót
                    if "ingatlan" not in text_lower and "lakóház" not in text_lower and "lakás" not in text_lower and "telek" not in text_lower:
                        continue

                    # Ár meghatározása
                    kikialtasi_ar = 0
                    words = text_content.replace(".", "").split()
                    for word in words:
                        clean_word = "".join(filter(str.isdigit, word))
                        if clean_word and 5 <= len(clean_word) <= 9:
                            kikialtasi_ar = int(clean_word)
                            break

                    # Intelligens helyszín keresés: Megkeressük az első olyan sort, ami nem ügyszám és nem üres
                    telepules = "MBVK Ingatlan"
                    lines = [l.strip() for l in text_content.split("\n") if l.strip()]
                    for line in lines:
                        # Ha a sor nem tartalmaz ügyszám formátumot (pl. .V. vagy /202) és nem csak számokból áll
                        if not re.search(r'\d+\.V\.\d+', line) and not line.replace(" ", "").isdigit() and len(line) > 3:
                            if "kikiáltási" not in line.lower() and "árverés" not in line.lower():
                                telepules = line[:40]
                                break

                    full_link = href if href.startswith("http") else f"https://arveres.mbvk.hu/{href}"

                    if not any(x["id"] == prop_id for x in arveresek):
                        arveresek.append({
                            "id": prop_id,
                            "telepules": telepules,
                            "ar": kikialtasi_ar,
                            "link": full_link
                        })
            except:
                continue

        browser.close()

    print(f"📊 Valódi, szűrt ingatlanok száma: {len(arveresek)}")
    new_found = False

    for prop in arveresek:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        # Max 2 000 000 HUF limit
        if kikialtasi_ar <= 50000000 or kikialtasi_ar == 0:
            if prop_id not in old_records:
                new_found = True
                old_records.append(prop_id)

                ar_kiiras = f"{kikialtasi_ar:,} HUF" if kikialtasi_ar > 0 else "Lásd az adatlapon"

                üzenet = (
                    f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
                    f"📍 *Helyszín:* {prop['telepules']}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                    f"🔗 [Ugrás az MBVK árverési adatlapra]({prop['link']})"
                )
                print(f"✨ Értesítés küldése: {prop['id']}")
                send_telegram_message(üzenet)

    if new_found:
        save_database(old_records)
        print("💾 Új találatok elmentve.")
    else:
        print("😴 Nem találtam új, kritériumoknak megfelelő ingatlant.")

if __name__ == "__main__":
    main()
