import json

data  = json.load(open("transform/ofac_normalized.json"))
persons = data["persons"]

# Find anyone with addresses and check for duplicates
for p in persons:
    addresses = p["properties"].get("address", [])
    if len(addresses) != len(set(addresses)):
        print("DUPLICATE FOUND:", p["ofac_uid"], addresses)

print("Address dedup check done")