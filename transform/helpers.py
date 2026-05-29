"""
transform/helpers.py

Shared utilities for normalize_cia.py and normalize_ofac.py.
Neither normalizer should define its own constants or helper functions —
everything shared lives here to avoid duplication.

Contains:
    - ABBREVIATIONS: CIA title abbreviation map
    - COUNTRY_OVERRIDES: manual overrides for ambiguous country names
    - ROMANCE_COUNTRIES: ISO codes for Spanish/Portuguese speaking countries
    - EAST_ASIAN_COUNTRIES: ISO codes for East Asian name-order countries
    - make_id(): deterministic UUID5 entity ID generation
    - to_iso_country(): country name to ISO alpha-2 code
    - expand_abbreviations(): expand CIA title abbreviations
    - normalize_date(): normalize various date formats to YYYY-MM-DD
    - split_cia_name(): split CIA name string into components
    - verify_country_mappings(): test country mappings against known strings
"""

import re
import uuid
import pycountry
import dateparser
from datetime import datetime
from nameparser import HumanName

# ─── Debug Flag ───────────────────────────────────────────────────────────────
# Set True during development to see verbose per-record output.
# Set False for normal runs — only warnings and errors print.

DEBUG = False


# ─── Abbreviation Map ─────────────────────────────────────────────────────────
# Built directly from profiler output — all 18 abbreviations found in CIA data.
# Intentionally hardcoded because these are CIA-specific abbreviations,
# not standard English. There is no library for this.
# "U.", "S.", "Inc." appear once each — too ambiguous to expand safely.

ABBREVIATIONS = {
    "Min.":   "Minister",
    "Pres.":  "President",
    "Dep.":   "Deputy",
    "Sec.":   "Secretary",
    "Gen.":   "General",
    "Chmn.":  "Chairman",
    "Govt.":  "Government",
    "Del.":   "Delegate",
    "Admin.": "Administrator",
    "Intl.":  "International",
    "Dir.":   "Director",
    "Dept.":  "Department",
    "Ctte.":  "Committee",
    "Fed.":   "Federal",
    "Gov.":   "Governor",
}


# ─── Country Overrides ────────────────────────────────────────────────────────
# Manual overrides for names pycountry handles incorrectly or cannot find.
# Only non-standard cases — pycountry handles the other ~190 automatically.

COUNTRY_OVERRIDES = {
    "Korea, North":                      "kp",
    "Korea, South":                      "kr",
    "Burma":                             "mm",
    "Bahamas, The":                      "bs",
    "Gambia, The":                       "gm",
    "Congo, Democratic Republic of the": "cd",
    "Congo, Republic of the":            "cg",
    "Micronesia, Federated States of":   "fm",
    "Holy See":                          "va",
    "Taiwan":                            "tw",
    "Czechia":                           "cz",
    "Laos":                              "la",
    "Vietnam":                           "vn",
    "Syria":                             "sy",
    "Iran":                              "ir",
    "Russia":                            "ru",
    "Venezuela":                         "ve",
    "Bolivia":                           "bo",
    "Moldova":                           "md",
    "Turkey":                            "tr",
    "Turkiye":                           "tr",
    "Palestinian":                       "ps",   # ISO 3166-1 alpha-2 for Palestine
    "Region: Gaza":                      "ps",   # OFAC address region stored as nationality
    "North Macedonia, The Republic of":  "mk",
}   


# ─── Country Set Constants ────────────────────────────────────────────────────
# Used by split_cia_name() to detect naming conventions by country.
# ISO alpha-2 codes (lowercase).

# Spanish and Portuguese speaking countries — double surname convention
# Both last names stored combined e.g. "Sheinbaum Pardo"
# Portuguese (Brazil, Angola, Mozambique etc.) follow same pattern
ROMANCE_COUNTRIES = {
    "mx", "ve", "co", "ni", "cu", "gt", "sv", "do", "cr", "hn",
    "ar", "ec", "bo", "py", "uy", "pe", "cl", "pa", "pr",
    "br", "ao", "mz", "pt", "gq",
}

