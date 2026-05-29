"""
normalize_ofac.py
=================
Transforms ingest/ofac_raw.json into FtM-shaped Person entities and writes
them to transform/ofac_normalized.json.

Unlike the CIA normalizer, OFAC records carry NO position/role data — we
only know that a person is sanctioned.  So this file produces only Person
entities (no Occupancy entities).

Run:
    python transform/normalize_ofac.py

Output:
    transform/ofac_normalized.json   — list of FtM Person dicts

Five verification checks are printed at the end of every run:
    1. Count sanity          — persons == unique UIDs
    2. Al-Zawahiri           — name title-cased, alias present, birth date present
    3. Multi-date person     — person with >1 birth date has all dates
    4. No ALL-CAPS last name — .title() was applied everywhere
    5. Programs in notes     — at least one person has a "Programs:" note
"""

import json
import os
from datetime import date

# ---------------------------------------------------------------------------
# helpers.py lives in the same transform/ directory.  We import the three
# functions we need:
#   make_id(source, *parts)  — UUID5 deterministic ID
#   normalize_date(raw)      — handles all OFAC date formats → ISO or None
#   to_iso_country(name)     — country name → ISO alpha-2 code (e.g. "eg")
# ---------------------------------------------------------------------------
from helpers import make_id, normalize_date, to_iso_country

# ---------------------------------------------------------------------------
# Paths 
# ---------------------------------------------------------------------------
OFAC_INPUT_PATH  = "ingest/ofac_raw.json"
OFAC_OUTPUT_PATH = "transform/ofac_normalized.json"


# Today's date, recorded once per pipeline run so every entity gets the same
# retrievedAt value.
TODAY = date.today().isoformat()   # e.g. "2026-05-29"

