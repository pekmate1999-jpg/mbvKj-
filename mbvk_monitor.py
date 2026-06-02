import os
import json
import requests
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
    print("🚀 MBVK Könnyített Monitor elindult...")
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

        # Közvetlen szűrt URL megnyitása (Ingatlan, Aktív, 1/1, tehermentes, beköltözhető)
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="load", timeout=45000)
        
        # Biztonsági várakozás, amíg az Angular felépíti a kártyákat
        page.wait_for_timeout(6000)

        print("--> Hirdetési elemek szűrése...")
        # Kiválasztjuk az összes linket az oldalon
        links = page.locator("a").all()
        print(f"--> Talált linkek száma elemzésre: {len(links)}")

        for link in links:
            try:
                href = link.get_attribute("href")
                if href and ("reszletek" in href or "arveres" in href or any(c.isdigit() for c in href)):
                    # Külső és téves linkek kiszűrése azonnal
                    if any(bad in href for bad in ["google", "support", "cookie", "analytics", "privacy"]):
                        continue

                    # ID kinyerése a linkből
                    prop_id = "".join(filter(str.isdigit, href))
                    if not prop_id or len(prop_id) < 4:
                        continue

                    # Szöveg kiolvasása közvetlenül a kártya linkjéből
                    text_content = link.inner_text()
                    if not text_content:
                        continue

                    # Ár meghatározása: megkeressük az első olyan számcsoportot, ami reális értékű
                    kikialtasi_ar = 0
                    words = text_content.replace(".", "").split()
                    for word in words:
                        clean_word = "".join(filter(str.isdigit, word))
                        if clean_word and 5 <= len(clean_word) <= 9:
                            kikialtasi_ar = int(clean_word)
                            break

                    # Település meghatározása a kártya első sorából
                    lines = [l.strip() for l in text_content.split("\n") if l.strip()]
                    telepules = lines[0][:40] if lines else "MBVK Ingatlan"

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

    print(f"📊 Összesen feldolgozott tiszta hirdetés: {len(arveresek)}")
    new_found = False

    for prop in arveresek:
        prop_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        # --- ÁR SZŰRÉS: Max 2 000 000 HUF ---
        if kikialtasi_ar <= 2000000 or kikialtasi_ar == 0:
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
                print(f"✨ Telegram értesítés kiküldve: {prop['id']}")
                send_telegram_message(üzenet)

    if new_found:
        save_database(old_records)
        print("💾 Új találatok elmentve az adatbázisba.")
    else:
        print("😴 Nem találtam a feltételeknek megfelelő ÚJ hirdetést.")

if __name__ == "__main__":
    main()
