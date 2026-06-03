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
    szoveg_clean = re.sub(r'\s+', ' ', szoveg.replace('\xa0', ' ')).strip()
    szoveg_low = szoveg_clean.lower()
    
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
                    
    nm_ar = None
    if size_num and size_num > 0 and kikialtasi_ar > 0:
        nm_ar = f"{round(kikialtasi_ar / size_num):,} HUF/m²"
        
    return aktualis_licit, minimal_ar, telekmeret, nm_ar

def kinyer_leiras_szoveg(adatlap_szoveg):
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
            if any(k in line.lower() for k in ["térkép", "licitálás", "árverési adatok", "adatok", "kapcsolat"]):
                if len(line) < 25:
                    break
            blokk_lines.append(line)
        if blokk_lines:
            return "\n".join(blokk_lines)[:700] 
            
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
    # Alapértelmezett beállítások (ha még nincs konfigurációs fájl)
    config = {"keyword": "mind", "max_ar": 1000000}
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                config = json.load(f)
            except Exception:
                pass

    # Telegram bot frissítések lekérése, ha változtattál a szűrőkön chaten keresztül
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
                # Nyugtázzuk a frissítéseket a Telegramnak
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

def get_links_data(page):
    return page.evaluate("""() => {
        const links = Array.from(document.querySelectorAll('a'));
        return links.map(a => ({
            href: a.href,
            text: a.innerText || ""
        }));
    }""")

def main():
    print("🚀 Licitnapló Monitor (Végtelen Görgetés Verzió) elindult...")
    old_records = load_database()
    
    config = load_and_update_config()
    print(f"🔍 Aktív szűrés -> Csoport: '{config['keyword']}', Max ár: {config['max_ar']:,} Ft")
    
    target_url = f"https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-{config['max_ar']}&status=aktiv"
    
    links_data = []
    
    with sync_playwright() as p:
        try:
            print("--> Virtuális Chrome indítása...")
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900}
            )
            page = context.new_page()
            
            print(f"--> Oldal betöltése: {target_url}")
            page.goto(target_url, wait_until="networkidle", timeout=60000)
            page.wait_