# ---------------------------------------------------------------------------
# ID type → FtM property name mapping
#
# Built from the actual ID type strings present in ofac_raw.json.
# Two FtM properties are used:
#   passportNumber — travel documents (passports, diplomatic passports)
#   idNumber       — everything else: national IDs, tax IDs, licences, etc.
#
# Crypto wallet addresses get their own "cryptoWalletAddress" property so
# they're searchable separately from document numbers.
# ---------------------------------------------------------------------------
ID_TYPE_MAP = {
    # --- Passports ---
    "Passport":                     "passportNumber",
    "Diplomatic Passport":          "passportNumber",
    "Stateless Person Passport":    "passportNumber",
    "British National Overseas Passport": "passportNumber",

    # --- National / civil identity documents ---
    "National ID No.":              "idNumber",
    "National Foreign ID Number":   "idNumber",
    "Identification Number":        "idNumber",
    "Personal ID Card":             "idNumber",
    "Tazkira National ID Card":     "idNumber",
    "Refugee ID Card":              "idNumber",
    "Stateless Person ID Card":     "idNumber",
    "Federal ID Card":              "idNumber",
    "UAE Identification":           "idNumber",
    "Tarjeta de Identidad":         "idNumber",   # Colombian ID card
    "D.N.I.":                       "idNumber",   # Documento Nacional de Identidad (Spain/Argentina)
    "N.I.E.":                       "idNumber",   # Número de Identidad de Extranjero (Spain)
    "Numero de Identidad":          "idNumber",   # Honduras
    "Kenyan ID No.":                "idNumber",
    "Moroccan Personal ID No.":     "idNumber",
    "Bosnian Personal ID No.":      "idNumber",
    "CNP (Personal Numerical Code)":"idNumber",   # Romania
    "Romanian Permanent Resident":  "idNumber",
    "Turkish Identification Number":"idNumber",
    "C.U.I.":                       "idNumber",   # Guatemalan ID
    "C.U.I.P.":                     "idNumber",   # Guatemalan passport ID
    "Citizen's Card Number":        "idNumber",   # China
    "Chinese Commercial Code":      "idNumber",
    "Italian Fiscal Code":          "idNumber",

    # --- Tax / fiscal IDs ---
    "Tax ID No.":                   "idNumber",
    "C.U.R.P.":                     "idNumber",   # Mexico — Clave Única de Registro de Población
    "R.F.C.":                       "idNumber",   # Mexico — Registro Federal de Contribuyentes
    "RFC":                          "idNumber",   # alternate label for the same thing
    "C.U.I.T.":                     "idNumber",   # Argentina — tax ID
    "NIT #":                        "idNumber",   # Colombia — Número de Identificación Tributaria
    "RUC #":                        "idNumber",   # Peru/Ecuador — tax ID
    "SSN":                          "idNumber",   # US Social Security Number
    "Russian State Individual Business Registration Number Pattern (OGRNIP)": "idNumber",

    # --- Electoral / civil registry ---
    "Electoral Registry No.":       "idNumber",
    "Credencial electoral":         "idNumber",   # Mexico
    "I.F.E.":                       "idNumber",   # Mexico — Instituto Federal Electoral
    "Cedula No.":                   "idNumber",   # Colombia

    # --- Travel / residency documents ---
    "Travel Document Number":       "idNumber",
    "Residency Number":             "idNumber",
    "Immigration No.":              "idNumber",
    "VisaNumberID":                 "idNumber",
    "LE Number":                    "idNumber",   # Law Enforcement credential

    # --- Professional / institutional ---
    "Driver's License No.":         "idNumber",
    "Pilot License Number":         "idNumber",
    "Birth Certificate Number":     "idNumber",
    "Military Registration Number": "idNumber",
    "Cartilla de Servicio Militar Nacional": "idNumber",   # Mexico military booklet
    "Matricula Mercantil No":       "idNumber",   # Colombia business registry
    "Registration ID":              "idNumber",
    "Registration Number":          "idNumber",
    "Government Gazette Number":    "idNumber",
    "Serial No.":                   "idNumber",
    "License":                      "idNumber",
    "Public Security and Immigration No.": "idNumber",
    "Seafarer's Identification Document":  "idNumber",

    # --- Crypto wallet addresses — separate property for searchability ---
    "Digital Currency Address - XBT":  "cryptoWalletAddress",   # Bitcoin
    "Digital Currency Address - ETH":  "cryptoWalletAddress",   # Ethereum
    "Digital Currency Address - TRX":  "cryptoWalletAddress",   # Tron
    "Digital Currency Address - USDT": "cryptoWalletAddress",   # Tether
    "Digital Currency Address - LTC":  "cryptoWalletAddress",   # Litecoin
    "Digital Currency Address - XMR":  "cryptoWalletAddress",   # Monero
    "Digital Currency Address - BCH":  "cryptoWalletAddress",   # Bitcoin Cash
    "Digital Currency Address - ZEC":  "cryptoWalletAddress",   # Zcash
    "Digital Currency Address - DASH": "cryptoWalletAddress",   # Dash
    "Digital Currency Address - BTG":  "cryptoWalletAddress",   # Bitcoin Gold
    "Digital Currency Address - ETC":  "cryptoWalletAddress",   # Ethereum Classic
    "Digital Currency Address - BSV":  "cryptoWalletAddress",   # Bitcoin SV
    "Digital Currency Address - XVG":  "cryptoWalletAddress",   # Verge
    "Digital Currency Address - ARB":  "cryptoWalletAddress",   # Arbitrum
    "Digital Currency Address - BSC":  "cryptoWalletAddress",   # Binance Smart Chain
    "Digital Currency Address - USDC": "cryptoWalletAddress",   # USD Coin
    "Digital Currency Address - SOL":  "cryptoWalletAddress",   # Solana
}

# Labels whose values we silently discard — they are attributes or legal
# boilerplate stored as "id" entries in the OFAC XML, not document numbers.
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


# ===========================================================================
# Name helpers
# ===========================================================================

