import os
import json
import requests
import re
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"
CONFIG_FILE = "monitor_config.json"

def load_database():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return []
    return []

def save_database(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def ellenoriz_kulcsszo(beallitott_kulcsszo, vizsgalt_szoveg):
    """
    Intelligens szűrő, ami kezeli a sima szavakat és a komplex megyecsoportokat is.
    """
    kw = beallitott_kulcsszo.lower().strip()
    txt = vizsgalt_szoveg.lower()
    
    if kw == "mind":
        return True
        
    if kw == "pest":
        return "pest" in txt
        
    if kw == "balaton":
        # Felhasználó által kért megyék a Balatonhoz
        balaton_megyek = ["zala", "somogy", "veszprém", "veszprem"]
        return any(megye in txt for megye in balaton_megyek)
        
    if kw in ["videk", "vidék"]:
        # Felhasználó által kért vidéki megyék
        videk_megyek = [
            "nógrád", "nograd", 
            "komárom", "komarom", 
            "tolna", 
            "fejér", "fejer", 
            "jász", "jasz", 
            "bács", "bacs"
        ]
        return any(megye in txt for megye in videk_megyek)
        
    # Bármilyen egyéb egyedi kulcsszó esetén (pl. ha kézzel beírod, hogy 'győr')
    return kw in txt

def load_and_update_config():
    config = {"keyword": "mind", "max_ar": 2000000}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                config = json.load(f)
            except:
                pass

    # Lekérjük az utolsó frissítést a Telegramról
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("ok"):
            updates = res.get("result", [])
            highest_update_id = None
            
            for update in updates:
                message = update.get("message", {})
                text = message.get("text", "").strip()
                update_id = update.get("update_id")
                highest_update_id = update_id
                
                # Ha a kopasz /szures parancs érkezik, felajánljuk a pontos gombokat
                if text == "/szures":
                    keyboard = {
                        "keyboard": [
                            [{"text": "/szures pest 1000000"}, {"text": "/szures pest 2000000"}],
                            [{"text": "/szures balaton 2000000"}, {"text": "/szures videk 2000000"}]
                        ],
                        "resize_keyboard": True,
                        "one_time_keyboard": True
                    }
                    send_telegram_message("💡 *Válassz egy szűrést az alábbi gombok közül:*", reply_markup=keyboard)
                
                # Ha a parancs paraméterekkel jön (gombnyomásra vagy kézi beírásra)
                elif text.startswith("/szures"):
                    parts = text.split()
                    if len(parts) >= 3:
                        config["keyword"] = parts[1]
                        config["max_ar"] = int(parts[2])
                    elif len(parts) == 2:
                        if parts[1].isdigit():
                            config["max_ar"] = int(parts[1])
                        else:
                            config["keyword"] = parts[1]
            
            # Nyugtázzuk az üzeneteket, hogy ne olvassa be őket újra
            if highest_update_id is not None:
                requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={highest_update_id + 1}", timeout=10)
                
    except Exception as e:
        print(f"⚠️ Telegram konfigurációs hiba: {e}")

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    return config

def send_telegram_message(text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"❌ Telegram küldési hiba: {e}")

def main():
    print("🚀 Licitnapló Távirányítós Szövegbányász Monitor elindult...")
    old_records = load_database()
    
    print(f"DEBUG: Jelenlegi adatbázis mérete: {len(old_records)} tétel.")

    # Konfiguráció frissítése a Telegram gombok alapján
    config = load_and_update_config()
    print(f"🔍 Aktív szűrés -> Kulcsszó csoport: '{config['keyword']}', Max ár: {config['max_ar']:,} Ft")
    
    # Kiterjesztett alap lekérés 5M Ft-ig, a szűrést a Python végzi el
    target_url = "https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-5000000&status=aktiv"
    
    links_data = []
    body_text = ""
    
    with sync_playwright() as p:
        try:
            print("--> Virtuális Chrome indítása...")
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 3000}
            )
            page = context.new_page()
            page.goto(target_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(4000)
            
            print("--> Szakaszos mélygörgetés...")
            for i in range(15):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                page.wait_for_timeout(1500)
            
            links_data = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a'));
                return links.map(a => ({
                    href: a.href,
                    text: a.innerText || ""
                }));
            }""")
            
            body_text = page.locator("body").inner_text()
            browser.close()
        except Exception as e:
            print(f"❌ Playwright hiba: {e}")
            return

    arveresek = []
    feldolgozott_idk = set()

    # Feldolgozás Linkek alapján
    for item in links_data:
        href = item.get("href", "")
        text = item.get("text", "").strip()
        
        if "000 Ft" in text and "licitnaplo.hu/" in href and not any(x in href for x in ["status=", "ar=", "bekoltozheto="]):
            try:
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                if not lines:
                    continue
                
                kikialtasi_ar = 0
                for line in lines:
                    if "ft" in line.lower():
                        digits = "".join(filter(str.isdigit, line))
                        if digits and 50000 <= int(digits) <= 5000000:
                            kikialtasi_ar = int(digits)
                            break
                
                if kikialtasi_ar == 0:
                    continue
                
                # --- ÁRSZŰRÉS ---
                if kikialtasi_ar > config["max_ar"]:
                    continue
                    
                # --- MEGYECSOPORT / KULCSSZÓ SZŰRÉS ---
                if not ellenoriz_kulcsszo(config["keyword"], text):
                    continue

                telepules = lines[0]
                cim = lines[1] if len(lines) > 1 and "Ft" not in lines[1] else telepules
                
                clean_id = "".join(filter(str.isalnum, href.split("/")[-1]))
                if not clean_id:
                    clean_id = "".join(filter(str.isalnum, cim))[:20] + f"_{kikialtasi_ar}"
                    
                auction_id = f"ln_{clean_id}"
                
                if auction_id in feldolgozott_idk:
                    continue
                feldolgozott_idk.add(auction_id)
                
                arveresek.append({
                    "id": auction_id,
                    "telepules": telepules,
                    "cim": cim,
                    "ar": kikialtasi_ar,
                    "link": href
                })
            except:
                continue

    # B-TERV (Törzsszöveg alapján, ha a DOM linkek üresek lennének)
    if not arveresek:
        blocks = re.findall(r'([^<> \n]+?\d{4}\s+[^<> \n]+?[\s\S]*?\d+[\d\s]*Ft)', body_text)
        for block in blocks:
            try:
                # --- MEGYECSOPORT / KULCSSZÓ SZŰRÉS ---
                if not ellenoriz_kulcsszo(config["keyword"], block):
                    continue
                    
                price_match = re.search(r'(\d+[\d\s]*)\s*Ft', block)
                if not price_match:
                    continue
                price = int(price_match.group(1).replace(" ", "").replace("\xa0", "").strip())
                
                # --- ÁRSZŰRÉS ---
                if price > config["max_ar"]:
                    continue
                    
                clean_text = re.sub(r'\s+', ' ', block).strip()
                auction_id = "ln_txt_" + "".join(filter(str.isalnum, clean_text[:15])) + f"_{price}"
                
                if auction_id in feldolgozott_idk:
                    continue
                feldolgozott_idk.add(auction_id)
                
                arveresek.append({
                    "id": auction_id,
                    "telepules": clean_text[:30],
                    "cim": clean_text[:60] + "...",
                    "ar": price,
                    "link": target_url
                })
            except:
                continue

    print(f"📊 Megmaradt szűrt ingatlanok száma: {len(arveresek)}")
    new_found_count = 0

    for prop in arveresek:
        auction_id = prop["id"]
        if auction_id not in old_records:
            new_found_count += 1
            old_records.append(auction_id)

            # Google Maps hivatkozás generálása
            keresendo_cim = f"{prop['telepules']} {prop['cim']}".replace("...", "").strip()
            maps_url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(keresendo_cim)}"

            üzenet = (
                f"🚨 *ÚJ TALÁLAT A SZŰRŐD ALAPJÁN!*\n\n"
                f"📍 *Település / Régió:* {prop['telepules']}\n"
                f"🏠 *Cím / Infó:* {prop['cim']}\n"
                f"💰 *Kikiáltási ár:* {prop['ar']:,} HUF\n"
                f"🗺️ [Megtekintés Google Maps-en]({maps_url})\n"
                f"🔗 [Ugrás az ingatlan adatlapjára]({prop['link']})"
            )
            send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új tétel elmentve.")
    else:
        print("😴 Nincs új találat.")
        send_telegram_message(f"✅ *Futtatás sikeres.*\n🔍 *Aktív szűrőcsoport:* `{config['keyword']}` | `{config['max_ar']:,} Ft` alatt.\n❌ Új tárgy nem érkezett.")

if __name__ == "__main__":
    main()
