"""
transform/normalize_ofac.py
============================
Transforms ingest/ofac_raw.json into FtM-shaped Person entities and writes
them to transform/ofac_normalized.json.

Unlike the CIA normalizer, OFAC records carry NO position/role data — we
only know that a person is sanctioned.  So this file produces only Person
entities (no Occupancy entities).

Run:
    python transform/normalize_ofac.py

Output:
    transform/ofac_normalized.json

OUTPUT STRUCTURE:
    {
        "meta": {
            "total_persons": 7506,
            "retrieved_at":  "2026-06-03"
        },
        "persons": [...]
    }

WHAT OFAC DATA GIVES US:
    Per person:
        first_name       → Person.firstName (honorific stripped if present)
        last_name        → Person.lastName  (title-cased from ALL CAPS)
        title            → Person.position  (role description, not honorific)
        aliases          → Person.alias     (all a.k.a. entries)
        dates_of_birth   → Person.birthDate (multiple = OFAC uncertainty)
        nationalities    → Person.nationality (ISO alpha-2)
        citizenships     → Person.citizenship (ISO alpha-2)
        ids              → passportNumber / idNumber / cryptoWalletAddress
        programs         → Person.notes    ("Programs: SDGT, ...")
        remarks          → Person.notes    (appended after programs)

WHAT WE CANNOT GET FROM OFAC (left empty):
        position is loose role description, not a verified government title
        No country_code — nationality is used instead for dedup matching
        No sourceUrl    — OFAC does not publish per-person URLs

FtM TOPICS TAG:
    All OFAC persons tagged "sanction".
    Everyone on the SDN list is a sanctioned individual by definition.

CUSTOM METADATA (outside FtM properties, matches CIA normalizer pattern):
    sources   → ["ofac_sdn"]
    ofac_uid  → original OFAC uid for cross-referencing raw file

Five verification checks printed at end of every run:
    1. Count sanity          — persons == unique UIDs
    2. Al-Zawahiri           — name title-cased, alias present, birth date present
    3. Multi-date person     — person with >1 birth date has all dates
    4. No ALL-CAPS last name — .title() was applied everywhere
    5. Programs in notes     — at least one person has a "Programs:" note
"""

import json
import os
import time
from datetime import date

from numpy import record

# ─── Imports ──────────────────────────────────────────────────────────────────
# helpers.py lives in the same transform/ directory.
#   make_id(source, *parts)  — UUID5 deterministic ID
#   normalize_date(raw)      — handles all OFAC date formats → ISO or None
#   to_iso_country(name)     — country name → ISO alpha-2 code (e.g. "eg")
#   title_case_name(raw)     — title-case a name while preserving known particles
from helpers import make_id, normalize_date, to_iso_country, title_case_name

# ─── Constants ────────────────────────────────────────────────────────────────

OFAC_INPUT_PATH  = "ingest/ofac_raw.json"
OFAC_OUTPUT_PATH = "transform/ofac_normalized.json"
OFAC_SOURCE_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"

# Set once per pipeline run — every entity gets the same retrievedAt value
RETRIEVED_AT = date.today().isoformat()   # e.g. "2026-06-03"

# ─── ID Type Mapping ──────────────────────────────────────────────────────────
# Maps OFAC free-text ID labels to FtM property names.
# Built from actual ID type strings found in ofac_raw.json.
#
# Three FtM properties used:
#   passportNumber     — travel documents
#   idNumber           — national IDs, tax IDs, licences, registrations
#   cryptoWalletAddress — crypto addresses (separate for searchability)

