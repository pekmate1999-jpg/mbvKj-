import os
import json
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# MBVK Ingatlanárverési kereső URL a szűrésekkel
# (Beköltözhető = I, Tehermentes = I, Tulajdoni hányad = 1/1)
URL = "https://arveres.mbvk.hu/arverezok/index.php?page=hirdetmeny&arveres_jellege=1&tulajdoni_hanyad=1%2F1&bekoltozheto=I&tehermentes=I"

print("🏠 MBVK Ingatlanfigyelő elindult...")

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

try:
    response = requests.get(URL, headers=headers, timeout=15)
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text, "html.parser")
except Exception as e:
    print(f"❌ Nem sikerült megnyitni az MBVK oldalt: {e}")
    exit(1)

# Előző állapot betöltése
try:
    with open("mbvk_state.json", "r", encoding="utf-8") as f:
        state = json.load(f)
except:
    state = {}

# Az MBVK listájában a hirdetmények általában táblázatban vagy specifikus osztályú elemekben vannak
# Megkeressük az összes árverési tételt (ügyszám alapján azonosítunk)
tarsashazak = soup.find_all("tr", class_=["sor1", "sor2"])  # MBVK struktúra szerinti sorok

if not tarsashazak:
    # Ha a dizájn változott, megpróbáljuk linkek alapján összeszedni a hirdetményeket
    tarsashazak = [a.find_parent("tr") for a in soup.find_all("a", href=True) if "hirdetmeny_adat" in a["href"]]

uj_talalatok = 0

for sor in tarsashazak:
    if not sor: continue
    
    links = sor.find_all("a", href=True)
    hirdetmeny_link = ""
    for l in links:
        if "hirdetmeny_adat" in l["href"]:
            hirdetmeny_link = "https://arveres.mbvk.hu/arverezok/" + l["href"]
            break
            
    if not hirdetmeny_link: continue

    # Kivonjuk az ügyszámot/ID-t a linkből azonosítónak
    arveres_id = hirdetmeny_link.split("id=")[-1] if "id=" in hirdetmeny_link else hirdetmeny_link

    # Ha már láttuk, kihagyjuk
    if arveres_id in state:
        continue

    # Adatok kikaparása a sorból (Cím, Kikiáltási ár)
    szoveg = sor.get_text(separator=" ", strip=True)
    
    # Kikiáltási ár keresése és ellenőrzése
    # Az MBVK-n a számok ponttal vannak elválasztva (pl. 1.000.000 Ft)
    ar_szurt = "".join([c for c in szoveg if c.isdigit()])
    
    # Megpróbáljuk kivenni az árat (a sorban lévő számokból trükkös lehet, de a hirdetmény címe és adatai a lényeg)
    # Biztonsági szűrés: ha van ár és az nagyobb, mint 1.000.000 Ft, kiszűrjük
    # (Mivel az MBVK listában a kikiáltási ár fixen szerepel, egy egyszerűsített árellenőrzést végzünk)
    
    # Leírás összeszedése
    cells = [c.get_text(strip=True) for c in sor.find_all("td")]
    if len(cells) >= 3:
        megnevezes = cells[1] # Pl. "Lakóház, udvar"
        telepules = cells[2]  # Pl. "Göd"
        ar_szoveg = cells[4] if len(cells) > 4 else "Nem meghatározott"
    else:
        megnevezes = "Ingatlan"
        telepules = "Ismeretlen"
        ar_szoveg = "Lásd a linken"

    # Ár átalakítása számmá az ellenőrzéshez
    try:
        ar_tiszta = int("".join([s for s in ar_szoveg if s.isdigit()]))
    except:
        ar_tiszta = 0

    # 1.000.000 Ft-os limit ellenőrzése
    if ar_tiszta > 1000000:
        continue # Túl drága, átugorjuk

    uj_talalatok += 1
    
    # Telegram üzenet összerakása
    message = (
        f"🚨 *ÚJ MBVK INGATLAN TALÁLAT!*\n\n"
        f"📍 *Helyszín:* {telepules}\n"
        f"🏠 *Típus:* {megnevezes}\n"
        f"💰 *Kikiáltási ár:* {ar_szoveg}\n"
        f"⚖️ *Státusz:* 1/1, Tehermentes, Beköltözhető\n\n"
        f"🔗 [Árverési adatlap megtekintése]({hirdetmeny_link})"
    )

    # Küldés Telegramra
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload)

    # Mentés az állapotba
    state[arveres_id] = ar_szoveg

# Állapot mentése fájlba
with open("mbvk_state.json", "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)

if uj_talalatok == 0:
    print("ℹ️ Nincs új megfelelő ingatlan az MBVK-n.")
    # Ha szeretnél státuszüzenetet az elején, ide beteheted:
    status_message = "✅ *MBVK figyelő lefutott.* Jelenleg nincs új 1.000.000 Ft alatti, 1/1-es beköltözhető ingatlan. 💤"
    payload = {"chat_id": CHAT_ID, "text": status_message, "parse_mode": "Markdown"}
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload)
