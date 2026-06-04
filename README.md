# MBVK Árverési Monitor

Automatikusan figyeli az [MBVK árverési rendszerét](https://arveres.mbvk.hu/), és Telegram értesítést küld az új, szűrési feltételeknek megfelelő ingatlan árverésekről.

## Funkciók

- Playwright-alapú Angular SPA scraping (JavaScript renderelés)
- Megye, beköltözhetőség, tulajdoni hányad és ár szerinti szűrés
- SQLite duplikáció-védelem
- Telegram értesítés
- GitHub Actions integráció (30 percenként fut)

## Szűrési feltételek

| Feltétel | Értékek |
|---|---|
| Megye | Veszprém, Zala, Somogy, Pest, Komárom, Fejér, Nógrád, Bács-Kiskun, Jász-Nagykun |
| Beköltözhető | igen |
| Tulajdoni hányad | 1/1 vagy 1/2 + 1/2 |
| Maximum ár | 1 000 000 Ft |

## Telepítés és futtatás

### Lokális futtatás

```bash
# Python 3.11+
pip install -r requirements.txt
playwright install chromium

export TELEGRAM_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"

python mbvk_monitor.py
```

### GitHub Actions

1. Fork/clone a repót
2. Állítsd be a repository secrets-t:
   - `TELEGRAM_TOKEN` – Telegram bot token ([@BotFather](https://t.me/BotFather))
   - `TELEGRAM_CHAT_ID` – chat ID ([@userinfobot](https://t.me/userinfobot))
3. A workflow automatikusan fut 30 percenként

## Fájlok

```
mbvk_monitor.py               # Főprogram
requirements.txt              # Python függőségek
.github/workflows/mbvk.yml   # GitHub Actions workflow
mbvk_v7.db                   # SQLite adatbázis (automatikusan létrejön)
```

## Adatbázis

```sql
CREATE TABLE properties (
    auction_id TEXT PRIMARY KEY,
    created    TEXT NOT NULL
);
```

Minden feldolgozott árverés ID-ja eltárolódik, így ugyanarról az árverésről csak egyszer érkezik értesítés.

## Telegram üzenet formátum

```
🏠 ÚJ MBVK TALÁLAT

📍 Cím: ...
💰 Ár: ...
📈 Legmagasabb licit: ...
📊 Licitek száma: ...
📐 Telekméret: ...
💵 Ft/m²: ...
🏘 Beköltözhető: igen
📜 Tulajdon: 1/1
⏰ Árverés vége: ...
🔗 Link: ...
```

## Megjegyzések

- Az MBVK oldal Angular SPA, ezért Playwright szükséges a JavaScript rendereléshez
- A scraper több fallback szelektort és regex mintát használ a robusztusság érdekében
- GitHub Actions cache-eli az SQLite adatbázist a futások között
