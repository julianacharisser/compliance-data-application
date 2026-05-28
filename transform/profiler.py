import json
from collections import Counter
import re

# ─── Constants ────────────────────────────────────────────────────────────────

CIA_PATH = "ingest/cia_raw.json"
OFAC_PATH = "ingest/ofac_raw.json"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def section(title):
    """Prints a section header to make the report easy to read."""
    print(f"\n{'═' * 55}")
    print(f"  {title}")
    print(f"{'═' * 55}")


def bullet(label, value):
    """Prints a single stat line."""
    print(f"  {label:<45} {value}")


def load_json(path):
    """Loads a JSON file and returns the data."""
    print(f"Loading {path}...")
    with open(path) as f:
        return json.load(f)


# ─── CIA Profiler ─────────────────────────────────────────────────────────────

def profile_cia(data):
    """
    Profiles the raw CIA data and prints a report.
    Key outputs:
    - All unique title abbreviations → feeds the abbreviation map in normalizer
    - VACANT positions → need special handling in normalizer
    - Countries with zero leaders → edge case to handle
    """
    section("CIA WORLD LEADERS — DATA PROFILE")

    total_countries = len(data)
    total_leaders = sum(len(c["leaders"]) for c in data)

    bullet("Total countries:", total_countries)
    bullet("Total leader records:", total_leaders)

    # ── Empty leaders list ────────────────────────────────────────────────────
    empty_countries = [c["country"] for c in data if len(c["leaders"]) == 0]
    bullet("Countries with zero leaders:", len(empty_countries))
    if empty_countries:
        print(f"    → {empty_countries}")

    # ── VACANT positions ──────────────────────────────────────────────────────
    # VACANT means the position exists but no one holds it
    vacant = [
        (c["country"], l["title"])
        for c in data
        for l in c["leaders"]
        if "VACANT" in l.get("name", "").upper()
    ]
    bullet("VACANT positions:", len(vacant))
    if vacant:
        print(f"    → Sample (first 5): {vacant[:5]}")

    # ── Empty names ───────────────────────────────────────────────────────────
    empty_names = [
        (c["country"], l.get("title", ""))
        for c in data
        for l in c["leaders"]
        if not l.get("name", "").strip()
    ]
    bullet("Leaders with empty name:", len(empty_names))
    if empty_names:
        print(f"    → Sample (first 5): {empty_names[:5]}")

    # ── Empty titles ──────────────────────────────────────────────────────────
    empty_titles = [
        (c["country"], l.get("name", ""))
        for c in data
        for l in c["leaders"]
        if not l.get("title", "").strip()
    ]
    bullet("Leaders with empty title:", len(empty_titles))
    if empty_titles:
        print(f"    → Sample (first 5): {empty_titles[:5]}")

    # ── Unique abbreviations ──────────────────────────────────────────────────
    # Extract all words ending in "." from titles — these are abbreviations
    # This directly tells us what to put in the abbreviation map
    abbrev_counter = Counter()
    for c in data:
        for l in c["leaders"]:
            title = l.get("title", "")
            abbrevs = re.findall(r'\b[A-Za-z]+\.', title)
            for a in abbrevs:
                abbrev_counter[a] += 1

    bullet("Unique abbreviations found:", len(abbrev_counter))
    print(f"    → All abbreviations (most common first):")
    for abbrev, count in abbrev_counter.most_common():
        print(f"       {abbrev:<20} appears {count} times")

    # ── Honorifics ────────────────────────────────────────────────────────────
    honorifics = Counter(
        l.get("honorific", "")
        for c in data
        for l in c["leaders"]
        if l.get("honorific", "").strip()
    )
    bullet("Non-empty honorifics:", sum(honorifics.values()))
    if honorifics:
        print(f"    → {dict(honorifics.most_common(10))}")

    # ── Caveat and diplomatic_exchange ────────────────────────────────────────
    caveats = [c["country"] for c in data if c.get("caveat", "").strip()]
    diplo = [c["country"] for c in data if c.get("diplomatic_exchange", "").strip()]
    bullet("Countries with caveat:", len(caveats))
    if caveats:
        print(f"    → {caveats}")
    bullet("Countries with diplomatic_exchange:", len(diplo))
    if diplo:
        print(f"    → {diplo}")

    # ── Duplicate names within same country ───────────────────────────────────
    dupes = []
    for c in data:
        names = [l.get("name", "") for l in c["leaders"]]
        seen = set()
        for name in names:
            if name in seen and name:
                dupes.append((c["country"], name))
            seen.add(name)
    bullet("Duplicate names within same country:", len(dupes))
    if dupes:
        print(f"    → {dupes[:5]}")

    # ── Sample country names for ISO mapping ─────────────────────────────────
    print(f"\n  Sample country names (first 20) — check ISO mapping:")
    for c in data[:20]:
        print(f"    {c['country']}")


# ─── OFAC Profiler ────────────────────────────────────────────────────────────