# East Asian countries — family name comes FIRST in CIA data (written in CAPS)
# e.g. "KIM Jong-un" → family: Kim, given: Jong-un
EAST_ASIAN_COUNTRIES = {
    "cn", "kp", "kr", "tw", "jp", "vn", "sg", "mn",
}


# ─── UUID Namespace ───────────────────────────────────────────────────────────
# Fixed private namespace for UUID5.
# UUID5 is deterministic — same inputs always produce the same UUID.
# Private namespace prevents collisions with other systems.

_UUID_NAMESPACE = uuid.UUID("b7c9a2e1-4f3d-5a8b-9c0e-1d2f3a4b5c6d")


# ─── Compiled Regex Patterns ──────────────────────────────────────────────────
# Defined at module level so they compile once, not on every function call.

# Roman numerals I through XVI — used to detect monarch regnal names
_ROMAN_RE = re.compile(
    r'\b(I{1,3}|IV|VI{0,3}|IX|XI{0,3}|XIV|XV|XVI)\s*$'
)

# Arabic particles that signal a family name follows
# Covers fused form (al-ALIMI) and separate form (bin IBRAHIM)
_ARABIC_PARTICLE_RE = re.compile(
    r'\b(al|el|bin|bint|abu|ibn)[\s\-]',
    re.IGNORECASE
)

# Inline alias in name field e.g. "Lai Ching-te (a.k.a. William Lai)"
_AKA_RE = re.compile(r'\(a\.k\.a\.?\s+([^)]+)\)', re.IGNORECASE)

# Honorific prefixes sometimes embedded in name field instead of honorific field
_HONORIFIC_PREFIXES_RE = re.compile(
    r'^(Dr\.?|Prof\.?|Eng\.?|Rev\.?|Sir|Dame|Sheikh|Sheik|Shaikh|Haji|Hajj)\s+',
    re.IGNORECASE
)


# ─── Helper: detect naming strategy ──────────────────────────────────────────

def _is_east_asian_name(tokens, country_code):
    """
    Returns True if name follows East Asian convention:
    CAPS word is the FIRST token (family name first).

    Trigger: first token is ALL CAPS + country is in EAST_ASIAN_COUNTRIES.

    Example:
        tokens=["KIM", "Jong-un"], country_code="kp" → True
        tokens=["Ferdinand", "MARCOS"], country_code="ph" → False
    """
    if country_code not in EAST_ASIAN_COUNTRIES:
        return False
    if not tokens:
        return False
    first_clean = re.sub(r'[^A-Za-z]', '', tokens[0])
    return first_clean.isupper() and len(first_clean) > 1


def _is_romance_name(tokens, country_code):
    """
    Returns True if name follows Spanish/Portuguese double surname convention:
    CAPS word appears in the MIDDLE (not at end) of the name.

    Trigger: CAPS word is not the last token + country is in ROMANCE_COUNTRIES.

    Example:
        tokens=["Claudia", "SHEINBAUM", "Pardo"], country_code="mx" → True
        tokens=["Vladimir", "PUTIN"], country_code="ru" → False

    WHY check CAPS not at end:
    Standard CIA names have CAPS at the end e.g. "Ferdinand MARCOS".
    Spanish CIA names have CAPS in the middle e.g. "Claudia SHEINBAUM Pardo".
    The position of CAPS distinguishes them.
    """
    if country_code not in ROMANCE_COUNTRIES:
        return False
    caps_indices = [
        i for i, t in enumerate(tokens)
        if re.sub(r'[^A-Za-z]', '', t).isupper()
        and len(re.sub(r'[^A-Za-z]', '', t)) > 1
    ]
    # CAPS word must exist and must NOT be the last token
    return bool(caps_indices) and caps_indices[-1] < len(tokens) - 1


# ─── Main Functions ───────────────────────────────────────────────────────────

