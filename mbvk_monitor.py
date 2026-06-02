import os
import json
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# MBVK szűrt URL (1/1 tulajdon, beköltözhető, tehermentes)
URL = "https://arveres.mbvk.hu/arverezok/index.php?page=hirdetmeny&arveres_jellege=1&tulajdoni_hanyad=1%2F1&bekoltozheto=I&tehermentes=I"

print("🏠 MBVK Reggeli Monitor elindult...")

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

try:
    response = requests.get(URL, headers=headers, timeout=20)
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text, "html.parser")
except Exception as e:
    print(f"❌ Hiba az MBVK elérésekor: {e}")
    exit(1)

try:
    with open("mbvk_state.json", "r", encoding="utf-8") as f:
        state = json.load(f)
except:
    state = {}

tarsashazak = soup.find_all("tr", class_=["sor1", "sor2"])
if not tarsashazak:
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
    arveres_id = hirdetmeny_link.split("id=")[-1] if "id=" in hirdetmeny_link else hirdetmeny_link

    if arveres_id in state:
        continue

    cells = [c.get_text(strip=True) for c in sor.find_all("td")]
    if len(cells) >= 3:
        megnevezes = cells[1]
        telepules = cells[2]
        ar_szoveg = cells[4] if len(cells) > 4 else "Lásd a linken"
    else:
        megnevezes = "Ingatlan"
        telepules = "Ismeretlen"
        ar_szoveg = "Lásd a linken"

    try:
        ar_tiszta = int("".join([s for s in ar_szoveg if s.isdigit()]))
    except:
        ar_tiszta = 0

    # 1.000.000 Ft limit szűrés
    if ar_tiszta > 1000000:
        continue

    uj_talalatok += 1
    
    message = (
        f"🚨 *ÚJ MBVK INGATLAN! *\n\n"
        f"📍 *Helyszín:* {telepules}\n"
        f"🏠 *Típus:* {megnevezes}\n"
        f"💰 *Kikiáltási ár:* {ar_szoveg}\n"
        f"⚖️ *Státusz:* 1/1, Tehermentes, Beköltözhető\n\n"
        f"🔗 [Adatlap megnyitása]({hirdetmeny_link})"
    )
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"})
    state[arveres_id] = ar_szoveg

with open("mbvk_state.json", "w", encoding="utf-8") as f:
    json.dump(state, f, ensure_ascii=False, indent=2)

# Reggeli státuszüzenet, ha nem volt új olcsó ingatlan
if uj_talalatok == 0:
    status_message = "☀️ *Jó reggelt! Az MBVK figyelő sikeresen lefutott.*\n\nA megadott szűrések alapján (1/1, tehermentes, beköltözhető, 1M Ft alatt) jelenleg nincs új hirdetmény a rendszerben. ☕"
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data={"chat_id": CHAT_ID, "text": status_message, "parse_mode": "Markdown"})