def profile_ofac(data):
    """
    Profiles the raw OFAC data and prints a report.
    Key outputs:
    - Date format samples → feeds date normalizer
    - Unique programs → useful context
    - Missing field counts → tells normalizer what defaults to set
    """
    section("OFAC SDN LIST — DATA PROFILE")

    bullet("Total individual records:", len(data))

    # ── Missing names ─────────────────────────────────────────────────────────
    no_first = sum(1 for r in data if not r.get("first_name", "").strip())
    no_last = sum(1 for r in data if not r.get("last_name", "").strip())
    bullet("Records with no first_name:", no_first)
    bullet("Records with no last_name:", no_last)

    # ── Dates of birth ────────────────────────────────────────────────────────
    no_dob = sum(1 for r in data if not r.get("dates_of_birth"))
    multi_dob = sum(1 for r in data if len(r.get("dates_of_birth", [])) > 1)
    bullet("Records with no date of birth:", no_dob)
    bullet("Records with multiple dates of birth:", multi_dob)

    # Collect all unique date formats — tells us what the date normalizer must handle
    date_formats = Counter()
    for r in data:
        for dob in r.get("dates_of_birth", []):
            date = dob.get("date", "")
            if date:
                # Categorize by format pattern
                if re.match(r'^\d{4}$', date):
                    date_formats["YYYY only (e.g. 1938)"] += 1
                elif re.match(r'^\d{2} \w+ \d{4}$', date):
                    date_formats["DD Mon YYYY (e.g. 03 May 1938)"] += 1
                elif re.match(r'^\d{4}-\d{2}-\d{2}$', date):
                    date_formats["YYYY-MM-DD (already ISO)"] += 1
                elif re.match(r'^\w+ \d{4}$', date):
                    date_formats["Mon YYYY (e.g. May 1938)"] += 1
                else:
                    date_formats[f"Other: {date}"] += 1

    print(f"\n  Date of birth formats found:")
    for fmt, count in date_formats.most_common():
        print(f"    {fmt:<45} {count} records")

    # ── Aliases ───────────────────────────────────────────────────────────────
    no_aliases = sum(1 for r in data if not r.get("aliases"))
    has_aliases = len(data) - no_aliases
    multi_aliases = sum(1 for r in data if len(r.get("aliases", [])) > 3)
    bullet("Records with no aliases:", no_aliases)
    bullet("Records with aliases:", has_aliases)
    bullet("Records with more than 3 aliases:", multi_aliases)

    # Alias types
    alias_types = Counter(
        a.get("type", "")
        for r in data
        for a in r.get("aliases", [])
    )
    print(f"\n  Alias types found:")
    for t, count in alias_types.most_common():
        print(f"    {t:<30} {count}")

    # ── Nationalities ─────────────────────────────────────────────────────────
    no_nationality = sum(1 for r in data if not r.get("nationalities"))
    bullet("Records with no nationality:", no_nationality)

    # All unique nationality strings — need ISO mapping
    all_nationalities = Counter(
        n
        for r in data
        for n in r.get("nationalities", [])
    )
    print(f"\n  Top 20 nationality strings (need ISO mapping):")
    for nat, count in all_nationalities.most_common(20):
        print(f"    {nat:<40} {count} records")

    # ── Programs ──────────────────────────────────────────────────────────────
    all_programs = Counter(
        p
        for r in data
        for p in r.get("programs", [])
    )
    bullet("Unique sanction programs:", len(all_programs))
    print(f"\n  All programs (most common first):")
    for prog, count in all_programs.most_common():
        print(f"    {prog:<40} {count} records")

    # ── IDs ───────────────────────────────────────────────────────────────────
    no_ids = sum(1 for r in data if not r.get("ids"))
    bullet("Records with no IDs:", no_ids)

    id_types = Counter(
        i.get("type", "")
        for r in data
        for i in r.get("ids", [])
    )
    print(f"\n  Top 10 ID types:")
    for id_type, count in id_types.most_common(10):
        print(f"    {id_type:<40} {count}")

    # ── Duplicate UIDs ────────────────────────────────────────────────────────
    uid_counts = Counter(r.get("uid", "") for r in data)
    dupes = {uid: count for uid, count in uid_counts.items() if count > 1}
    bullet("Duplicate UIDs:", len(dupes))
    if dupes:
        print(f"    → {dupes}")


# ─── Entry Point ──────────────────────────────────────────────────────────────

def run():
    """
    Loads both raw data files and prints a full data quality report.
    Run this once before writing the normalizer — it tells you exactly
    what edge cases, missing fields, and formats to handle.
    """
    cia_data = load_json(CIA_PATH)
    ofac_data = load_json(OFAC_PATH)

    profile_cia(cia_data)
    profile_ofac(ofac_data)

    section("PROFILE COMPLETE")
    print("  Use the abbreviations list to build your CIA abbreviation map.")
    print("  Use the date formats list to build your date normalizer.")
    print("  Use the nationality strings to build your ISO code map.\n")


if __name__ == "__main__":
    run()