ID_TYPE_MAP = {
    # --- Passports ---
    "Passport":                          "passportNumber",
    "Diplomatic Passport":               "passportNumber",
    "Stateless Person Passport":         "passportNumber",
    "British National Overseas Passport":"passportNumber",

    # --- National / civil identity documents ---
    "National ID No.":                   "idNumber",
    "National Foreign ID Number":        "idNumber",
    "Identification Number":             "idNumber",
    "Personal ID Card":                  "idNumber",
    "Tazkira National ID Card":          "idNumber",
    "Refugee ID Card":                   "idNumber",
    "Stateless Person ID Card":          "idNumber",
    "Federal ID Card":                   "idNumber",
    "UAE Identification":                "idNumber",
    "Tarjeta de Identidad":              "idNumber",   # Colombian ID card
    "D.N.I.":                            "idNumber",   # Spain / Argentina
    "N.I.E.":                            "idNumber",   # Spain foreigner ID
    "Numero de Identidad":               "idNumber",   # Honduras
    "Kenyan ID No.":                     "idNumber",
    "Moroccan Personal ID No.":          "idNumber",
    "Bosnian Personal ID No.":           "idNumber",
    "CNP (Personal Numerical Code)":     "idNumber",   # Romania
    "Romanian Permanent Resident":       "idNumber",
    "Turkish Identification Number":     "idNumber",
    "C.U.I.":                            "idNumber",   # Guatemala
    "C.U.I.P.":                          "idNumber",   # Guatemala passport ID
    "Citizen's Card Number":             "idNumber",   # China
    "Chinese Commercial Code":           "idNumber",
    "Italian Fiscal Code":               "idNumber",

    # --- Tax / fiscal IDs ---
    "Tax ID No.":                        "idNumber",
    "C.U.R.P.":                          "idNumber",   # Mexico civil registry
    "R.F.C.":                            "idNumber",   # Mexico tax ID
    "RFC":                               "idNumber",   # alternate spelling
    "C.U.I.T.":                          "idNumber",   # Argentina tax ID
    "NIT #":                             "idNumber",   # Colombia tax ID
    "RUC #":                             "idNumber",   # Peru / Ecuador tax ID
    "SSN":                               "idNumber",   # US Social Security
    "Russian State Individual Business Registration Number Pattern (OGRNIP)": "idNumber",

    # --- Electoral / civil registry ---
    "Electoral Registry No.":            "idNumber",
    "Credencial electoral":              "idNumber",   # Mexico
    "I.F.E.":                            "idNumber",   # Mexico electoral ID
    "Cedula No.":                        "idNumber",   # Colombia

    # --- Travel / residency documents ---
    "Travel Document Number":            "idNumber",
    "Residency Number":                  "idNumber",
    "Immigration No.":                   "idNumber",
    "VisaNumberID":                      "idNumber",
    "LE Number":                         "idNumber",   # Law Enforcement

    # --- Professional / institutional ---
    "Driver's License No.":              "idNumber",
    "Pilot License Number":              "idNumber",
    "Birth Certificate Number":          "idNumber",
    "Military Registration Number":      "idNumber",
    "Cartilla de Servicio Militar Nacional": "idNumber",   # Mexico military
    "Matricula Mercantil No":            "idNumber",   # Colombia business registry
    "Registration ID":                   "idNumber",
    "Registration Number":               "idNumber",
    "Government Gazette Number":         "idNumber",
    "Serial No.":                        "idNumber",
    "License":                           "idNumber",
    "Public Security and Immigration No.": "idNumber",
    "Seafarer's Identification Document":  "idNumber",

    # --- Crypto wallet addresses ---
    "Digital Currency Address - XBT":    "cryptoWalletAddress",   # Bitcoin
    "Digital Currency Address - ETH":    "cryptoWalletAddress",   # Ethereum
    "Digital Currency Address - TRX":    "cryptoWalletAddress",   # Tron
    "Digital Currency Address - USDT":   "cryptoWalletAddress",   # Tether
    "Digital Currency Address - LTC":    "cryptoWalletAddress",   # Litecoin
    "Digital Currency Address - XMR":    "cryptoWalletAddress",   # Monero
    "Digital Currency Address - BCH":    "cryptoWalletAddress",   # Bitcoin Cash
    "Digital Currency Address - ZEC":    "cryptoWalletAddress",   # Zcash
    "Digital Currency Address - DASH":   "cryptoWalletAddress",   # Dash
    "Digital Currency Address - BTG":    "cryptoWalletAddress",   # Bitcoin Gold
    "Digital Currency Address - ETC":    "cryptoWalletAddress",   # Ethereum Classic
    "Digital Currency Address - BSV":    "cryptoWalletAddress",   # Bitcoin SV
    "Digital Currency Address - XVG":    "cryptoWalletAddress",   # Verge
    "Digital Currency Address - ARB":    "cryptoWalletAddress",   # Arbitrum
    "Digital Currency Address - BSC":    "cryptoWalletAddress",   # Binance Smart Chain
    "Digital Currency Address - USDC":   "cryptoWalletAddress",   # USD Coin
    "Digital Currency Address - SOL":    "cryptoWalletAddress",   # Solana
}

