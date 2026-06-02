import os
import json
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

print("🏠 MBVK Profi Monitor indítása...")

# 1. Elindítunk egy session-t, hogy megmaradjanak a sütik
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "hu,en-US;q=0.7,en;q=0.3"
})

# 2. Megnyitjuk a főoldalt, hogy megkapjuk az alap sütiket
FOOLDAL = "https://arveres.mbvk.hu/arverezok/index.php?page=hirdetmeny&arveres_jellege=1"
try:
    session.get(FOOLDAL, timeout=20)
except Exception as e:
    print(f"❌ Nem sikerült elérni az MBVK főoldalt: {e}")
    exit(1)

# 3. Elküldjük a pontos keresési adatokat (POST kéréssel), pont úgy, mintha rákattintottál volna a Keresés gombra
# Beköltözhető: I, Tehermentes: I, Tulajdoni hányad: 1/1
search_data = {
    "tulajdoni_hanyad": "1/1",
    "bekoltozheto": "I",
    "tehermentes": "I",
    "arveres_jellege": "1", # 1 = Ingatlan
    "page": "hirdetmeny",
    "submit": "Keresés"
}

try:
    # Az MBVK az űrlapokat ugyanarra az URL-re küldi vissza
    response = session.post(FOOLDAL, data=search_data, timeout=20)
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text, "html.parser")
except Exception as e:
    print(f"❌ Hiba a keresési űrlap elküldésekor: {e}")
    exit(1)

# Előző állapot betöltése
try:
    with open("mbvk_state.json", "r", encoding="utf-8") as f:
        state = json.load(f)
except:
    state = {}

# Megkeressük a találati táblázat sorait
tarsashazak = soup.find_all("tr", class_=["sor1", "sor2"])
print(f"📊 Talált nyers sorok száma az oldalon: {len(tarsashazak)}")

uj_talalatok = 0

for sor in tarsashazak:
    if not sor: continue
    
    # Kikeressük a hirdetmény részletes linkjét
    links = sor.find_all("a", href=True)
    hirdetmeny_link = ""
    for l in links:
        if "hirdetmeny_adat" in l["href"]:
            hirdetmeny_link = "https://arveres.mbvk.hu/arverezok/" + l["href"]
            break
            
    if not hirdetmeny_link: continue
    arveres_id = hirdetmeny_link.split("id=")[-1] if "id=" in hirdetmeny_link else hirdetmeny_link

    # Ha már láttuk régebben, átugorjuk
    if arveres_id in state:
        continue

    # Cellák adatainak kibányászása biztonságosan
    cells = [c.get_text(strip=True) for c in sor.find_all("td")]
    
    # Az MBVK táblázat felépítése:
    # 0: Ügyszám, 1: Jelleg/Megnevezés, 2: Település, 3: Licit kezdete, 4: Kikiáltási ár
    if len(cells) >= 5:
        megnevezes = cells[1]
        telepules = cells[2]
        ar_szoveg = cells[4]
    else:
        continue # Ha nincs elég adat a sorban, hibás sor, kihagyjuk

    # Ár megtisztítása (kiszedünk minden betűt és pontot, csak a szám marad)
    try:
        ar_tiszta = int("".join([s for s in ar_szoveg if s.isdigit()]))
    except:
        ar_tiszta = 0

    # 1.000.000 Ft-os összeghatár szűrése
    if ar_tiszta > 1000000 or ar_tiszta == 0:
        continue

    uj_talalatok += 1
    
    # Értesítés összerakása
    message = (
        f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
        f"📍 *Helyszín:* {telepules}\n"
        f"🏠 *Típus:* {megnevezes}\n"
        f"💰 *Kikiáltási ár:* {ar_szoveg}\n"
        f"⚖️ *Státusz:* 1/1, Tehermentes, Beköltözhető\n\n"
        f"🔗 [Árverési adatlap megnyitása]({hirdetmeny_link})"
    )
    
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"})
    state[arveres_id] = ar_szoveg

# Mentés
with open("mbvk_state.json", "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)

# Reggeli jelentés
if uj_talalatok == 0:
    status_message = "☀️ *Jó reggelt! Az MBVK figyelő sikeresen lefutott.*\n\nA megadott szűrések alapján (1/1, tehermentes, beköltözhető, 1M Ft alatt) jelenleg nincs új hirdetmény a rendszerben. ☕"
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": status_message, "parse_mode": "Markdown"})
print("✅ Futás vége.")
