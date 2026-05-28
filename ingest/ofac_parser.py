import requests
import xml.etree.ElementTree as ET
import json
import time

# ─── Constants ────────────────────────────────────────────────────────────────
# These will later be moved to config.yaml

XML_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"
XML_PATH = "ingest/sdn.xml"          # saved locally so we don't re-download on every parse
OUTPUT_PATH = "ingest/ofac_raw.json"

# The namespace declared at the top of the XML file:
# <sdnList xmlns="https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML">
# Every tag search must include this — without it, findall/findtext return nothing.
NS = {"ofac": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def ft(element, tag):
    """
    Shorthand for element.findtext() with namespace.
    Returns empty string if the tag is missing — avoids None checks everywhere.

    Args:
        element: an XML element to search inside
        tag: tag name without namespace prefix, e.g. "firstName"

    Example:
        ft(entry, "firstName")  →  "Vladimir" or ""
    """
    return element.findtext(f"ofac:{tag}", default="", namespaces=NS)


def fa(element, tag):
    """
    Shorthand for element.findall() with namespace.
    Returns a list of matching child elements.

    Args:
        element: an XML element to search inside
        tag: tag name without namespace prefix, e.g. "aka"

    Example:
        fa(aka_list, "aka")  →  list of <aka> elements
    """
    return element.findall(f"ofac:{tag}", NS)


# ─── Download ─────────────────────────────────────────────────────────────────

def download_xml():
    """
    Downloads the OFAC SDN XML file and saves it to disk.
    We save it locally so:
    - If parsing fails, we don't need to re-download
    - Future enhancement: check Last-Modified header to skip download if unchanged

    The file is ~30MB — small enough to download and load into memory entirely.
    """
    print(f"Downloading OFAC SDN XML from {XML_URL}...")
    start = time.time()

    resp = requests.get(XML_URL, stream=True)
    resp.raise_for_status()

    with open(XML_PATH, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    elapsed = round(time.time() - start, 1)
    print(f"Downloaded in {elapsed}s → {XML_PATH}\n")


# ─── Parsers for nested sections ──────────────────────────────────────────────

def parse_aliases(entry):
    """
    Extracts all aliases from <akaList><aka> elements.

    From the real XML we confirmed:
    - Each <aka> has: uid, type (e.g. "a.k.a."), category (e.g. "strong"), 
      lastName, and optionally firstName
    - Some aliases are just a lastName (e.g. company aliases like "AERO-CARIBBEAN")

    Returns list of dicts, empty list if no akaList exists.
    """
    aka_list = entry.find("ofac:akaList", NS)
    if aka_list is None:
        return []

    aliases = []
    for aka in fa(aka_list, "aka"):
        aliases.append({
            "type": ft(aka, "type"),          # "a.k.a.", "f.k.a.", etc.
            "category": ft(aka, "category"),  # "strong" or "weak"
            "first_name": ft(aka, "firstName"),
            "last_name": ft(aka, "lastName"),
        })
    return aliases


def parse_dates_of_birth(entry):
    """
    Extracts all dates of birth from <dateOfBirthList><dateOfBirthItem> elements.

    From the real XML we confirmed:
    - Multiple DOBs are common — OFAC lists all known or possible dates
    - Dates are NOT standardized — could be "03 May 1938", "1938", "1952-10-07"
    - mainEntry="true" marks the primary/most reliable date
    - We capture raw date strings here — normalization to ISO format happens in transform step

    Returns list of dicts ordered by mainEntry first.
    """
    dob_list = entry.find("ofac:dateOfBirthList", NS)
    if dob_list is None:
        return []

    dates = []
    for item in fa(dob_list, "dateOfBirthItem"):
        dates.append({
            "date": ft(item, "dateOfBirth"),
            "main_entry": ft(item, "mainEntry") == "true",  # convert string to bool
        })

    # Sort so main_entry=True comes first
    dates.sort(key=lambda x: not x["main_entry"])
    return dates


def parse_nationalities(entry):
    """
    Extracts nationalities from <nationalityList><nationality> elements.
    Returns list of country name strings.
    """
    nat_list = entry.find("ofac:nationalityList", NS)
    if nat_list is None:
        return []

    return [
        ft(nat, "country")
        for nat in fa(nat_list, "nationality")
        if ft(nat, "country")  # skip empty strings
    ]


def parse_citizenships(entry):
    """
    Extracts citizenships from <citizenshipList><citizenship> elements.
    Returns list of country name strings.
    """
    cit_list = entry.find("ofac:citizenshipList", NS)
    if cit_list is None:
        return []

    return [
        ft(cit, "country")
        for cit in fa(cit_list, "citizenship")
        if ft(cit, "country")
    ]


def parse_programs(entry):
    """
    Extracts sanction programs from <programList><program> elements.
    Programs indicate why someone is sanctioned e.g. "IRAN", "RUSSIA", "SDGT".
    Returns list of program name strings.
    """
    prog_list = entry.find("ofac:programList", NS)
    if prog_list is None:
        return []

    return [
        prog.text.strip()
        for prog in fa(prog_list, "program")
        if prog.text
    ]


def parse_ids(entry):
    """
    Extracts IDs from <idList><id> elements.
    IDs include passport numbers, national IDs, tax numbers, etc.
    Useful later for deduplication in Part 3.
    Returns list of dicts.
    """
    id_list = entry.find("ofac:idList", NS)
    if id_list is None:
        return []

    ids = []
    for id_item in fa(id_list, "id"):
        ids.append({
            "type": ft(id_item, "idType"),
            "number": ft(id_item, "idNumber"),
            "country": ft(id_item, "idCountry"),
        })
    return ids


def parse_addresses(entry):
    """
    Extracts addresses from <addressList><address> elements.
    Returns list of dicts with city, country, and other available fields.
    """
    addr_list = entry.find("ofac:addressList", NS)
    if addr_list is None:
        return []

    addresses = []
    for addr in fa(addr_list, "address"):
        addresses.append({
            "city": ft(addr, "city"),
            "country": ft(addr, "country"),
            "state_or_province": ft(addr, "stateOrProvince"),
            "postal_code": ft(addr, "postalCode"),
        })
    return addresses


# ─── Main Parser ──────────────────────────────────────────────────────────────

def parse_xml():
    """
    Parses the locally saved sdn.xml and extracts all Individual-type records.

    Why we filter by sdnType == "Individual":
    - The SDN list contains people, companies, vessels, aircraft
    - We only want real people for this compliance project
    - Filtering early keeps the output clean and focused

    Returns list of dicts — one per Individual record.
    """
    print(f"[Parser] Opening {XML_PATH}...")

    try:
        tree = ET.parse(XML_PATH)
    except FileNotFoundError:
        raise FileNotFoundError(f"[Parser] ERROR — {XML_PATH} not found. Run with skip_download=False first.")
    except ET.ParseError as e:
        raise Exception(f"[Parser] ERROR — XML is malformed: {e}")

    print("[Parser] Building element tree...")
    root = tree.getroot()

    all_entries = root.findall("ofac:sdnEntry", NS)
    print(f"[Parser] Total SDN entries found: {len(all_entries)}")

    records = []
    skipped = 0
    errors = []

    for i, entry in enumerate(all_entries, start=1):

        # Progress log every 1000 entries so you know it's running
        if i % 1000 == 0:
            print(f"[Parser] Processing entry {i}/{len(all_entries)}...")

        sdn_type = ft(entry, "sdnType")
        if sdn_type != "Individual":
            skipped += 1
            continue

        # Wrap each record in try/except so one bad record doesn't stop everything
        try:
            record = {
                "uid": ft(entry, "uid"),
                "first_name": ft(entry, "firstName"),
                "last_name": ft(entry, "lastName"),
                "title": ft(entry, "title"),
                "remarks": ft(entry, "remarks"),
                "programs": parse_programs(entry),
                "aliases": parse_aliases(entry),
                "dates_of_birth": parse_dates_of_birth(entry),
                "nationalities": parse_nationalities(entry),
                "citizenships": parse_citizenships(entry),
                "addresses": parse_addresses(entry),
                "ids": parse_ids(entry),
            }
            records.append(record)
        except Exception as e:
            uid = ft(entry, "uid")
            errors.append(uid)
            print(f"[Parser] FAILED on entry uid={uid}: {e}")

    print(f"\n[Parser] Done processing all entries")
    print(f"[Parser] Individuals extracted: {len(records)}")
    print(f"[Parser] Non-individuals skipped: {skipped}")
    if errors:
        print(f"[Parser] Records with errors: {len(errors)} — uids: {errors[:10]}")

    return records


# ─── Main Run Function ────────────────────────────────────────────────────────

def run(skip_download=False):
    """
    Downloads and parses the OFAC SDN XML file.

    Args:
        skip_download: if True, skip downloading and use existing sdn.xml
                       useful during development so you don't re-download every run

    Returns:
        list of Individual record dicts (so FastAPI pipeline can use directly)

    Output files:
        ingest/sdn.xml        — raw downloaded XML file
        ingest/ofac_raw.json  — parsed Individual records
    """
    start_time = time.time()

    if skip_download:
        print("Skipping download — using existing sdn.xml\n")
    else:
        download_xml()

    records = parse_xml()

    # Save to JSON
    with open(OUTPUT_PATH, "w") as f:
        json.dump(records, f, indent=2)
    print(f"Saved {len(records)} records to {OUTPUT_PATH}")

    # Print runtime
    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    print(f"Completed in {minutes}m {seconds}s")

    return records


# ─── Entry Point ──────────────────────────────────────────────────────────────
# Only runs when you execute: python ofac_parser.py
# Does NOT run when imported by another module

if __name__ == "__main__":
    run(skip_download=True)  # set skip_download=True after first run to save time