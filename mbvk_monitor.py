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
    print("🚀 Licitnapló Mélygörgető és Direkt Link Monitor elindult...")
    old_records = load_database()
    
    target_url = "https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-2000000&status=aktiv"
    html_content = ""
    
    with sync_playwright() as p:
        try:
            print("--> Virtuális Chrome indítása...")
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 3000}
            )
            page = context.new_page()
            print(f"--> URL megnyitása: {target_url}")
            
            page.goto(target_url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(3000)
            
            # --- LAZY LOADING JÁTSZMA: Dinamikus görgetés az összes tételért ---
            print("--> Szakaszos mélygörgetés indítása a teljes lista betöltéséhez...")
            previous_height = page.evaluate("document.body.scrollHeight")
            
            for scroll_step in range(15):  # Akár 15-ször is legördül, hogy biztosan elérje az alját
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                page.wait_for_timeout(1500)  # Vár egy kicsit, amíg a szerver beadja az új kártyákat
                
                new_height = page.evaluate("document.body.scrollHeight")
                if new_height == previous_height:
                    # Ha a magasság nem változott, elértük az abszolút alját az 50 hirdetésnek
                    break
                previous_height = new_height
            
            print("--> Minden tétel sikeresen lerenderelve a virtuális képernyőn.")
            html_content = page.content()
            browser.close()
        except Exception as e:
            print(f"❌ Playwright hiba: {e}")
            return

    if not html_content or len(html_content) < 1000:
        return

    soup = BeautifulSoup(html_content, "html.parser")
    
    # A Licitnapló hirdetései a Bootstrap struktúra szerint div-ekben laknak, amik tartalmaznak árat.
    # Megkeressük az összes div-et, ami a 'card' osztályt viseli.
    cards = soup.find_all("div", class_=lambda x: x and 'card' in x.lower())
    print(f"📋 Összesen beolvasott hirdetési kártyák száma: {len(cards)}")
    
    arveresek = []
    feldolgozott_idk = set()

    for card in cards:
        try:
            # Megpróbáljuk kinyerni a kártya belső linkjét vagy azonosítóját
            # A Licitnapló gombjai vagy képei gyakran hordozzák az egyedi azonosítót a linkben
            link_el = card.find("a")
            href = link_el.get("href") if link_el else ""
            
            card_text = card.get_text(separator="\n").strip()
            lines = [l.strip() for l in card_text.split("\n") if l.strip()]
            if not lines or "000 Ft" not in card_text:
                continue

            # 1. Település és Cím bányászat
            telepules = lines[0]
            cim = lines[1] if len(lines) > 1 and not any(x in lines[1] for x in ["Ft", "nap", "MBVK"]) else telepules
            
            # 2. Ár kinyerése
            kikialtasi_ar = 0
            for line in lines:
                if "ft" in line.lower():
                    digits = "".join(filter(str.isdigit, line))
                    if digits and 50000 <= int(digits) <= 2000000:
                        kikialtasi_ar = int(digits)
                        break
            
            if kikialtasi_ar == 0:
                continue

            # 3. DIREKT ADATLAP LINK GENERÁLÁSA
            # Ha van belső relatív link az a-tagben (pl. /adatlap-valami-12345), azt használjuk
            if href and len(href) > 2 and not href.startswith("#"):
                full_link = href if href.startswith("http") else f"https://licitnaplo.hu{href}"
                # Kinyerjük a link végén lévő egyedi számot azonosítónak
                id_match = re.search(r'(\d+)(?:[^\d]*)$', href)
                auction_id = f"ln_{id_match.group(1)}" if id_match else f"ln_{hash(clean_text[:15])}"
            else:
                # B-terv: ha nem talált közvetlen linket, magából a címből gyártunk ID-t, a link pedig a főoldal marad
                slug = "".join(filter(str.isalnum, cim))[:20]
                auction_id = f"ln_{slug}_{kikialtasi_ar}"
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

    print(f"📊 Megrostált, egyedi ingatlanok száma: {len(arveresek)}")
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
                f"📍 *Település:* {prop['telepules']}\n"
                f"🏠 *Pontos cím:* {prop['cim']}\n"
                f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                f"🔗 [Ugrás a konkrét hirdetmény adatlapjára]({prop['link']})"
            )
            send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új tétel elmentve.")
    else:
        print("😴 Nincs új találat.")
        send_telegram_message("✅Sikeres Futtatás.❌ Nincs új tárgy.❌")

if __name__ == "__main__":
    main()
