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
    print("🚀 MBVK Blokk-Alapú Szöveges Monitor elindult...")
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
            viewport={"width": 1280, "height": 2000}
        )
        page = context.new_page()

        # Éles, szűrt URL (1/1, tehermentes, beköltözhető)
        target_url = "https://arveres.mbvk.hu/#/kereses?kategoria=INGATLAN&allapot=AKTIV&tulajdon=1%2F1&tehermentes=true&bekoltozheto=true"
        print(f"--> URL megnyitása: {target_url}")
        
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(6000)
        
        # Alapos görgetés a háttérben
        for i in range(5):
            page.evaluate(f"window.scrollTo(0, {i * 400});")
            page.wait_for_timeout(1500)
        
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        page.wait_for_timeout(3000)

        # A jól bevált teljes szöveges kinyerés
        body_text = page.locator("body").inner_text()
        browser.close()

    if not body_text:
        print("📭 Az oldal szövege üres.")
        return

    # --- BLOKKOKRA BONTÁS ÜGYSZÁMOK ALAPJÁN ---
    # Megkeressük az összes ügyszámot az oldalon
    ugyszam_poziciok = [m.start() for m in re.finditer(r'\d+\.V\.\d+(?:/\d+)?', body_text)]
    
    if not ugyszam_poziciok:
        print("📭 Egyetlen ügyszámot sem találtam a szövegben.")
        send_telegram_message("ℹ️ *MBVK Monitor:* Nem található aktív árverési ügyszám az oldalon.")
        return

    print(f"🔹 Talált ügyszám pozíciók száma: {len(ugyszam_poziciok)}")

    # Feldaraboljuk a teljes body szövegét különálló ingatlan kártyákra
    for index, pozicio in enumerate(ugyszam_poziciok):
        try:
            # Egy blokk az aktuális ügyszámtól a következő ügyszámig tart (vagy a szöveg végéig)
            start = pozicio
            end = ugyszam_poziciok[index + 1] if index + 1 < len(ugyszam_poziciok) else len(body_text)
            
            # Kibővítjük a blokkot kicsit "felfelé" is, mert a helyszín/városnév az ügyszám FELETT lakik a hirdetésben!
            blokk_start = max(0, start - 300)
            blokk_szoveg = body_text[blokk_start:end]
            
            # 1. Ügyszám kinyerése a blokkból
            ugyszam_match = re.search(r'\d+\.V\.\d+(?:/\d+)?', body_text[start:end])
            if not ugyszam_match:
                continue
            ugyszam = ugyszam_match.group(0)
            auction_id = ugyszam.replace(".", "_").replace("/", "_")

            # Blokkon belüli sorok tisztítása
            blokk_sorok = [s.strip() for s in blokk_szoveg.split("\n") if s.strip()]

            # 2. Ár kinyerése a konkrét blokkból
            kikialtasi_ar = 0
            for sor in blokk_sorok:
                if "ft" in sor.lower():
                    szamok = "".join(filter(str.isdigit, sor))
                    if szamok and 100000 <= int(szamok) <= 800000000:
                        if int(szamok) > kikialtasi_ar:
                            kikialtasi_ar = int(szamok)

            # 3. Helyszín kinyerése (Az ügyszám feletti első tiszta sor)
            telepules = "MBVK Ingatlan"
            # Megkeressük, hol van az ügyszám a sorok között, és elindulunk VISSZAFELÉ
            for i, sor in enumerate(blokk_sorok):
                if ugyszam in sor:
                    # Megnézzük az ügyszám feletti 3 sort visszafelé
                    for j in range(i - 1, max(-1, i - 4), -1):
                        vizsgalt_sor = blokk_sorok[j]
                        vizsgalt_lower = vizsgalt_sor.lower()
                        
                        # Ha a sor nem technikai szöveg, nem ár és elég hosszú, az lesz a város/cím
                        if (len(vizsgalt_sor) > 4 and 
                            "ft" not in vizsgalt_lower and 
                            "ügyszám" not in vizsgalt_lower and 
                            "kikiáltási" not in vizsgalt_lower and
                            "minimál" not in vizsgalt_lower and
                            "becsérték" not in vizsgalt_lower and
                            "licit" not in vizsgalt_lower and
                            "tulajdon" not in vizsgalt_lower and
                            not re.search(r'\d{4}\.\d{2}\.\d{2}', vizsgalt_sor) and
                            not re.search(r'\d+\.V\.\d+', vizsgalt_sor)):
                            
                            telepules = vizsgalt_sor
                            break
                    break

            # 4. Közvetlen Kereső Link a fixen működő főoldalra
            full_link = "https://arveres.mbvk.hu/#/kereses"

            if not any(x["id"] == auction_id for x in arveresek):
                arveresek.append({
                    "id": auction_id,
                    "ugyszam": ugyszam,
                    "telepules": telepules,
                    "ar": kikialtasi_ar,
                    "link": full_link
                })
        except Exception as e:
            print(f"⚠️ Blokk hiba: {e}")
            continue

    print(f"📊 Feldolgozott egyedi ingatlanok száma: {len(arveresek)}")
    new_found_count = 0

    for prop in arveresek:
        auction_id = prop["id"]
        kikialtasi_ar = prop["ar"]
        ugyszam = prop["ugyszam"]

        # Éles limit szűrés (2 000 000 Ft)
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
                    f"🔹 *Másolható ügyszám (kattints rá):* `{ugyszam}`\n\n"
                    f"🔗 [Megnyitás az MBVK Keresőben]({prop['link']})"
                )
                send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új tétel elmentve.")
    else:
        # Ha mindent átfésült, de a limit alatt nem volt új, küldünk egy diszkrét jelet, hogy megnyugodj: a kód él!
        send_telegram_message("✅ *MBVK Monitor:* A keresés sikeresen lefutott. Jelenleg nincs új, feltételeknek megfelelő ingatlan 2M Ft alatt.")

if __name__ == "__main__":
    main()