def titlecase_name(raw: str) -> str:
    """
    Convert an ALL-CAPS OFAC name to title case.

    OFAC stores last names in ALL CAPS (e.g. "AL ZAWAHIRI", "PUTIN").
    Python's str.title() handles the basic case but stumbles on:
      - Hyphenated particles: "AL-ZAWAHIRI" → we want "Al-Zawahiri"
      - Short Arabic particles that should stay lower: handled by leaving
        str.title() output as-is (al- prefix is kept by OFAC on the word)

    We use str.title() which correctly title-cases each word and each
    hyphen-separated segment ("AL-ZAWAHIRI" → "Al-Zawahiri").

    Args:
        raw: A string in ALL CAPS or mixed case.

    Returns:
        Title-cased version of the string.
    """
    if not raw:
        return raw
    return raw.title()


# Honorific prefixes that sometimes appear embedded in OFAC first_name fields.
# We strip these out so they don't pollute firstName and store them in `title`
# alongside any role title the record already has.
HONORIFIC_PREFIXES = {
    "Dr.", "Dr",
    "Prof.", "Prof",
    "Eng.", "Eng",
    "Rev.", "Rev",
    "Sheikh", "Shaikh",
    "Haji", "Hajj",
    "Mr.", "Mr", "Mrs.", "Mrs", "Ms.", "Ms",
}


def strip_honorific(raw_first: str) -> tuple[str | None, str | None]:
    """
    Separate an embedded honorific from a first name string.

    OFAC occasionally stores entries like first_name="Dr. Ahmad" or even
    first_name="Dr." with no actual given name.  We split those apart so
    the honorific ends up in the `title` field and firstName stays clean.

    Args:
        raw_first: The raw first_name string from an OFAC record.

    Returns:
        A tuple of (honorific_or_None, cleaned_first_or_None).

    Examples:
        "Dr. Ahmad"  → ("Dr.", "Ahmad")
        "Dr."        → ("Dr.", None)
        "Ahmad"      → (None,  "Ahmad")
    """
    if not raw_first or not raw_first.strip():
        return None, None

    parts = raw_first.strip().split(None, 1)   # split on first whitespace only
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

    FtM's `name` property is the full human-readable name.  We build it by
    joining whichever parts are present.

    Args:
        first_name: Given name, already title-cased.
        last_name:  Family name, already title-cased.

    Returns:
        "First Last", "Last" (if no first), "First" (if no last), or None.
    """
    parts = [p for p in [first_name, last_name] if p]
    return " ".join(parts) if parts else None


# ===========================================================================
# Alias helpers
# ===========================================================================

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
        first = titlecase_name(alias.get("first_name", "") or "")
        last  = titlecase_name(alias.get("last_name",  "") or "")
        full  = build_full_name(first or None, last or None)
        if full:
            result.append(full)
    return result


# ===========================================================================
# Date helpers
# ===========================================================================

def extract_birth_dates(dates_of_birth: list[dict]) -> list[str]:
    """
    Normalise all OFAC birth date entries using helpers.normalize_date().

    OFAC stores birth dates in several formats:
        "03 May 1938"          → "1938-05-03"
        "1938"                 → "1938"   (year-only, preserved as-is)
        "circa 1940"           → "1940"   (circa stripped, year preserved)
        "1938 to 1940"         → "1938"   (range, first year taken)

    normalize_date() in helpers.py handles all of these already.

    Args:
        dates_of_birth: List of dicts, each with at least a "date" key.

    Returns:
        List of ISO date strings or year strings.  Entries that fail to
        parse are silently skipped (normalize_date returns None for those).
    """
    result = []
    for entry in dates_of_birth:
        raw = entry.get("date", "")
        normalised = normalize_date(raw)
        if normalised:
            result.append(normalised)
    return result


# ===========================================================================
# Country / nationality helpers
# ===========================================================================

def extract_nationalities(nationalities: list[str]) -> list[str]:
    """
    Convert OFAC nationality strings to ISO alpha-2 country codes.

    to_iso_country() in helpers.py uses pycountry with overrides for
    ambiguous names (e.g. "Iran" → "ir", "Turkey" → "tr").

    Entries that can't be resolved are skipped with a warning so we don't
    silently lose data.

    Args:
        nationalities: List of country name strings from OFAC raw record.

    Returns:
        List of ISO alpha-2 codes (e.g. ["eg", "sa"]).
    """
    result = []
    for country_name in nationalities:
        code = to_iso_country(country_name)
        if code:
            result.append(code)
        else:
            print(f"  [WARN] Could not resolve nationality: '{country_name}'")
    return result


# ===========================================================================
# Identity document helpers
# ===========================================================================

def extract_ids(ids: list[dict]) -> dict[str, list[str]]:
    """
    Map OFAC identity document entries to FtM property names.

    Returns a dict keyed by FtM property name, e.g.:
        {
            "passportNumber": ["AB123456", "CD789012"],
            "idNumber":       ["12345678"]
        }

    ID types not in ID_TYPE_MAP and not in ID_SKIP_LABELS trigger a warning
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
            continue  # these aren't document numbers, skip silently

        ftm_prop = ID_TYPE_MAP.get(id_type)
        if ftm_prop is None:
            # Unknown type — warn so we can update the map if needed
            print(f"  [WARN] Unknown ID type: '{id_type}' — skipping")
            continue

        result.setdefault(ftm_prop, []).append(id_number)

    return result


