import os
import json
import requests
import re
from urllib.parse import quote_plus
from playwright.sync_api import sync_playwright

# Környezeti változók és fájlok
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

def kinyer_extra_infok(szoveg, kikialtasi_ar):
    """
    Kibányássza a szövegből a méretet, minimálárat és aktuális licitet,
    valamint kiszámolja a négyzetméterárat.
    """
    szoveg_low = szoveg.lower()
    
    # 1. Alapterület / Telekméret keresése (pl. 54 m², 120 m2, 450 nm)
    meret_match = re.search(r'(\d+[\d\s]*)\s*(m²|m2|nm)', szoveg_low)
    size_num = None
    telekmeret = "Nincs információ"
    if meret_match:
        try:
            clean_size = "".join(filter(str.isdigit, meret_match.group(1)))
            if clean_size:
                size_num = int(clean_size)
                telekmeret = f"{size_num:,} m²"
        except:
            pass
        
    minimal_ar = "Nincs megadva"
    aktualis_licit = "Nincs aktív licit"
    
    # 2. Soronkénti elemzés a kulcsszavakhoz
    for line in szoveg.split("\n"):
        l_low = line.lower().strip()
        if any(x in l_low for x in ["minimál", "minimal", "min.ár", "minimum"]):
            digits = "".join(filter(str.isdigit, l_low))
            if digits:
                minimal_ar = f"{int(digits):,} HUF"
        if any(x in l_low for x in ["aktuális", "aktualis", "jelenlegi", "licit", "itt tart"]):
            if not any(x in l_low for x in ["kikiáltási", "kikialtasi", "minimál", "minimal"]):
                digits = "".join(filter(str.isdigit, l_low))
                if digits:
                    aktualis_licit = f"{int(digits):,} HUF"
                    
    # 3. Négyzetméterár kiszámítása
    nm_ar = "Nem számítható"
    if size_num and size_num > 0 and kikialtasi_ar > 0:
        nm_ar = f"{round(kikialtasi_ar / size_num):,} HUF/m²"
        
    return aktualis_licit, minimal_ar, telekmeret, nm_ar

def ellenoriz_kulcsszo(beallitott_kulcsszo, vizsgalt_szoveg):
    kw = beallitott_kulcsszo.lower().strip()
    txt = vizsgalt_szoveg.lower()
    
    if kw == "mind":
        return True
    if kw == "pest":
        return "pest" in txt
    if kw == "balaton":
        balaton_megyek = ["zala", "somogy", "veszprém", "veszprem"]
        return any(megye in txt for megye in balaton_megyek)
    if kw in ["videk", "vidék"]:
        videk_megyek = ["nógrád", "nograd", "komárom", "komarom", "tolna", "fejér", "fejer", "jász", "jasz", "bács", "bacs"]
        return any(megye in txt for megye in videk_megyek)
        
    return kw in txt

def load_and_update_config():
    config = {"keyword": "mind", "max_ar": 2000000}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                config = json.load(f)
            except:
                pass

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
                
                if text.startswith("/szures"):
                    parts = text.split()
                    if len(parts) >= 3:
                        config["keyword"] = parts[1]
                        config["max_ar"] = int(parts[2])
                    elif len(parts) == 2:
                        if parts[1].isdigit():
                            config["max_ar"] = int(parts[1])
                        else:
                            config["keyword"] = parts[1]
            
            if highest_update_id is not None:
                requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={highest_update_id + 1}", timeout=10)
                
    except Exception as e:
        print(f"⚠️ Telegram konfigurációs hiba: {e}")

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    return config

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"❌ Telegram küldési hiba: {e}")

def main():
    print("🚀 Licitnapló Szövegbányász Monitor elindult...")
    old_records = load_database()
    
    config = load_and_update_config()
    print(f"🔍 Aktív szűrés -> Csoport: '{config['keyword']}', Max ár: {config['max_ar']:,} Ft")
    
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
                
                if kikialtasi_ar == 0 or kikialtasi_ar > config["max_ar"]:
                    continue
                    
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
                
                akt_licit, min_ar, t_meret, nm_ar = kinyer_extra_infok(text, kikialtasi_ar)
                
                arveresek.append({
                    "id": auction_id,
                    "telepules": telepules,
                    "cim": cim,
                    "ar": kikialtasi_ar,
                    "link": href,
                    "aktualis_licit": akt_licit,
                    "minimal_ar": min_ar,
                    "telekmeret": t_meret,
                    "nm_ar": nm_ar
                })
            except:
                continue

    # B-TERV (Szöveges törzs alapján)
    if not arveresek:
        blocks = re.findall(r'([^<> \n]+?\d{4}\s+[^<> \n]+?[\s\S]*?\d+[\d\s]*Ft)', body_text)
        for block in blocks:
            try:
                if not ellenoriz_kulcsszo(config["keyword"], block):
                    continue
                    
                price_match = re.search(r'(\d+[\d\s]*)\s*Ft', block)
                if not price_match:
                    continue
                price = int(price_match.group(1).replace(" ", "").replace("\xa0", "").strip())
                
                if price > config["max_ar"]:
                    continue
                    
                clean_text = re.sub(r'\s+', ' ', block).strip()
                auction_id = "ln_txt_" + "".join(filter(str.isalnum, clean_text[:15])) + f"_{price}"
                
                if auction_id in feldolgozott_idk:
                    continue
                feldolgozott_idk.add(auction_id)
                
                akt_licit, min_ar, t_meret, nm_ar = kinyer_extra_infok(block, price)
                
                arveresek.append({
                    "id": auction_id,
                    "telepules": clean_text[:30],
                    "cim": clean_text[:60] + "...",
                    "ar": price,
                    "link": target_url,
                    "aktualis_licit": akt_licit,
                    "minimal_ar": min_ar,
                    "telekmeret": t_meret,
                    "nm_ar": nm_ar
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

            keresendo_cim = f"{prop['telepules']} {prop['cim']}".replace("...", "").strip()
            maps_url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(keresendo_cim)}"

            # Itt javítottam ki a behúzást (szigorúan a cikluson belülre kerültek a sorok):
            kategoria_nev = config['keyword'].upper()
            max_ar_formazott = f"{config['max_ar']:,} Ft"

            üzenet = (
                f"🚨 *ÚJ TALÁLAT A SZŰRŐD ALAPJÁN!* (`{kategoria_nev}` | `{max_ar_formazott}` alatt)\n\n"
                f"📍 *Település / Régió:* {prop['telepules']}\n"
                f"🏠 *Cím:* {prop['cim']}\n"
                f"💰 *Kikiáltási ár:* {prop['ar']:,} HUF\n"
                f"📈 *Aktuális licit:* {prop['aktualis_licit']}\n"
                f"📉 *Minimum ár:* {prop['minimal_ar']}\n"
                f"📐 *Telekméret / Alapterület:* {prop['telekmeret']}\n"
                f"🧮 *Négyzetméterár:* {prop['nm_ar']}\n\n"
                f"🗺️ [Megtekintés Google Maps-en]({maps_url})\n"
                f"🔗 [Ugrás az ingatlan adatlapjára]({prop['link']})"
            )
            send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új tétel elmentve.")
    else:
        print("😴 Nincs új találat.")
        send_telegram_message(f"✅ *Futtatás sikeres.*\n🔍 *Aktív szűrőcsoport:* `{config['keyword'].upper()}` | `{config['max_ar']:,} Ft` alatt.\n❌ Új tárgy nem érkezett.")

if __name__ == "__main__":
    main()