# Labels silently discarded — legal boilerplate stored as "id" entries in the
# OFAC XML, not actual document numbers.
ID_SKIP_LABELS = {
    "Gender",
    "Secondary sanctions risk:",
    "Additional Sanctions Information -",
    "Transactions Prohibited For Persons Owned or Controlled By U.S. Financial Institutions:",
    "Executive Order 13846 information:",
    "CAATSA Section 235 Information:",
    "PAIPA Section 2 Information:",
    "Additional Program Tags -",
    "Email Address",
    "Phone Number",
    "Website",
}

# Honorific prefixes that sometimes appear embedded in OFAC first_name fields.
# Stripped out so they don't pollute firstName — stored in `title` instead.
HONORIFIC_PREFIXES = {
    "Dr.", "Dr",
    "Prof.", "Prof",
    "Eng.", "Eng",
    "Rev.", "Rev",
    "Sheikh", "Shaikh",
    "Haji", "Hajj",
    "Mr.", "Mr", "Mrs.", "Mrs", "Ms.", "Ms",
}


# ─── Name Helpers ─────────────────────────────────────────────────────────────

def strip_honorific(raw_first: str) -> tuple[str | None, str | None]:
    """
    Separate an embedded honorific from a first name string.

    OFAC occasionally stores entries like first_name="Dr. Ahmad" or even
    first_name="Dr." with no actual given name.  We split those apart so
    the honorific ends up in the `title` field and firstName stays clean.

    Args:
        raw_first: The raw first_name string from an OFAC record.

    Returns:
        Tuple of (honorific_or_None, cleaned_first_or_None).

    Examples:
        "Dr. Ahmad"  → ("Dr.", "Ahmad")
        "Dr."        → ("Dr.", None)
        "Ahmad"      → (None,  "Ahmad")
    """
    if not raw_first or not raw_first.strip():
        return None, None

    parts       = raw_first.strip().split(None, 1)   # split on first whitespace only
    first_token = parts[0]

    if first_token in HONORIFIC_PREFIXES:
        honorific     = first_token
        remainder     = parts[1].strip() if len(parts) > 1 else None
        cleaned_first = remainder if remainder else None
        return honorific, cleaned_first

    return None, raw_first.strip()


def build_full_name(first_name: str | None, last_name: str | None) -> str | None:
    """
    Combine first and last name into a single display name string.

    Args:
        first_name: Given name, already title-cased.
        last_name:  Family name, already title-cased.

    Returns:
        "First Last", "Last" (if no first), "First" (if no last), or None.
    """
    parts = [p for p in [first_name, last_name] if p]
    return " ".join(parts) if parts else None