def make_id(source, *parts):
    """
    Creates a deterministic UUID5 entity ID from source + identifying parts.

    WHY UUID5:
    Standard deterministic UUID format. Same inputs always produce same ID.
    Databases and APIs understand UUID format natively.
    Prevents duplicate records when pipeline runs multiple times.

    Args:
        source: data source e.g. "cia" or "ofac"
        *parts: identifying strings e.g. country, name, title

    Returns:
        UUID5 string e.g. "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

    Example:
        make_id("cia", "Philippines", "Ferdinand MARCOS, Jr.")
    """
    raw = ":".join([source] + [str(p).lower().strip() for p in parts])
    result = str(uuid.uuid5(_UUID_NAMESPACE, raw))
    if DEBUG:
        print(f"  [DEBUG] make_id({source!r}, {parts}) → {result}")
    return result


def to_iso_country(name):
    """
    Converts country name string to ISO alpha-2 code (lowercase).

    Two-step lookup:
    1. Check COUNTRY_OVERRIDES for known problem cases
    2. Fall back to pycountry fuzzy search for everything else

    Returns lowercased original if nothing matches — data never lost.
    Prints [WARN] so you know what needs adding to COUNTRY_OVERRIDES.

    Args:
        name: country name string e.g. "Russia", "Korea, North"

    Returns:
        ISO alpha-2 code e.g. "ru", "kp"
        or lowercased original if not found
    """
    if not name:
        return ""
    name = name.strip()

    if name in COUNTRY_OVERRIDES:
        result = COUNTRY_OVERRIDES[name]
        if DEBUG:
            print(f"  [DEBUG] to_iso_country: '{name}' → '{result}' (override)")
        return result

    try:
        results = pycountry.countries.search_fuzzy(name)
        result = results[0].alpha_2.lower()
        if DEBUG:
            print(f"  [DEBUG] to_iso_country: '{name}' → '{result}' (pycountry)")
        return result
    except LookupError:
        print(f"  [WARN] Country not mapped: '{name}' — storing as-is")
        return name.lower()


def expand_abbreviations(title):
    """
    Expands CIA title abbreviations to full forms.
    Uses word boundary matching to avoid partial replacements.
    Logs [WARN] for any abbreviation-like token not in our map —
    helps catch new abbreviations if CIA updates their format.

    Args:
        title: raw CIA title e.g. "Dep. Min. of Finance"

    Returns:
        expanded title e.g. "Deputy Minister of Finance"
    """
    if not title:
        return title

    original = title

    found_abbrevs = re.findall(r'\b[A-Za-z]+\.', title)
    for abbrev in found_abbrevs:
        if abbrev not in ABBREVIATIONS:
            print(f"  [WARN] Unknown abbreviation '{abbrev}' in: '{title}'")

    for abbrev, expanded in ABBREVIATIONS.items():
        title = re.sub(r'\b' + re.escape(abbrev), expanded, title)

    if DEBUG and title != original:
        print(f"  [DEBUG] expand_abbreviations: '{original}' → '{title}'")

    return title


def normalize_date(raw):
    """
    Normalizes OFAC date formats to YYYY-MM-DD where possible.

    WHY DATEPARSER + MANUAL PREPROCESSING:
    dateparser handles most formats automatically but parses year-only
    "1938" as "1938-01-01" implying false precision. We intercept
    year-only and month-year before dateparser sees them.

    Handles all formats from profiler:
        "1952-10-07"                 → "1952-10-07"  already ISO
        "03 May 1938"                → "1938-05-03"
        "1938"                       → "1938"         year only preserved
        "May 1938"                   → "1938-05"
        "1970 to 1972"               → "1970"         range, take start
        "01 Jan 1970 to 31 Dec 1970" → "1970-01-01"
        "circa 07 Jul 1966"          → "1966-07-07"

    Args:
        raw: raw date string from OFAC data

    Returns:
        normalized date string or original if unparseable
    """
    if not raw:
        return ""

    raw = raw.strip()
    original = raw

    if re.match(r'^\d{4}-\d{2}-\d{2}$', raw):
        return raw

    if re.match(r'^\d{4}$', raw):
        return raw

    raw = re.sub(r'^circa\s+', '', raw, flags=re.IGNORECASE)

    if " to " in raw:
        raw = raw.split(" to ")[0].strip()
        if DEBUG:
            print(f"  [DEBUG] normalize_date: range → using start: '{raw}'")

    if re.match(r'^\d{4}$', raw):
        return raw

    try:
        result = datetime.strptime(raw, "%b %Y").strftime("%Y-%m")
        if DEBUG:
            print(f"  [DEBUG] normalize_date: '{original}' → '{result}' (Mon YYYY)")
        return result
    except ValueError:
        pass

    result = dateparser.parse(
        raw,
            settings={"PREFER_DATES_FROM": "past", "RETURN_AS_TIMEZONE_AWARE": False}
    )
    if result:
        normalized = result.strftime("%Y-%m-%d")
        if DEBUG:
            print(f"  [DEBUG] normalize_date: '{original}' → '{normalized}' (dateparser)")
        return normalized

    print(f"  [WARN] Could not normalize date: '{raw}'")
    return original


