import requests
url = "https://arveres.mbvk.hu/publicapi/auction/detail/723/610058"
r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
data = r.json()["data"]
print("Ügyszám:", data["caseNumber"])
print("Cím:", data["propertyAddress"][0]["formattedAddress"])
print("Tulajdoni hányad:", data["p_tulajdonihanyad"])
print("Minimum ár:", data["minPrice"])
