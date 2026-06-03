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
                data = json.load(f)
                if isinstance(data, dict):
                    return list(data.keys())
                return data if isinstance(data, list) else []
            except Exception:
                return []
    return []

def save_database(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def kinyer_extra_infok(szoveg, kikialtasi_ar):
    """
    Kibányássza a részletes szövegből a méretet, minimálárat és aktuális licitet.
    Mivel már a teljes leírásban keres, sokkal pontosabb!
    """
    szoveg_clean = re.sub(r'\s+', ' ', szoveg.replace('\xa0', ' ')).strip()
    szoveg_low = szoveg_clean.lower()
    
    # 1. Alapterület / Telekméret keresése (pl. 54 m², 120 m2, 450 nm)
    meret_match = re.search(r'(\d+[\d\s\.]*)\s*(m²|m2|nm|négyzetméter|negyzetmeter)', szoveg_low)
    size_num = None
    telekmeret = None
    if meret_match:
        try:
            clean_size = "".join(filter(str.isdigit, meret_match.group(1)))
            if clean_size:
                size_num = int(clean_size)
                telekmeret = f"{size_num:,} m²"
        except Exception:
            pass
        
    minimal_ar = None
    aktualis_licit = None
    
    # 2. Soronkénti elemzés a kulcsszavakhoz
    for line in szoveg.split("\n"):
        l_low = line.lower().replace('\xa0', ' ').strip()
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
    nm_ar = None
    if size_num and size_num > 0 and kikialtasi_ar > 0:
        nm_ar = f"{round(kikialtasi_ar / size_num):,} HUF/m²"
        
    return aktualis_licit, minimal_ar, telekmeret, nm_ar

def kinyer_leiras_szoveg(adatlap_szoveg):
    """
    Megpróbálja kivágni az adatlap szövegéből kifejezetten az ingatlan leírását.
    """
    if not adatlap_szoveg:
        return None
    lines = [l.strip() for l in adatlap_szoveg.split("\n") if l.strip()]
    
    leiras_start = -1
    for i, line in enumerate(lines):
        if any(k in line.lower() for k in ["leírás", "leiras", "hirdetmény leírása", "ingatlan leírása", "megjegyzés"]):
            if len(line) < 35:
                leiras_start = i
                break
                
    if leiras_start != -1 and leiras_start + 1 < len(lines):
        blokk_lines = []
        for line in lines[leiras_start + 1 : leiras_start + 15]:
            # Ha elérünk egy másik szekciófejlécet, megállunk
            if any(k in line.lower() for k in ["térkép", "licitálás", "árverési adatok", "adatok", "kapcsolat"]):
                if len(line) < 25:
                    break
            blokk_lines.append(line)
        if blokk_lines:
            return "\n".join(blokk_lines)[:700] # Telegram limit miatt max 700 karakter
            
    # Fallback: ha nincs fix szekció, az első értelmesebb hosszabb blokkot adjuk vissza
    return None

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
    config = {"keyword": "mind", "max_ar": 1000000}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                config = json.load(f)
                config["max_ar"] = 1000000  # Kényszerített 1 millió Ft
            except Exception:
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
    
    target_url = f"https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-{config['max_ar']}&status=aktiv"
    
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
            
            print(f"--> Oldal betöltése: {target_url}")
            page.goto(target_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(4000)
            
            print("--> Intelligens mélygörgetés az összes találat (100+) kibontásához...")
            last_height = page.evaluate("document.body.scrollHeight")
            for i in range(35):  # Megemelve 35 görgetésre a biztonság kedvéért
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                page.wait_for_timeout(1500)  # Több időt hagyunk a kártyák berajzolásának
                
                new_height = page.evaluate("document.body.scrollHeight")
                if new_height == last_height and i > 12:
                    # Ha elakadt a lazy-load, picit felgördülünk, majd megint le
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight - 800);")
                    page.wait_for_timeout(400)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    page.wait_for_timeout(800)
                last_height = new_height
            
            links_data = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a'));
                return links.map(a => ({
                    href: a.href,
                    text: a.innerText || ""
                }));
            }""")
            
            body_text = page.locator("body").inner_text()
            
            arveresek = []
            feldolgozott_idk = set()

            # JAVÍTOTT FELDOLGOZÁS: Kivettem a szigorú "000 Ft" korlátozást, csak a "ft"-ot nézzük!
            for item in links_data:
                href = item.get("href", "")
                text = item.get("text", "").strip()
                
                if "ft" in text.lower() and "licitnaplo.hu/" in href and not any(x in href for x in ["status=", "ar=", "bekoltozheto=", "oldal=", "rendezes="]):
                    try:
                        lines = [l.strip() for l in text.split("\n") if l.strip()]
                        if not lines:
                            continue
                        
                        kikialtasi_ar = 0
                        for line in lines:
                            if "ft" in line.lower():
                                digits = "".join(filter(str.isdigit, line))
                                if digits and 10000 <= int(digits) <= 20000000:
                                    kikialtasi_ar = int(digits)
                                    break
                        
                        if kikialtasi_ar == 0 or kikialtasi_ar > config["max_ar"]:
                            continue
                            
                        if not ellenoriz_kulcsszo(config["keyword"], text):
                            continue

                        telepules = lines[0]
                        cim = lines[1] if len(lines) > 1 and "Ft" not in lines[1] else telepules
                        
                        clean_id = "".join(filter(str.isalnum, href.strip("/").split("/")[-1]))
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
                            "link": href,
                            "leiras": None  # Ezt majd csak az új találatoknál töltjük be!
                        })
                    except Exception:
                        continue

            print(f"📊 Összesen talált és szűrt ingatlanok száma: {len(arveresek)}")
            new_found_count = 0

            # Új találatok részletes feldolgozása a háttérben nyitott lapokon
            for prop in arveresek:
                auction_id = prop["id"]
                if auction_id not in old_records:
                    new_found_count += 1
                    print(f"🔗 Új tétel észlelve! Adatlap letöltése: {prop['link']}")
                    
                    # Megnyitjuk az egyedi adatlapot a háttérben a részletes leírásért
                    try:
                        detail_page = context.new_page()
                        detail_page.goto(prop['link'], wait_until="networkidle", timeout=30000)
                        detail_page.wait_for_timeout(1500)
                        detail_text = detail_page.locator("body").inner_text()
                        
                        # Kinyerjük a tiszta leírás szövegét
                        prop["leiras"] = kinyer_leiras_szoveg(detail_text)
                        
                        # Újra lefuttatjuk az adatbányászatot a TELJES szövegen (telekméret, minimálár reményében)
                        akt_licit, min_ar, t_meret, nm_ar = kinyer_extra_infok(detail_text, prop["ar"])
                        detail_page.close()
                    except Exception as e:
                        print(f"⚠️ Nem sikerült az adatlap részletes beolvasása: {e}")
                        akt_licit, min_ar, t_meret, nm_ar = None, None, None, None
                        if 'detail_page' in locals(): detail_page.close()

                    old_records.append(auction_id)

                    keresendo_cim = f"{prop['telepules']} {prop['cim']}".replace("...", "").strip()
                    maps_url = f"https://www.google.com/maps/search/?api=1&query={quote_plus(keresendo_cim)}"

                    msg_lines = [
                        f"🚨 *ÚJ TALÁLAT*\n",
                        f"📍 *Település:* {prop['telepules']}",
                        f"🏠 *Cím:* {prop['cim']}",
                        f"💰 *Kikiáltási ár:* {prop['ar']:,} HUF"
                    ]
                    
                    if akt_licit: msg_lines.append(f"📈 *Aktuális licit:* {akt_licit}")
                    if min_ar: msg_lines.append(f"📉 *Minimum ár:* {min_ar}")
                    if t_meret: msg_lines.append(f"📐 *Alapterület / Méret:* {t_meret}")
                    if nm_ar: msg_lines.append(f"🧮 *Négyzetméterár:* {nm_ar}")
                    
                    # Ha sikerült kinyerni az árverési leírást, formázottan hozzácsapjuk az üzenethez
                    if prop["leiras"]:
                        clean_desc = prop["leiras"].replace("*", "").replace("_", "").replace("`", "")
                        msg_lines.append(f"\n📝 *Árverési leírás:*\n_{clean_desc}_")
                        
                    msg_lines.append(f"\n🗺️ [Megtekintés Google Maps-en]({maps_url})")
                    msg_lines.append(f"🔗 [Ugrás az ingatlan adatlapjára]({prop['link']})")

                    üzenet = "\n".join(msg_lines)
                    send_telegram_message(üzenet)

            if new_found_count > 0:
                save_database(old_records)
                print(f"💾 {new_found_count} új tétel elmentve az adatbázisba.")
            else:
                print("😴 Nincs új találat.")
                send_telegram_message(f"✅ *Futtatás sikeres.*\n🔍 *Aktív szűrőcsoport:* `{config['keyword'].upper()}` | `{config['max_ar']:,} Ft` alatt.\n❌ Új tárgy nem érkezett.")

            browser.close()
            
        except Exception as e:
            print(f"❌ Playwright fő hiba: {e}")
            if 'browser' in locals(): browser.close()
            return

if __name__ == "__main__":
    main()