# ─── Country Helpers ─────────────────────────────────────────────────────────
def to_iso_codes(country_names: list[str]) -> list[str]:
    """
    Convert a list of country name strings to ISO alpha-2 codes.
    Entries that can't be resolved are skipped with a warning.

    Args:
        country_names: List of country name strings e.g. ["Russia", "Iran"]

    Returns:
        List of ISO alpha-2 codes e.g. ["ru", "ir"]
    """
    result = []
    for country_name in country_names:
        code = to_iso_country(country_name)
        if code:
            result.append(code)
        else:
            print(f"  [WARN] Country not mapped: '{country_name}' — storing as-is")
    return result

# ─── Field Extractors ─────────────────────────────────────────────────────────

def extract_aliases(aliases: list[dict]) -> list[str]:
    """
    Pull alias strings out of an OFAC aliases list.

    Each alias dict looks like:
        {"type": "a.k.a.", "category": "strong", "first_name": "", "last_name": "AL-ZAWAHIRI"}

    We build a display name from first+last (same logic as the main name),
    title-case it, and include it if non-empty.

    Args:
        aliases: List of alias dicts from the OFAC raw record.

    Returns:
        List of alias name strings (may be empty).
    """
    result = []
    for alias in aliases:
        first = title_case_name(alias.get("first_name", "") or "")
        last  = title_case_name(alias.get("last_name",  "") or "")
        full  = build_full_name(first or None, last or None)
        if full:
            result.append(full)
    return result


def extract_birth_dates(dates_of_birth: list[dict]) -> list[str]:
    """
    Normalise all OFAC birth date entries using helpers.normalize_date().

    OFAC stores birth dates in several formats:
        "03 May 1938"   → "1938-05-03"
        "1938"          → "1938"   (year-only, preserved as-is)
        "circa 1940"    → "1940"   (circa stripped, year preserved)
        "1938 to 1940"  → "1938"   (range: first year taken)

    Multiple dates are intentional — OFAC records uncertain DOBs as a list
    of candidates.  All are preserved so dedup matching can treat any as valid.

    Args:
        dates_of_birth: List of dicts, each with at least a "date" key.

    Returns:
        List of ISO date strings or year strings.  Unparseable entries skipped.
    """
    result = []
    for entry in dates_of_birth:
        raw        = entry.get("date", "")
        normalised = normalize_date(raw)
        if normalised:
            result.append(normalised)
    return result


def extract_country_codes(country_names: list[str]) -> list[str]:
    """
    Convert a list of country name strings to ISO alpha-2 codes.
    Entries that can't be resolved are skipped with a warning.

    Args:
        country_names: List of country name strings e.g. ["Russia", "Iran"]

    Returns:
        List of ISO alpha-2 codes e.g. ["ru", "ir"]
    """
    result = []
    for country_name in country_names:
        code = to_iso_country(country_name)
        if code:
            result.append(code)
        else:
            print(f"  [WARN] Country not mapped: '{country_name}' — storing as-is")
    return result


def extract_ids(ids: list[dict]) -> dict[str, list[str]]:
    """
    Map OFAC identity document entries to FtM property names.

    Returns a dict keyed by FtM property name, e.g.:
        {
            "passportNumber":      ["AB123456", "CD789012"],
            "idNumber":            ["12345678"],
            "cryptoWalletAddress": ["1A1zP1eP..."]
        }

    ID types not in ID_TYPE_MAP and not in ID_SKIP_LABELS print a warning
    so we can catch new label types in future OFAC updates.

    Args:
        ids: List of id dicts, each with at least "type" and "number" keys.

    Returns:
        Dict of FtM property name → list of value strings.
    """
    result: dict[str, list[str]] = {}

    for entry in ids:
        id_type   = entry.get("type",   "").strip()
        id_number = entry.get("number", "").strip()

        if not id_number:
            continue  # empty number, nothing to store

        if id_type in ID_SKIP_LABELS:
            continue  # legal boilerplate, skip silently

        ftm_prop = ID_TYPE_MAP.get(id_type)
        if ftm_prop is None:
            print(f"  [WARN] Unknown ID type: '{id_type}' — skipping")
            continue

        result.setdefault(ftm_prop, []).append(id_number)

    return result


