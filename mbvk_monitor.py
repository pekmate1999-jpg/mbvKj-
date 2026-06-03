import os
import json
import requests

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
    print("🚀 Licitnapló API Monitor elindult...")
    old_records = load_database()
    
    # A Licitnapló belső adatlekérdező végpontja a te pontos szűrési paramétereiddel
    api_url = "https://licitnaplo.hu/api/auctions?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-2000000&status=aktiv"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Referer": "https://licitnaplo.hu/"
    }
    
    try:
        response = requests.get(api_url, headers=headers, timeout=30)
        if response.status_code != 200:
            print(f"❌ API hiba: {response.status_code}")
            # Ha az API közvetlenül nem elérhető, megpróbáljuk a sima főoldalt hátha beágyazott JSON van benne
            parse_from_html_backup(old_records)
            return
        
        data = response.json()
    except Exception as e:
        print(f"❌ Kivétel az API hívás során: {e}")
        parse_from_html_backup(old_records)
        return

    # Az API válaszból kiszedjük az aukciók listáját (általában 'items', 'data' vagy maga a lista)
    auctions = []
    if isinstance(data, list):
        auctions = data
    elif isinstance(data, dict):
        auctions = data.get("items", data.get("data", data.get("auctions", [])))

    print(f"📋 API által visszaadott nyers hirdetések száma: {len(auctions)}")
    process_auctions(auctions, old_records)


def parse_from_html_backup(old_records):
    """B-terv: Ha az API végpont zárt lenne, a főoldal tiszta text-elemzésével szedjük ki a kártyákat"""
    print("🔄 B-terv: Szelektorfélreértés-mentes szöveges elemzés indítása...")
    target_url = "https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-2000000&status=aktiv"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    try:
        res = requests.get(target_url, headers=headers, timeout=20)
        html_content = res.text
    except:
        return

    # Regexszel kivágjuk az összes olyan szövegrészt, ami településnévvel kezdődik és 'Ft'-ra végződik
    # A Licitnapló forrásában a kártyák jól látható mintát követnek
    blocks = re.findall(r'([^<>]+?\d{4}\s+[^<>]+?\d+[\d\s]*Ft)', html_content)
    auctions = []
    
    for block in blocks:
        try:
            # Megkeressük benne az árat
            price_match = re.search(r'(\d+[\d\s]*)\s*Ft', block)
            if not price_match:
                continue
            price = int(price_match.group(1).replace(" ", "").strip())
            
            if not (50000 <= price <= 2000000):
                continue
                
            # Kiszedjük a települést és a címet a megtalált szövegtömbből
            clean_block = re.sub(r'\s+', ' ', block).strip()
            
            # Generálunk egy egyedi ID-t a szövegből
            auction_id = "ln_b_" + "".join(filter(str.isalnum, clean_block[:30])) + f"_{price}"
            
            auctions.append({
                "id": auction_id,
                "title": "Licitnapló Ingatlan",
                "address": clean_block,
                "price": price,
                "url": "https://licitnaplo.hu/?bekoltozheto=true&tulajdoniHanyad=true&tehermentes=true&ar=0-2000000&status=aktiv"
            })
        except:
            continue
            
    process_auctions(auctions, old_records)


def process_auctions(auctions, old_records):
    if not auctions:
        print("😴 Nincs feldolgozható hirdetés.")
        return

    new_found_count = 0

    for item in auctions:
        try:
            # Rugalmas mezőkezelés attól függően, hogy az API hogyan nevezi a kulcsokat
            auction_id = str(item.get("id", item.get("_id", item.get("auctionId", ""))))
            if not auction_id:
                continue
                
            db_id = f"ln_{auction_id}"
            
            telepules = item.get("city", item.get("telepules", item.get("title", "Licitnapló Ingatlan")))
            cim = item.get("address", item.get("cim", item.get("location", telepules)))
            kikialtasi_ar = int(item.get("price", item.get("ar", item.get("currentPrice", 0))))
            ugyszam = item.get("caseNumber", item.get("ugyszam", "Lásd az adatlapon"))
            
            # Ha az ár valamiért 0 vagy nem jött át, megpróbáljuk kiszedni a nyers adatokból
            if kikialtasi_ar == 0:
                continue

            slug = item.get("slug", "")
            if slug:
                full_link = f"https://licitnaplo.hu/arveres/{slug}"
            else:
                full_link = f"https://licitnaplo.hu/arveres/{auction_id}"

            # --- SZŰRÉS ÉS TELEGRAM KÜLDÉS ---
            if db_id not in old_records:
                new_found_count += 1
                old_records.append(db_id)

                ar_kiiras = f"{kikialtasi_ar:,} HUF"
                üzenet = (
                    f"🚨 *ÚJ OLCSÓ INGATLAN TALÁLAT!* (Licitnapló API)\n\n"
                    f"📍 *Település:* {telepules}\n"
                    f"🏠 *Pontos cím:* {cim}\n"
                    f"💰 *Kikiáltási ár:* {ar_kiiras}\n"
                    f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n"
                    f"🔹 *Ügyszám:* `{ugyszam}`\n\n"
                    f"🔗 [Ugrás a konkrét hirdetmény adatlapjára]({full_link})"
                )
                send_telegram_message(üzenet)
                
        except Exception as e:
            print(f"⚠️ Hiba egy tétel feldolgozásakor: {e}")
            continue

    if new_found_count > 0:
        save_database(old_records)
        print(f"💾 {new_found_count} új ingatlan elmentve.")
    else:
        print("😴 Nincs új találat.")


if __name__ == "__main__":
    main()
