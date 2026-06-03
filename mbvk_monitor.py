import os
import json
import requests
import re
from playwright.sync_api import sync_playwright

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"
CONFIG_FILE = "monitor_config.json"

def load_database():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: return []
    return []

def save_database(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def load_and_update_config():
    """Beolvassa a mentett szűrési feltételeket, majd ellenőrzi, hogy küldtél-e új parancsot Telegramon"""
    # Alapértelmezett beállítások, ha még nem létezik a fájl
    config = {"keyword": "mind", "max_ar": 2000000}
    
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try: config = json.load(f)
            except: pass

    # Lekérjük a legfrissebb üzeneteket a Telegramtól
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("ok"):
            for update in res.get("result", []):
                message = update.get("message", {})
                text = message.get("text", "").strip()
                
                # Figyeljük a /szures parancsot (Formátum: /szures [kulcsszó] [max_ár])
                if text.startswith("/szures"):
                    parts = text.split()
                    if len(parts) >= 3:
                        config["keyword"] = parts[1]
                        config["max_ar"] = int(parts[2])
                        print(f"🔄 Új konfiguráció észlelve: Kulcsszó={config['keyword']}, MaxÁr={config['max_ar']}")
                    elif len(parts) == 2:
                        if parts[1].isdigit():
                            config["max_ar"] = int(parts[1])
                        else:
                            config["keyword"] = parts[1]
    except Exception as e:
        print(f"⚠️ Nem sikerült frissíteni a konfigurációt Telegramról: {e}")

    # Elmentjük az aktuális állapotot
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
        
    return config

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"❌ Telegram küldési hiba: {e}")

def main():
    print("🚀 Licitnapló Távirányítós Monitor elindult...")
    old_records = load_database()
    
    # 1. LÉPÉS: Lekérjük, hogy éppen mire kell szűrnünk
    config = load_and_update_config()
    print(f"🔍 Aktív szűrés -> Kulcsszó: '{config['keyword']}', Maximális ár: {config['max_ar']:,} Ft")
    
    # Mindig a teljes listát kérjük le a Licitnaplóról, a szűrést a Python végzi el dinamikusan
    target_url = "https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-2500000&status=aktiv"
    
    html_content = ""
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
            page.wait_for_timeout(3000)
            
            # Dinamikus görgetés (Lazy loading feloldása)
            for i in range(15):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                page.wait_for_timeout(1200)
            
            html_content = page.content()
            browser.close()
        except Exception as e:
            print(f"❌ Playwright hiba: {e}")
            return

    if not html_content:
        return

    # Megkeressük az összes linket és a rajtuk lévő szöveget közvetlenül a renderelt HTML-ből
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, "html.parser")
    cards = soup.find_all("div", class_=lambda x: x and 'card' in x.lower())
    
    arveresek = []
    feldolgozott_idk = set()

    for card in cards:
        try:
            card_text = card.get_text(separator="\n").strip()
            lines = [l.strip() for l in card_text.split("\n") if l.strip()]
            
            if not lines or "000 Ft" not in card_text:
                continue

            # Ár kiszedése
            kikialtasi_ar = 0
            for line in lines:
                if "ft" in line.lower():
                    digits = "".join(filter(str.isdigit, line))
                    if digits and 50000 <= int(digits) <= 3000000:
                        kikialtasi_ar = int(digits)
                        break
            
            if kikialtasi_ar == 0:
                continue

            # 2. LÉPÉS: DINAMIKUS PYTHON SZŰRÉS
            # Árszűrés ellenőrzése
            if kikialtasi_ar > config["max_ar"]:
                continue
                
            # Kulcsszó/Megye szűrés ellenőrzése (ha nem 'mind' van beállítva)
            if config["keyword"].lower() != "mind":
                if config["keyword"].lower() not in card_text.lower():
                    continue

            telepules = lines[0]
            cim = lines[1] if len(lines) > 1 and "Ft" not in lines[1] else telepules
            
            link_el = card.find("a")
            href = link_el.get("href") if link_el else ""
            
            if href and len(href) > 2 and not href.startswith("#"):
                full_link = href if href.startswith("http") else f"https://licitnaplo.hu{href}"
                id_match = re.search(r'(\d+)(?:[^\d]*)$', href)
                auction_id = f"ln_{id_match.group(1)}" if id_match else f"ln_{hash(cim[:15])}_{kikialtasi_ar}"
            else:
                slug = "".join(filter(str.isalnum, cim))[:20]
                auction_id = f"ln_fb_{slug}_{kikialtasi_ar}"
                full_link = target_url

            if auction_id in feldolgozott_idk:
                continue
            feldolgozott_idk.add(auction_id)

            arveresek.append({
                "id": auction_id,
                "telepules": telepules,
                "cim": cim,
                "ar": kikialtasi_ar,
                "link": full_link
            })
        except:
            continue

    print(f"📊 Szűrés után megmaradt ingatlanok száma: {len(arveresek)}")
    new_found_count = 0

    for prop in arveresek:
        auction_id = prop["id"]
        if auction_id not in old_records:
            new_found_count += 1
            old_records.append(auction_id)

            üzenet = (
                f"🚨 *ÚJ TALÁLAT A SZŰRŐD ALAPJÁN!* 🎯\n\n"
                f"📍 *Település:* {prop['telepules']}\n"
                f"🏠 *Cím / Infó:* {prop['cim']}\n"
                f"💰 *Kikiáltási ár:* {prop['ar']:,} HUF\n"
                f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                f"🔗 [Ugrás az ingatlan adatlapjára]({prop['link']})"
            )
            send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új tétel elmentve.")
    else:
        print("😴 Nincs új találat.")
        # Jelentés küldése az aktuális beállításról, hogy tudd, él a rendszer
        send_telegram_message(f"✅ *Futtatás sikeres.*\n🔍 *Aktív szűrő:* `{config['keyword']}` | `{config['max_ar']:,} Ft` alatt.\n❌ Új tárgy nem érkezett.")

if __name__ == "__main__":
    main()