# ===========================================================================
# Programs → notes
# ===========================================================================

def build_programs_note(programs: list[str]) -> str | None:
    """
    Format OFAC sanctions programs as a human-readable note string.

    Programs are short codes like "SDGT", "RUSSIA-EO14024".  We store them
    in the FtM notes field so end users can see which list(s) the person
    appears on without needing to decode the raw codes separately.

    Returns None if the programs list is empty.

    Args:
        programs: List of program code strings.

    Returns:
        A string like "Programs: SDGT, RUSSIA-EO14024", or None.
    """
    if not programs:
        return None
    return "Programs: " + ", ".join(programs)


# ===========================================================================
# Core transform: one raw OFAC record → one FtM Person dict
# ===========================================================================

def normalize_record(record: dict) -> dict:
    """
    Transform a single OFAC raw record into an FtM-shaped Person entity.

    FtM (FollowTheMoney) schema: https://followthemoney.tech/explorer/
    All property values must be lists, even if there's only one value.
    Custom metadata (sources, country codes) lives outside the properties
    dict, following the same convention as normalize_cia.py.

    Args:
        record: A single dict from ofac_raw.json.

    Returns:
        An FtM Person dict ready to be written to ofac_normalized.json.
    """
    uid        = record["uid"]
    first_raw  = record.get("first_name", "") or ""
    last_raw   = record.get("last_name",  "") or ""
    title_raw  = record.get("title",      "") or ""
    remarks    = record.get("remarks",    "") or ""

    # --- Name -----------------------------------------------------------------
    # OFAC stores last names in ALL CAPS.  title() normalises to Title Case.
    # first_name sometimes contains an embedded honorific (e.g. "Dr. Ahmad") —
    # strip_honorific separates those so firstName stays clean.
    embedded_honorific, first_clean = strip_honorific(first_raw)
    first_name = titlecase_name(first_clean) if first_clean else None
    last_name  = titlecase_name(last_raw)    if last_raw.strip() else None
    full_name  = build_full_name(first_name, last_name)

    # Merge embedded honorific with the record-level title field.
    # e.g. record has title="General" and first_name="Dr. Ahmad"
    # → title_parts = ["Dr.", "General"]
    title_parts = []
    if embedded_honorific:
        title_parts.append(embedded_honorific)
    if title_raw.strip():
        title_parts.append(title_raw.strip())

    # --- Deterministic ID -----------------------------------------------------
    # We use the OFAC uid as the stable identifier.  This means re-running the
    # pipeline will produce the same ID for the same person — safe for upserts.
    person_id = make_id("ofac", uid)

    # --- Multi-valued fields --------------------------------------------------
    aliases      = extract_aliases(record.get("aliases", []))
    birth_dates  = extract_birth_dates(record.get("dates_of_birth", []))
    nationalities = extract_nationalities(
        record.get("nationalities", []) + record.get("citizenships", [])
    )

    # --- Identity documents ---------------------------------------------------
    # Returns e.g. {"passportNumber": ["AB123456"], "idNumber": ["12345678"]}
    id_props = extract_ids(record.get("ids", []))

    # --- Notes ----------------------------------------------------------------
    # Combine the sanctions programs note with any remarks field
    notes = []
    programs_note = build_programs_note(record.get("programs", []))
    if programs_note:
        notes.append(programs_note)
    if remarks.strip():
        notes.append(remarks.strip())

    # --- Assemble FtM Person --------------------------------------------------
    # Properties dict: every value is a list, even scalars.
    # Empty lists are fine — FtM consumers treat them as "no value".
    properties = {
        # Core name fields
        "name":       [full_name] if full_name else [],
        "firstName":  [first_name] if first_name else [],
        "lastName":   [last_name]  if last_name  else [],

        # Aliases (a.k.a. entries from OFAC)
        "alias": aliases,

        # OFAC title strings describe roles loosely (e.g. "Operational and
        # Military Leader", "Former President").  You raised a fair point:
        # these are closer to positions than to honorifics.  We store them
        # in `position` so they align with CIA data during deduplication —
        # both sources end up with position values that can be compared.
        # Honorifics (Dr., Prof., Sheikh) stay in `title` where they belong.
        "title":    title_parts,
        "position": [title_raw.strip()] if title_raw.strip() else [],

        # FtM topic tag — all OFAC persons are sanctioned individuals
        "topics": ["sanction"],

        # Birth dates (may be multiple — OFAC tracks uncertainty with ranges)
        "birthDate": birth_dates,

        # Nationality / citizenship (ISO alpha-2 codes)
        "nationality": nationalities,

        # Identity documents — merged in from id_props dict
        **id_props,

        # Notes: programs + remarks
        "notes": notes,

        # When this record was retrieved by our pipeline
        "retrievedAt": [TODAY],
    }

    return {
        "schema":     "Person",
        "id":         person_id,
        "properties": properties,
        # Custom metadata outside properties (same pattern as CIA normalizer)
        "sources":    ["ofac_sdn"],
        "ofac_uid":   uid,   # preserved so we can cross-reference the raw file
    }