def split_cia_name(raw_name, country_code=""):
    """
    Splits a CIA leader name string into structured components.

    CIA NAMING CONVENTION:
    Last names written in ALL CAPS, first names in Title Case.
    e.g. "Ferdinand MARCOS, Jr." → MARCOS is last, Ferdinand is first.

    Requires country_code to detect naming conventions that differ
    by region (East Asian, Spanish/Portuguese, Burmese mononyms).

    STRATEGIES (applied in order, first match wins):
        1. Guard       — VACANT or empty → return all None
        2. Preprocessing — strip a.k.a. alias, strip embedded honorific
        3. East Asian  — CAPS first token + EA country → swap order
        4. Burma       — country=mm + all CAPS tokens → mononym
        5. Monarch     — all CAPS tokens + Roman numeral → regnal name
        6. Arabic      — particle (al-, bin, bint) present → extract last CAPS
        7. Romance     — CAPS in middle + Romance country → compound last name
        8. nameparser  — default fallback for all other names

    Args:
        raw_name: raw CIA name string e.g. "Ferdinand MARCOS, Jr."
        country_code: ISO alpha-2 code e.g. "ph" — used for strategy detection

    Returns:
        dict with keys:
            first_name         (str|None): given name(s) including middle
            last_name          (str|None): family name, no suffix
            suffix             (str|None): e.g. "Jr.", "Sr.", "III"
            alias              (str|None): from "(a.k.a. ...)" if present
            embedded_honorific (str|None): honorific stripped from name field
        Any key can be None. Returns all-None dict for VACANT/empty.

    KNOWN EDGE CASES (documented for responses.md):
        1. "TARIQ Muhammad Abdallah Salih" — TARIQ treated as last name
           (ALL CAPS = last name rule). CIA inconsistency, unfixable without
           a manual lookup table.
        2. Mononyms (Burma) — last_name only, first_name=None.
        3. "CHARLES III" — last_name="Charles III", first_name=None.
           III is regnal numeral, belongs to whole name not family name.
        4. "al-ALIMI" — particle stays attached: last_name="al-Alimi".
        5. Spanish names — both surnames combined: last_name="Sheinbaum Pardo".
    """
    # Null result for early returns
    null_result = {
        "first_name": None,
        "last_name": None,
        "suffix": None,
        "alias": None,
        "embedded_honorific": None,
    }

    # ── Strategy 1: Guard ─────────────────────────────────────────────────────
    if not raw_name or "VACANT" in raw_name.upper():
        if DEBUG:
            print(f"  [DEBUG] split_cia_name: skipped VACANT/empty '{raw_name}'")
        return null_result

    # ── Strategy 2a: Extract inline alias ────────────────────────────────────
    # CIA sometimes includes alias inline: "Lai Ching-te (a.k.a. William Lai)"
    # Extract and remove before any other processing
    alias = None
    aka_match = _AKA_RE.search(raw_name)
    if aka_match:
        alias = aka_match.group(1).strip()
        raw_name = _AKA_RE.sub("", raw_name).strip()
        raw_name = re.sub(r'\s{2,}', ' ', raw_name)
        if DEBUG:
            print(f"  [DEBUG] split_cia_name: extracted alias '{alias}'")

    # ── Strategy 2b: Strip embedded honorific ────────────────────────────────
    # CIA sometimes puts "Dr." in name field instead of honorific field
    # e.g. "Dr. Shaya al-Zindani" — strip so it doesn't contaminate firstName
    embedded_honorific = None
    hon_match = _HONORIFIC_PREFIXES_RE.match(raw_name)
    if hon_match:
        embedded_honorific = hon_match.group(1).strip()
        raw_name = raw_name[hon_match.end():].strip()
        if DEBUG:
            print(f"  [DEBUG] split_cia_name: stripped honorific '{embedded_honorific}'")

    tokens = raw_name.split()
    if not tokens:
        return null_result

    # ── Strategy 3: East Asian (family name first) ────────────────────────────
    # CIA writes East Asian family names in ALL CAPS as the FIRST token
    # "KIM Jong-un" → family: Kim, given: Jong-un
    # Tested: nameparser gets this WRONG — treats CAPS first token as first name
    if _is_east_asian_name(tokens, country_code):
        last_name = tokens[0].title()
        first_name = " ".join(tokens[1:]).strip() or None
        if DEBUG:
            print(f"  [DEBUG] split_cia_name: East Asian → first='{first_name}' last='{last_name}'")
        return {
            "first_name": first_name,
            "last_name": last_name,
            "suffix": None,
            "alias": alias,
            "embedded_honorific": embedded_honorific,
        }

    # ── Strategy 4: Burma mononym ─────────────────────────────────────────────
    # Burma has no family name convention — full name stored as last_name
    # Trigger: country is Burma (mm) AND all tokens are ALL CAPS
    if country_code == "mm":
        all_caps = all(
            re.sub(r'[^A-Za-z]', '', t).isupper()
            for t in tokens
            if re.sub(r'[^A-Za-z]', '', t)
        )
        if all_caps:
            last_name = " ".join(t.title() for t in tokens)
            if DEBUG:
                print(f"  [DEBUG] split_cia_name: Burma mononym → last='{last_name}'")
            return {
                "first_name": None,
                "last_name": last_name,
                "suffix": None,
                "alias": alias,
                "embedded_honorific": embedded_honorific,
            }

    # ── Strategy 5: Monarch/regnal name ──────────────────────────────────────
    # e.g. "CHARLES III", "WILLEM-ALEXANDER"
    # Trigger: all non-numeral tokens are CAPS + Roman numeral at end
    # WHY III is not a suffix here: regnal numerals belong to the whole name
    non_numeral_tokens = [t for t in tokens if not _ROMAN_RE.match(t)]
    if _ROMAN_RE.search(raw_name) and non_numeral_tokens:
        clean_non_numeral = [re.sub(r'[^A-Za-z\-]', '', t) for t in non_numeral_tokens]
        if all(t.isupper() and len(t) > 0 for t in clean_non_numeral if t):
            result_tokens = []
            for t in tokens:
                if _ROMAN_RE.match(t):
                    result_tokens.append(t.upper())
                else:
                    result_tokens.append(t.title())
            last_name = " ".join(result_tokens)
            if DEBUG:
                print(f"  [DEBUG] split_cia_name: Monarch → last='{last_name}'")
            return {
                "first_name": None,
                "last_name": last_name,
                "suffix": None,
                "alias": alias,
                "embedded_honorific": embedded_honorific,
            }

    # ── Strategy 6: Arabic particle ──────────────────────────────────────────
    # Trigger: name contains al-, el-, bin, bint, abu, ibn before a CAPS word
    # Strategy: find last CAPS token (with any attached particle) = family name
    # Everything before = given name chain
    # Tested: nameparser handles most Arabic names correctly but misses
    # some cases where particle is separate e.g. "bin Said al-UTAYBA"
    if _ARABIC_PARTICLE_RE.search(raw_name):
        last_caps_idx = None
        for i in range(len(tokens) - 1, -1, -1):
            token = tokens[i]
            # Strip fused particle to check if remainder is CAPS
            # e.g. "al-ALIMI" → strip "al-" → "ALIMI"
            core = token
            for prefix in ("al-", "Al-", "AL-", "el-", "El-", "EL-"):
                if token.startswith(prefix):
                    core = token[len(prefix):]
                    break
            clean_core = re.sub(r'[^A-Za-z]', '', core)
            if clean_core and clean_core.isupper() and len(clean_core) > 1:
                last_caps_idx = i
                break

        if last_caps_idx is not None:
            first_tokens = tokens[:last_caps_idx]
            last_token = tokens[last_caps_idx]

            # Title-case preserving fused particle
            # "al-ALIMI" → "al-Alimi"
            if '-' in last_token:
                particle_part, name_part = last_token.split('-', 1)
                last_name = f"{particle_part.lower()}-{name_part.title()}"
            else:
                last_name = last_token.title()

            first_name = " ".join(first_tokens).strip() or None

            if DEBUG:
                print(f"  [DEBUG] split_cia_name: Arabic → first='{first_name}' last='{last_name}'")
            return {
                "first_name": first_name,
                "last_name": last_name,
                "suffix": None,
                "alias": alias,
                "embedded_honorific": embedded_honorific,
            }

    # ── Strategy 7: Romance compound surname ─────────────────────────────────
    # Spanish/Portuguese names: CAPS word in MIDDLE + title-case word after
    # e.g. "Claudia SHEINBAUM Pardo" → first: Claudia, last: Sheinbaum Pardo
    # Tested: nameparser gets this WRONG — puts CAPS word in middle, last word as last
    if _is_romance_name(tokens, country_code):
        caps_indices = [
            i for i, t in enumerate(tokens)
            if re.sub(r'[^A-Za-z]', '', t).isupper()
            and len(re.sub(r'[^A-Za-z]', '', t)) > 1
        ]
        # Take the first CAPS index as start of last name
        last_start = caps_indices[0]
        first_name = " ".join(tokens[:last_start]).strip() or None
        # Everything from CAPS word to end = combined last name
        last_parts = tokens[last_start:]
        last_name = " ".join(t.title() for t in last_parts).strip() or None

        if DEBUG:
            print(f"  [DEBUG] split_cia_name: Romance → first='{first_name}' last='{last_name}'")
        return {
            "first_name": first_name,
            "last_name": last_name,
            "suffix": None,
            "alias": alias,
            "embedded_honorific": embedded_honorific,
        }

    # ── Strategy 8: nameparser default fallback ───────────────────────────────
    # Handles: standard Western, Iran (hyphenated), most African, most Arabic
    # nameparser understands particles (van der, de la), suffixes (Jr., Sr.)
    # Middle name appended to first_name — FtM has no middleName property
    parsed = HumanName(raw_name)

    first_name = parsed.first or None
    middle = parsed.middle or None
    last_name = parsed.last or None
    suffix = parsed.suffix or None

    if first_name and middle:
        first_name = f"{first_name} {middle}"

    # Title-case last name — nameparser may preserve original casing
    if last_name:
        last_name = last_name.title()

    if not first_name and not last_name:
        print(f"  [WARN] split_cia_name: could not split '{raw_name}'")
    elif not first_name:
        print(f"  [WARN] split_cia_name: no first name in '{raw_name}'")
    elif not last_name:
        print(f"  [WARN] split_cia_name: no last name in '{raw_name}'")

    if DEBUG:
        print(f"  [DEBUG] split_cia_name: nameparser → first='{first_name}' last='{last_name}' suffix='{suffix}'")

    return {
        "first_name": first_name,
        "last_name": last_name,
        "suffix": suffix,
        "alias": alias,
        "embedded_honorific": embedded_honorific,
    }