def build_programs_note(programs: list[str]) -> str | None:
    """
    Format OFAC sanctions programs as a human-readable note string.

    Programs are short codes like "SDGT", "RUSSIA-EO14024".  Stored in
    notes so end users can see which list(s) the person appears on.

    Args:
        programs: List of program code strings.

    Returns:
        "Programs: SDGT, RUSSIA-EO14024" or None if list is empty.
    """
    if not programs:
        return None
    return "Programs: " + ", ".join(programs)


# ─── Entity Builder ───────────────────────────────────────────────────────────

def normalize_record(record: dict) -> dict:
    """
    Transform a single OFAC raw record into an FtM-shaped Person entity.

    FtM (FollowTheMoney) schema: https://followthemoney.tech/explorer/
    All property values must be lists, even if there's only one value.
    Custom metadata (sources, ofac_uid) lives outside the properties dict,
    following the same convention as normalize_cia.py.

    NAME HANDLING:
        last_name stored ALL CAPS in OFAC → title-cased with str.title()
        first_name may embed an honorific (e.g. "Dr. Ahmad") → stripped out
        Honorific stored in `title`, role description stored in `position`

    POSITION VS TITLE:
        OFAC's `title` field contains role descriptions ("General",
        "Former President") not honorifics.  Stored in `position` so it
        aligns with CIA data during deduplication.
        Honorifics (Dr., Sheikh) go in `title` where they belong.

    Args:
        record: A single dict from ofac_raw.json.

    Returns:
        FtM Person dict ready to be written to ofac_normalized.json.
    """
    uid       = record["uid"]
    first_raw = record.get("first_name", "") or ""
    last_raw  = record.get("last_name",  "") or ""
    title_raw = record.get("title",      "") or ""
    remarks   = record.get("remarks",    "") or ""

    # --- Name -----------------------------------------------------------------
    # strip_honorific splits "Dr. Ahmad" → ("Dr.", "Ahmad")
    # last_name is ALL CAPS in OFAC — title() normalises it
    embedded_honorific, first_clean = strip_honorific(first_raw)
    first_name = title_case_name(first_clean) if first_clean else None
    last_name  = title_case_name(last_raw)    if last_raw.strip() else None
    full_name  = build_full_name(first_name, last_name)

    # Merge embedded honorific with record-level title field
    # e.g. title="General", first_name="Dr. Ahmad" → title_parts=["Dr.","General"]
    title_parts = []
    if embedded_honorific:
        title_parts.append(embedded_honorific)

    # --- Deterministic ID -----------------------------------------------------
    # OFAC uid is stable across SDN list updates — safe for upserts
    person_id = make_id("ofac", uid)

    # --- Multi-valued fields --------------------------------------------------
    aliases       = extract_aliases(record.get("aliases", []))
    birth_dates   = extract_birth_dates(record.get("dates_of_birth", []))
    nationalities = extract_country_codes(record.get("nationalities", []))
    citizenships  = extract_country_codes(record.get("citizenships", []))

    # --- Identity documents ---------------------------------------------------
    # Returns e.g. {"passportNumber": ["AB123456"], "idNumber": ["12345678"]}
    id_props = extract_ids(record.get("ids", []))

    # --- Notes ----------------------------------------------------------------
    notes = []
    programs_note = build_programs_note(record.get("programs", []))
    if programs_note:
        notes.append(programs_note)
    if remarks.strip():
        notes.append(remarks.strip())

    # --- Assemble FtM Person --------------------------------------------------
    # All property values are lists — empty list means "no value"
    properties = {
        # ── Thing (inherited) ─────────────────────────────────────────────────
        "name":        [full_name] if full_name else [],
        "notes":       notes,
        "sourceUrl":   [OFAC_SOURCE_URL],
        "retrievedAt": [RETRIEVED_AT],

        # ── Person ────────────────────────────────────────────────────────────
        "firstName":  [first_name] if first_name else [],
        "lastName":   [last_name]  if last_name  else [],
        "alias":      aliases,
        "title":      title_parts,
        "position": [title_case_name(title_raw)] if title_raw.strip() else [],
        "topics":     ["sanction"],
        "birthDate":  birth_dates,
        "nationality": nationalities,
        "citizenship": citizenships,
        # ── Identity documents (merged from id_props) ─────────────────────────
        **id_props,
    }

    return {
        "schema":     "Person",
        "id":         person_id,
        "properties": properties,
        # ── Custom metadata (outside FtM properties) ──────────────────────────
        "sources":  ["ofac_sdn"],
        "ofac_uid": uid,   # preserved for cross-referencing ingest/ofac_raw.json
    }


