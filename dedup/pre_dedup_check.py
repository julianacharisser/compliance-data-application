import json
from collections import Counter

with open("ingest/ofac_raw.json") as f:
    raw = json.load(f)

with open("transform/ofac_normalized.json") as f:
    normalized = json.load(f)

# Handle both list and dict format
persons = normalized if isinstance(normalized, list) else normalized.get("persons", [])

print(f"Raw records:        {len(raw)}")
print(f"Normalized persons: {len(persons)}\n")

# ── Check name fields ──────────────────────────────────────────────────────

def get_prop(person, field):
    """Gets a property value list from a person entity."""
    return person.get("properties", {}).get(field, [])

# Spot check first 3 records side by side
print("═" * 60)
print("SIDE BY SIDE — RAW vs NORMALIZED (first 3)")
print("═" * 60)
for i in range(min(3, len(raw))):
    r = raw[i]
    p = persons[i]
    print(f"\nRecord {i+1}:")
    print(f"  raw first_name : {r.get('first_name', '')}")
    print(f"  raw last_name  : {r.get('last_name', '')}")
    raw_aliases = []
    for a in r.get('aliases', []):
        alias_full = f"{a.get('first_name', '')} {a.get('last_name', '')}".strip()
        raw_aliases.append(alias_full)
    print(f"  raw aliases    : {raw_aliases}")  
    print(f"  norm name      : {get_prop(p, 'name')}")
    print(f"  norm firstName : {get_prop(p, 'firstName')}")
    print(f"  norm lastName  : {get_prop(p, 'lastName')}")
    print(f"  norm alias     : {get_prop(p, 'alias')}")
    print(f"  norm title     : {get_prop(p, 'title')}")

# ── Stats ──────────────────────────────────────────────────────────────────
print("\n" + "═" * 60)
print("FIELD COVERAGE")
print("═" * 60)

checks = {
    "name":      lambda p: bool(get_prop(p, "name")),
    "firstName": lambda p: bool(get_prop(p, "firstName")),
    "lastName":  lambda p: bool(get_prop(p, "lastName")),
    "alias":     lambda p: bool(get_prop(p, "alias")),
    "title":     lambda p: bool(get_prop(p, "title")),
}

for field, check in checks.items():
    count = sum(1 for p in persons if check(p))
    pct = count / len(persons) * 100
    print(f"  {field:<12} present in {count:>5} / {len(persons)} records ({pct:.1f}%)")

# ── Name is full name check ────────────────────────────────────────────────
print("\n" + "═" * 60)
print("NAME COMPLETENESS CHECK")
print("═" * 60)

# broken = []
# for r, p in zip(raw, persons):
#     raw_full = f"{r.get('first_name', '')} {r.get('last_name', '')}".strip()
#     norm_name = get_prop(p, "name")
#     norm_name_str = norm_name[0] if norm_name else ""

#     # Flag if normalized name is missing or much shorter than raw
#     if not norm_name_str or len(norm_name_str) < len(raw_full) - 3:
#         broken.append({
#             "raw":  raw_full,
#             "norm": norm_name_str,
#         })

# print(f"  Potentially broken names: {len(broken)}")
# if broken:
#     # Print all broken cases for manual review    
#     for b in broken:
#         print(f"    raw='{b['raw']}' → norm='{b['norm']}'")

HONORIFICS = {"dr.", "haji", "sheikh", "shaikh", "shaykh", "sir", "hon."}

broken = []
for r, p in zip(raw, persons):
    raw_full = f"{r.get('first_name', '')} {r.get('last_name', '')}".strip()
    
    # Strip known honorifics from raw before comparing length
    raw_stripped = raw_full
    for h in HONORIFICS:
        if raw_stripped.lower().startswith(h):
            raw_stripped = raw_stripped[len(h):].strip()
    
    norm_name = get_prop(p, "name")
    norm_name_str = norm_name[0] if norm_name else ""

    if not norm_name_str or len(norm_name_str) < len(raw_stripped) - 3:
        broken.append({"raw": raw_full, "norm": norm_name_str})

print(f"  Potentially broken names: {len(broken)}")
if broken:
    # Print all broken cases for manual review    
    for b in broken:
        print(f"    raw='{b['raw']}' → norm='{b['norm']}'")

