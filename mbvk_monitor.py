import os
import json
import requests

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})

def test_api():
    api_url = "https://arveres.mbvk.hu/api/v1/arveresek/kereses"
    
    # Teljesen tiszta lekérés, szűrések nélkül, hogy lássuk a nyers adatot
    payload = {
        "page": 0,
        "size": 10
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(api_url, json=payload, headers=headers)
        
        # Elmentjük a nyers választ, hogy meg tudjuk nézni GitHubon
        with open("valasz.json", "w", encoding="utf-8") as f:
            json.dump(response.json(), f, ensure_ascii=False, indent=4)
            
        send_telegram_message(f"Teszt lefutott! Státusz: {response.status_code}. Nézd meg a valasz.json fájlt a repódban!")
        
    except Exception as e:
        send_telegram_message(f"Hiba történt a teszt során: {str(e)}")

if __name__ == "__main__":
    test_api()