# ─── Run Function ─────────────────────────────────────────────────────────────

def run() -> dict:
    """
    Load ofac_raw.json, normalise every record, write ofac_normalized.json.

    OUTPUT STRUCTURE:
    {
        "meta": {
            "total_persons": 7506,
            "sourceUrl": "https://www.treasury.gov/ofac/downloads/sdn.xml",
            "retrieved_at":  "2026-06-03"
        },
        "persons": [...]
    }

    Returns the output dict so FastAPI pipeline can use data without
    reading from disk (same pattern as normalize_cia.py).
    """
    start = time.time()

    print(f"Loading {OFAC_INPUT_PATH} ...")
    with open(OFAC_INPUT_PATH, "r", encoding="utf-8") as f:
        raw_records = json.load(f)

    print(f"  {len(raw_records)} raw OFAC records loaded\n")

    persons = []
    for record in raw_records:
        person = normalize_record(record)
        persons.append(person)

    elapsed = time.time() - start

    output = {
        "meta": {
            "total_persons": len(persons),
            "retrieved_at":  RETRIEVED_AT,
        },
        "persons": persons,
    }

    os.makedirs(os.path.dirname(OFAC_OUTPUT_PATH), exist_ok=True)
    with open(OFAC_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # ── Summary report ────────────────────────────────────────────────────────
    _print_summary(persons, elapsed)

    # ── Verification checks ───────────────────────────────────────────────────
    _run_verification(persons)

    return output


# ─── Summary Report ───────────────────────────────────────────────────────────

def _print_summary(persons: list[dict], elapsed: float) -> None:
    """Print a quality summary after normalisation."""

    no_first      = sum(1 for p in persons if not p["properties"]["firstName"])
    no_last       = sum(1 for p in persons if not p["properties"]["lastName"])
    no_birth_date = sum(1 for p in persons if not p["properties"]["birthDate"])
    has_alias     = sum(1 for p in persons if p["properties"]["alias"])
    has_passport  = sum(1 for p in persons if p["properties"].get("passportNumber"))
    has_id_number = sum(1 for p in persons if p["properties"].get("idNumber"))
    has_nat       = sum(1 for p in persons if p["properties"]["nationality"])
    has_cit       = sum(1 for p in persons if p["properties"]["citizenship"])

    all_nat: set[str] = set()
    for p in persons:
        all_nat.update(p["properties"]["nationality"])

    print(f"\n{'═' * 50}")
    print(f"  OFAC NORMALISATION REPORT")
    print(f"{'═' * 50}")
    print(f"  Total persons:         {len(persons)}")
    print(f"  Completed in:          {int(elapsed//60)}m {int(elapsed%60)}s")
    print(f"  Retrieved at:          {RETRIEVED_AT}")
    print(f"\n{'─' * 50}")
    print(f"  QUALITY CHECKS")
    print(f"{'─' * 50}")
    print(f"  No first name:         {no_first}")
    print(f"  No last name:          {no_last}")
    print(f"  No birth date:         {no_birth_date}")
    print(f"  Has alias:             {has_alias}")
    print(f"  Has passport number:   {has_passport}")
    print(f"  Has other ID number:   {has_id_number}")
    print(f"  Has nationality:       {has_nat}")
    print(f"  Has citizenship:       {has_cit}")
    print(f"  Unique nationalities:  {len(all_nat)}")
    print(f"{'═' * 50}\n")


# ─── Verification Checks ──────────────────────────────────────────────────────

def _run_verification(persons: list[dict]) -> None:
    """
    Run five spot-checks and print PASS / FAIL for each.

    Deliberately simple — catches regressions quickly without a test framework.
    """
    print("--- Verification checks ---")
    passed = 0
    failed = 0

    # 1. Count sanity — no duplicate UIDs created by the normaliser
    uids = [p["ofac_uid"] for p in persons]
    if len(uids) == len(set(uids)):
        print(f"  [PASS] 1. Count sanity: {len(persons)} persons, all UIDs unique")
        passed += 1
    else:
        dupes = len(uids) - len(set(uids))
        print(f"  [FAIL] 1. Count sanity: {dupes} duplicate UIDs found")
        failed += 1

    # 2. Al-Zawahiri — title-cased, alias present, birth date present
    zawahiri = [
        p for p in persons
        if "zawahiri" in " ".join(p["properties"]["lastName"]).lower()
    ]
    if zawahiri:
        p         = zawahiri[0]
        ln        = p["properties"]["lastName"][0]
        has_alias = bool(p["properties"]["alias"])
        has_dob   = bool(p["properties"]["birthDate"])
        tc_ok     = not ln.isupper()
        ok        = tc_ok and has_alias and has_dob
        print(f"  {'[PASS]' if ok else '[FAIL]'} 2. Al-Zawahiri: "
              f"lastName='{ln}', alias={has_alias}, birthDate={has_dob}")
        passed += (1 if ok else 0)
        failed += (0 if ok else 1)
    else:
        print("  [WARN] 2. Al-Zawahiri: not found (may have been removed from SDN)")

    # 3. Multi-DOB — at least one person has multiple birth dates
    multi_dob = [p for p in persons if len(p["properties"]["birthDate"]) > 1]
    if multi_dob:
        example = multi_dob[0]
        name    = example["properties"]["name"]
        dates   = example["properties"]["birthDate"]
        print(f"  [PASS] 3. Multi-DOB: '{name}' has {len(dates)} dates: {dates}")
        passed += 1
    else:
        print("  [FAIL] 3. Multi-DOB: no person found with multiple birth dates")
        failed += 1

    # 4. No ALL-CAPS last names remain (>2 chars to allow abbreviations)
    all_caps_last = [
        p for p in persons
        if p["properties"]["lastName"]
        and len(p["properties"]["lastName"][0]) > 2
        and p["properties"]["lastName"][0].replace("-", "").replace(" ", "").isupper()
    ]
    if not all_caps_last:
        print(f"  [PASS] 4. No ALL-CAPS last names")
        passed += 1
    else:
        examples = [p["properties"]["lastName"][0] for p in all_caps_last[:3]]
        print(f"  [FAIL] 4. {len(all_caps_last)} ALL-CAPS last names remain, e.g.: {examples}")
        failed += 1

    # 5. Programs appear in notes for at least one person
    programs_in_notes = [
        p for p in persons
        if any("Programs:" in note for note in p["properties"]["notes"])
    ]
    if programs_in_notes:
        example_note = next(
            note for note in programs_in_notes[0]["properties"]["notes"]
            if "Programs:" in note
        )
        print(f"  [PASS] 5. Programs in notes: e.g. '{example_note[:60]}...'")
        passed += 1
    else:
        print("  [FAIL] 5. Programs in notes: no 'Programs:' note found")
        failed += 1

    print(f"\n  {passed} passed, {failed} failed")
    print("---------------------------\n")


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()