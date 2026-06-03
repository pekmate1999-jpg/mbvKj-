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
    print("🚀 Licitnapló Brutál-Görgető és Szövegbányász Monitor elindult...")
    old_records = load_database()
    
    target_url = "https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-2000000&status=aktiv"
    
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
            page.wait_for_timeout(5000)
            
            # 1. LAZY LOADING KIKÉNYSZERÍTÉSE - Erőteljes görgetés lefelé
            print("--> Görgetés az összes tétel betöltéséhez...")
            for i in range(15):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                page.wait_for_timeout(1500)
            
            page.evaluate("window.scrollTo(0, 0);") # Vissza a tetejére a biztonság kedvéért
            page.wait_for_timeout(1000)

            # 2. ÖSSZES LINK ÉS SZÖVEG KINYERÉSE KÖZVETLENÜL A RENDERELETT BÖNGÉSZŐBŐL
            print("--> Linkek és kártyaszövegek bányászata szelektorok nélkül...")
            
            # Megkeressük az összes link elemet az oldalon
            links_data = page.evaluate("""() => {
                const links = Array.from(document.querySelectorAll('a'));
                return links.map(a => ({
                    href: a.href,
                    text: a.innerText || ""
                }));
            }""")
            
            # Kivesszük a teljes body szöveget is biztonsági tartaléknak
            body_text = page.locator("body").inner_text()
            browser.close()
        except Exception as e:
            print(f"❌ Playwright futási hiba: {e}")
            send_telegram_message(f"⚠️ *Playwright hiba*: {e}")
            return

    arveresek = []
    feldolgozott_idk = set()

    # Szűrjük ki azokat a linkeket, amik valódi ingatlan hirdetésekre mutatnak
    # A Licitnapló egyedi azonosítót vagy településnevet tesz a linkbe, és a szövegében ott van a Ft
    for item in links_data:
        href = item.get("href", "")
        text = item.get("text", "").strip()
        
        # Ha a link szövegében vagy a környezetében van ár, és nem főoldali navigáció
        if "000 Ft" in text and "licitnaplo.hu/" in href and not any(x in href for x in ["status=", "ar=", "bekoltozheto="]):
            try:
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                if not lines:
                    continue
                
                # Ár kinyerése
                kikialtasi_ar = 0
                for line in lines:
                    if "ft" in line.lower():
                        digits = "".join(filter(str.isdigit, line))
                        if digits and 50000 <= int(digits) <= 2500000:
                            kikialtasi_ar = int(digits)
                            break
                
                if kikialtasi_ar == 0:
                    continue
                
                telepules = lines[0]
                cim = lines[1] if len(lines) > 1 and "Ft" not in lines[1] else telepules
                
                # Egyedi ID gyártása a linkből
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

    # B-TERV: Ha a linkeken belüli text mégis üres lenne, a nyers body szöveget vágjuk szét az árak mentén
    if not arveresek:
        print("🔄 B-terv: Nyers szöveges blokkolás indítása...")
        blocks = re.findall(r'([^<> \n]+?\d{4}\s+[^<> \n]+?[\s\S]*?\d+[\d\s]*Ft)', body_text)
        for block in blocks:
            try:
                price_match = re.search(r'(\d+[\d\s]*)\s*Ft', block)
                if not price_match:
                    continue
                price = int(price_match.group(1).replace(" ", "").replace("\xa0", "").strip())
                
                if not (50000 <= price <= 2000000):
                    continue
                    
                clean_text = re.sub(r'\s+', ' ', block).strip()
                auction_id = "ln_text_" + "".join(filter(str.isalnum, clean_text[:15])) + f"_{price}"
                
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

    print(f"📊 Detektált ingatlanok száma: {len(arveresek)}")
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
                f"🏠 *Pontos cím / Infó:* {prop['cim']}\n"
                f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                f"🔗 [Ugrás az ingatlan adatlapjára]({prop['link']})"
            )
            send_telegram_message(üzenet)

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új tétel elmentve.")
    else:
        print("😴 Ténylegesen nincs új találat az adatbázishoz képest.")
        send_telegram_message("✅Sikeres Futtatás.❌ Nincs új tárgy.❌")

if __name__ == "__main__":
    main()