# ─── Verification ─────────────────────────────────────────────────────────────

def verify_country_mappings():
    """
    Tests to_iso_country() against all country strings from profiler output.
    Run once after setup. Any FAIL = add to COUNTRY_OVERRIDES.

    Usage:
        python transform/helpers.py
    """
    test_names = [
        "Afghanistan", "Albania", "Algeria", "Andorra", "Angola",
        "Antigua and Barbuda", "Argentina", "Armenia", "Aruba", "Australia",
        "Austria", "Azerbaijan", "Bahamas, The", "Bahrain", "Bangladesh",
        "Barbados", "Belarus", "Belgium", "Belize", "Benin", "Bhutan",
        "Bolivia", "Bosnia and Herzegovina", "Botswana", "Brazil", "Brunei",
        "Bulgaria", "Burkina Faso", "Burma", "Burundi", "Cabo Verde",
        "Cambodia", "Cameroon", "Canada", "Central African Republic", "Chad",
        "Chile", "China", "Colombia", "Comoros",
        "Congo, Democratic Republic of the", "Congo, Republic of the",
        "Costa Rica", "Croatia", "Cuba", "Cyprus", "Czechia", "Denmark",
        "Djibouti", "Dominica", "Dominican Republic", "Ecuador", "Egypt",
        "El Salvador", "Equatorial Guinea", "Eritrea", "Estonia", "Eswatini",
        "Ethiopia", "Fiji", "Finland", "France", "Gabon", "Gambia, The",
        "Georgia", "Germany", "Ghana", "Greece", "Grenada", "Guatemala",
        "Guinea", "Guinea-Bissau", "Guyana", "Haiti", "Holy See", "Honduras",
        "Hungary", "Iceland", "India", "Indonesia", "Iran", "Iraq", "Ireland",
        "Israel", "Italy", "Jamaica", "Japan", "Jordan", "Kazakhstan", "Kenya",
        "Kiribati", "Korea, North", "Korea, South", "Kuwait", "Kyrgyzstan",
        "Laos", "Lebanon", "Lesotho", "Liberia", "Libya", "Liechtenstein",
        "Lithuania", "Luxembourg", "Madagascar", "Malawi", "Malaysia",
        "Maldives", "Mali", "Malta", "Marshall Islands", "Mauritania",
        "Mauritius", "Mexico", "Micronesia, Federated States of", "Moldova",
        "Monaco", "Mongolia", "Montenegro", "Morocco", "Mozambique",
        "Namibia", "Nauru", "Nepal", "Netherlands", "New Zealand",
        "Nicaragua", "Niger", "Nigeria", "North Macedonia", "Norway", "Oman",
        "Pakistan", "Palau", "Panama", "Papua New Guinea", "Paraguay", "Peru",
        "Philippines", "Poland", "Portugal", "Qatar", "Romania", "Russia",
        "Rwanda", "Saint Kitts and Nevis", "Saint Lucia",
        "Saint Vincent and the Grenadines", "Samoa", "San Marino",
        "Saudi Arabia", "Senegal", "Serbia", "Seychelles", "Sierra Leone",
        "Singapore", "Slovakia", "Slovenia", "Solomon Islands", "Somalia",
        "South Africa", "South Sudan", "Spain", "Sri Lanka", "Sudan",
        "Suriname", "Sweden", "Switzerland", "Syria", "Taiwan", "Tajikistan",
        "Tanzania", "Thailand", "Timor-Leste", "Togo", "Tonga",
        "Trinidad and Tobago", "Tunisia", "Turkey", "Turkmenistan", "Tuvalu",
        "Uganda", "Ukraine", "United Arab Emirates", "United Kingdom",
        "United States", "Uruguay", "Uzbekistan", "Vanuatu", "Venezuela",
        "Vietnam", "Yemen", "Zambia", "Zimbabwe",
        "Iraq", "Lebanon", "China", "Korea, North", "Ukraine",
        "Burma", "Colombia", "Yemen", "Pakistan", "Turkey",
        "Nicaragua", "India", "Somalia", "Saudi Arabia",
    ]

    print("Verifying country mappings...\n")
    failures = []

    for name in sorted(set(test_names)):
        result = to_iso_country(name)
        status = "OK  " if len(result) == 2 else "FAIL"
        if status == "FAIL":
            failures.append((name, result))
        print(f"  {status}  {name:<45} → {result}")

    print(f"\n{'─' * 55}")
    print(f"  {len(test_names) - len(failures)} passed, {len(failures)} failed")
    if failures:
        print("\n  Add these to COUNTRY_OVERRIDES:")
        for name, result in failures:
            print(f'    "{name}": "??",  # got: {result}')
    else:
        print("\n  All country mappings resolved correctly.")


if __name__ == "__main__":
    verify_country_mappings()