# ===========================================================================
# Batch runner
# ===========================================================================

def run() -> list[dict]:
    """
    Load ofac_raw.json, normalise every record, write ofac_normalized.json.

    Returns the list of normalised Person dicts (useful for testing).
    """
    print(f"Loading {OFAC_INPUT_PATH} ...")
    with open(OFAC_INPUT_PATH, "r", encoding="utf-8") as f:
        raw_records = json.load(f)

    print(f"  {len(raw_records)} raw OFAC records loaded")

    persons = []
    for record in raw_records:
        person = normalize_record(record)
        persons.append(person)

    # Write output
    os.makedirs(os.path.dirname(OFAC_OUTPUT_PATH), exist_ok=True)
    with open(OFAC_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(persons, f, ensure_ascii=False, indent=2)

    print(f"\nWrote {len(persons)} persons to {OFAC_OUTPUT_PATH}")

    # -----------------------------------------------------------------------
    # Summary report
    # -----------------------------------------------------------------------
    _print_summary(raw_records, persons)

    # -----------------------------------------------------------------------
    # Verification checks
    # -----------------------------------------------------------------------
    _run_verification(persons)

    return persons


# ===========================================================================
# Summary report
# ===========================================================================

def _print_summary(raw_records: list[dict], persons: list[dict]) -> None:
    """Print a quality summary after normalisation."""

    # Count how many persons have each field populated
    no_first      = sum(1 for p in persons if not p["properties"]["firstName"])
    no_last       = sum(1 for p in persons if not p["properties"]["lastName"])
    no_birth_date = sum(1 for p in persons if not p["properties"]["birthDate"])
    has_alias     = sum(1 for p in persons if p["properties"]["alias"])
    has_passport  = sum(1 for p in persons if p["properties"].get("passportNumber"))
    has_id_number = sum(1 for p in persons if p["properties"].get("idNumber"))
    has_nat       = sum(1 for p in persons if p["properties"]["nationality"])

    # Count unique nationalities resolved
    all_nat = set()
    for p in persons:
        all_nat.update(p["properties"]["nationality"])

    print("\n--- OFAC Normalisation Summary ---")
    print(f"  Total persons:         {len(persons)}")
    print(f"  No first name:         {no_first}")
    print(f"  No last name:          {no_last}")
    print(f"  No birth date:         {no_birth_date}")
    print(f"  Has alias:             {has_alias}")
    print(f"  Has passport number:   {has_passport}")
    print(f"  Has other ID number:   {has_id_number}")
    print(f"  Has nationality:       {has_nat}")
    print(f"  Unique nationalities:  {len(all_nat)}")
    print("----------------------------------\n")


# ===========================================================================
# Verification checks
# ===========================================================================

def _run_verification(persons: list[dict]) -> None:
    """
    Run five spot-checks and print PASS / FAIL for each.

    These checks are deliberately simple — they catch regressions quickly
    without needing a test framework.
    """
    print("--- Verification checks ---")
    passed = 0
    failed = 0

    # ------------------------------------------------------------------
    # Check 1: Count sanity — number of persons equals number of unique
    # ofac_uid values (no duplicates created by the normaliser).
    # ------------------------------------------------------------------
    uids = [p["ofac_uid"] for p in persons]
    if len(uids) == len(set(uids)):
        print(f"  [PASS] 1. Count sanity: {len(persons)} persons, all UIDs unique")
        passed += 1
    else:
        dupes = len(uids) - len(set(uids))
        print(f"  [FAIL] 1. Count sanity: {dupes} duplicate UIDs found")
        failed += 1

    # ------------------------------------------------------------------
    # Check 2: Al-Zawahiri — find by last name, confirm title-case,
    # alias present, birth date present.
    # ------------------------------------------------------------------
    zawahiri = [
        p for p in persons
        if "zawahiri" in " ".join(p["properties"]["lastName"]).lower()
    ]
    if zawahiri:
        p = zawahiri[0]
        ln    = p["properties"]["lastName"][0]
        has_alias = bool(p["properties"]["alias"])
        has_dob   = bool(p["properties"]["birthDate"])
        if ln[0].isupper() and ln[1:].islower() or "-" in ln:
            tc_ok = True
        else:
            # Title case check: first char upper, not all-caps
            tc_ok = not ln.isupper()
        ok = tc_ok and has_alias and has_dob
        print(f"  {'[PASS]' if ok else '[FAIL]'} 2. Al-Zawahiri: "
              f"lastName='{ln}', alias={has_alias}, birthDate={has_dob}")
        passed += (1 if ok else 0)
        failed += (0 if ok else 1)
    else:
        print("  [WARN] 2. Al-Zawahiri: record not found (may have been removed from SDN)")
        # Not a hard failure — OFAC list changes over time

    # ------------------------------------------------------------------
    # Check 3: At least one person has multiple birth dates.
    # (OFAC records with uncertain DOBs often list several candidates.)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Check 4: No ALL-CAPS last names remain in the output.
    # A last name is considered still-broken if it's all-caps and longer
    # than 2 chars (to allow legit 2-letter abbreviations like "OH").
    # ------------------------------------------------------------------
    all_caps_last = [
        p for p in persons
        if p["properties"]["lastName"]
        and p["properties"]["lastName"][0].isupper()
        and p["properties"]["lastName"][0] == p["properties"]["lastName"][0].upper()
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

    # ------------------------------------------------------------------
    # Check 5: Programs appear in notes for at least one person.
    # ------------------------------------------------------------------
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


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    run()