import os
import json
import requests
from bs4 import BeautifulSoup
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
    print("🚀 Licitnapló Diagnosztikus Monitor elindult...")
    old_records = load_database()
    
    # A pontos, készre szűrt Licitnapló URL
    target_url = "https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-2000000&status=aktiv"
    
    html_content = ""
    
    # Virtuális böngésző indítása, hogy a Licitnapló ne tudja blokkolni a kérést
    with sync_playwright() as p:
        try:
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
            print(f"--> URL megnyitása: {target_url}")
            
            page.goto(target_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(4000)
            
            html_content = page.content()
            browser.close()
        except Exception as e:
            print(f"❌ Playwright hiba: {e}")
            send_telegram_message(f"⚠️ *Licitnapló Monitor Hiba:* Nem sikerült megnyitni az oldalt a böngészővel.\nHiba: {e}")
            return

    if not html_content or len(html_content) < 1000:
        print("📭 Az oldal forrása üres vagy túl rövid.")
        send_telegram_message("❌ *Licitnapló Monitor:* A szerver üres oldalt küldött vissza (Blokkolás gyanú).")
        return

    soup = BeautifulSoup(html_content, "html.parser")
    
    # Szelektorfüggetlen szöveges blokkkeresés a kártyák mintájára (Település + Ft összeg)
    # A Licitnapló kártyáit a bennük lévő irányítószámok és 'Ft' feliratok alapján fogjuk meg
    blocks = re.findall(r'([^<>]+?\d{4}\s+[^<>]+?\d+[\d\s]*Ft)', html_content)
    
    # Ha a regex nem talált semmit, megpróbáljuk a nyers text-alapú kártyakeresést
    if not blocks:
        text_elements = soup.find_all(text=re.compile(r'\d+[\d\s]*Ft'))
        blocks = [el.parent.parent.get_text() for el in text_elements if el.parent and el.parent.parent]

    print(f"📋 Talált nyers hirdetési blokkok száma: {len(blocks)}")
    
    arveresek = []
    feldolgozott_idk = set()

    for block in blocks:
        try:
            # Megkeressük az árat a blokkban
            price_match = re.search(r'(\d+[\d\s]*)\s*Ft', block)
            if not price_match:
                continue
            price = int(price_match.group(1).replace(" ", "").replace("\xa0", "").strip())
            
            # Szigorú árszűrés (2 000 000 Ft alatt)
            if not (50000 <= price <= 2000000):
                continue
                
            # Megtisztítjuk a szöveget a felesleges szóközöktől és törésektől
            clean_text = re.sub(r'\s+', ' ', block).strip()
            
            # Generálunk egy egyedi ID-t a szöveg elejéből és az árból
            auction_id = "ln_" + "".join(filter(str.isalnum, clean_text[:20])) + f"_{price}"
            
            if auction_id in feldolgozott_idk:
                continue
            feldolgozott_idk.add(auction_id)

            # Megpróbáljuk szépen szétszedni a települést és a címet
            parts = [p.strip() for p in clean_text.split(",") if p.strip()]
            telepules = parts[0] if parts else "Licitnapló Ingatlan"
            
            arveresek.append({
                "id": auction_id,
                "telepules": telepules,
                "cim": clean_text[:100] + "...",
                "ar": price
            })
        except:
            continue

    new_found_count = 0

    for prop in arveresek:
        auction_id = prop["id"]
        kikialtasi_ar = prop["ar"]

        if auction_id not in old_records:
            new_found_count += 1
            old_records.append(auction_id)

            ar_kiiras = f"{kikialtasi_ar:,} HUF"
            üzenet = (
                f"🚨 *ÚJ OLCSÓ INGATLAN TALÁLAT!* (Licitnapló)\n\n"
                f"📍 *Helyszín:* {prop['telepules']}\n"
                f"🏠 *Leírás:* {prop['cim']}\n"
                f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                f"🔗 [Megnyitás a Licitnapló Keresőben]({target_url})"
            )
            send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új ingatlan elmentve.")
    else:
        # A kért egyedi státuszüzenet, ha lefutott a kód, de nincs új tétel a limit alatt
        print("😴 Nincs új találat.")
        send_telegram_message("✅Sikeres Futtatás.❌ Nincs új tárgy.❌")

if __name__ == "__main__":
    main()
