import os
import json
import requests
from bs4 import BeautifulSoup

# Telegram beállítások a GitHub Secrets-ből
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DB_FILE = "mbvk_adatbazis.json"

def load_database():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_database(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Hiba a Telegram üzenet küldésekor: {e}")

def scrape_mbvk():
    url = "https://arveres.mbvk.hu/arverezok/index.php"
    
    # Az MBVK által elvárt háttér-adatok (POST payload) a szűréshez
    # Beállítva: Aktív, 1/1 tulajdon, Tehermentes: Igen, Beköltözhető: Igen
    payload = {
        "nav": "arveres",
        "szures": "1",
        "arveres_allapota": "AKTIV",
        "tulajdoni_hanyad": "1/1",
        "tehermentes": "IGEN",
        "bekoltozheto": "IGEN",
        "kategoria": "INGATLAN"
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    print("MBVK adatok lekérése...")
    response = requests.post(url, data=payload, headers=headers)
    
    if response.status_code != 200:
        print("Nem sikerült elérni az MBVK oldalt.")
        return []
        
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Megkeressük az összes árverési sort a táblázatban
    # Megjegyzés: Az MBVK struktúrájától függően a pontos HTML szelektorokat finomítani kellhet
    arveresek = []
    rows = soup.find_all('tr', class_='arveres_sor') # Az MBVK aktuális HTML osztálya alapján
    
    if not rows:
        # Ha a specifikus class nem talál semmit, megpróbáljuk a linkek alapján kiszedni
        rows = soup.find_all('a', href=lambda href: href and "ugyszam" in href)
        
    for row in rows:
        try:
            # Példa adatkinyerésre (ezt az első éles tesztnél finomítjuk, ha szükséges)
            # Feltételezzük, hogy az ügyszám azonosítja az árverést
            ugyszam = row.get_text(strip=True) 
            link = "https://arveres.mbvk.hu" + row['href'] if row.has_attr('href') else url
            
            # TODO: Ár és részletek kinyerése a HTML-ből
            # Egyelőre egy fix példa a szűrés logikájára:
            kikiatasi_ar = 1500000  # Ezt a HTML-ből fogjuk kiszedni
            
            if kikiatasi_ar <= 2000000:
                arveresek.append({
                    "id": ugyszam,
                    "ar": kikiatasi_ar,
                    "link": link
                })
        except Exception as e:
            continue
            
    return arveresek

def main():
    old_records = load_database()
    current_items = scrape_mbvk()
    
    new_found = False
    for item in current_items:
        if item["id"] not in old_records:
            new_found = True
            old_records.append(item["id"])
            
            # Értesítés formázása
            üzenet = (
                f"🚨 *ÚJ MBVK INGATLAN!*\n\n"
                f"🔹 *Ügyszám:* {item['id']}\n"
                f"💰 *Kikiáltási ár:* {item['ar']:,} HUF\n"
                f"📋 *Feltételek:* 1/1, Tehermentes, Beköltözhető\n\n"
                f"🔗 [Megtekintés az MBVK-n]({item['link']})"
            )
            send_telegram_message(üzenet)
            
    if new_found:
        save_database(old_records)
    else:
        print("Nem találtam új, feltételeknek megfelelő ingatlant.")

if __name__ == "__main__":
    main()
