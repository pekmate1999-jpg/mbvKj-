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
    print("🚀 Licitnapló Atombiztos Görgető Monitor elindult...")
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
            page.wait_for_timeout(4000)
            
            # Dinamikus görgetés az összes tételért (Lazy Loading kisjátszása)
            print("--> Szakaszos mélygörgetés indítása...")
            previous_height = page.evaluate("document.body.scrollHeight")
            
            for scroll_step in range(12):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                page.wait_for_timeout(2000)  # Több időt hagyunk a betöltődésre
                
                new_height = page.evaluate("document.body.scrollHeight")
                if new_height == previous_height:
                    break
                previous_height = new_height
            
            html_content = page.content()
            browser.close()
        except Exception as e:
            print(f"❌ Playwright hiba: {e}")
            send_telegram_message(f"⚠️ *Playwright hiba*: {e}")
            return

    if not html_content or len(html_content) < 1000:
        return

    soup = BeautifulSoup(html_content, "html.parser")
    
    # Megkeressük az összes olyan div-et, ami hirdetési kártya
    cards = soup.find_all("div", class_=lambda x: x and 'card' in x.lower())
    print(f"📋 Összesen megtalált kártyák száma: {len(cards)}")
    
    arveresek = []
    feldolgozott_idk = set()

    for card in cards:
        try:
            card_text = card.get_text(separator="\n").strip()
            lines = [l.strip() for l in card_text.split("\n") if l.strip()]
            
            # Ha nincs benne ár, vagy gyanúsan rövid, átugorjuk (pl. menüpontok)
            if not lines or "000 Ft" not in card_text or "Keresel" in card_text:
                continue

            # 1. Adatok kinyerése a sorokból
            telepules = lines[0]
            cim = lines[1] if len(lines) > 1 and "Ft" not in lines[1] and "nap" not in lines[1] else telepules
            
            # 2. Ár kiszedése
            kikialtasi_ar = 0
            for line in lines:
                if "ft" in line.lower():
                    digits = "".join(filter(str.isdigit, line))
                    if digits and 50000 <= int(digits) <= 2500000:
                        kikialtasi_ar = int(digits)
                        break
            
            if kikialtasi_ar == 0:
                continue

            # 3. Direkt link keresése - Ha nincs, akkor a főoldali szűrt linket kapja meg, de NEM DOBJUK EL!
            link_el = card.find("a")
            href = link_el.get("href") if link_el else ""
            
            if href and len(href) > 2 and not href.startswith("#") and "javascript" not in href:
                full_link = href if href.startswith("http") else f"https://licitnaplo.hu{href}"
                id_match = re.search(r'(\d+)(?:[^\d]*)$', href)
                auction_id = f"ln_{id_match.group(1)}" if id_match else f"ln_{hash(cim[:15])}_{kikialtasi_ar}"
            else:
                # Golyóálló B-terv: ha nincs tiszta link, a címből csinálunk ID-t, a link pedig a szűrt lista
                slug = "".join(filter(str.isalnum, cim))[:25]
                auction_id = f"ln_fallback_{slug}_{kikialtasi_ar}"
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
        except Exception as e:
            print(f"Kártya hiba: {e}")
            continue

    print(f"📊 Beolvasásra kész egyedi tételek: {len(arveresek)}")
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
                f"🏠 *Pontos cím / Leírás:* {prop['cim']}\n"
                f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                f"🔗 [Ugrás az ingatlan adatlapjára]({prop['link']})"
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
