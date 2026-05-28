import requests
from bs4 import BeautifulSoup
import json
import time

# ─── Constants ────────────────────────────────────────────────────────────────
# These will later be moved to config.yaml — kept here for now for simplicity

INDEX_URL = "https://www.cia.gov/resources/world-leaders/foreign-governments/"
BASE_JSON_URL = "https://www.cia.gov/resources/world-leaders/page-data/foreign-governments"

HEADERS = {
    # Identifies your scraper honestly — avoids being blocked as an anonymous bot
    "User-Agent": "ComplianceResearch/1.0 (academic use)"
}

DELAY_SECONDS = 1.5  # Polite delay between requests
MAX_RETRIES = 3       # How many times to retry a failed request


# ─── Helpers ──────────────────────────────────────────────────────────────────

def fetch_with_retry(url):
    """
    Fetches a URL with simple retry logic.
    Waits 2^attempt seconds between retries (2s, 4s, 8s).
    Raises an exception if all retries fail.

    Future enhancement: replace with tenacity library for more control.
    """
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            wait = 2 ** (attempt + 1)  # 2s, 4s, 8s
            print(f"    Retry {attempt + 1}/{MAX_RETRIES} for {url} — waiting {wait}s ({e})")
            time.sleep(wait)

    raise Exception(f"All {MAX_RETRIES} retries failed for {url}")


def split_datetime(raw_date):
    """
    Splits "2024-10-09 21:27:30" into ("2024-10-09", "21:27:30").
    Returns ("", "") if the value is empty or missing.
    """
    if not raw_date:
        return "", ""
    if " " in raw_date:
        date, time_str = raw_date.split(" ", maxsplit=1)
        return date, time_str
    return raw_date, ""  # date only, no time component


# ─── Core Functions ───────────────────────────────────────────────────────────

def get_country_slugs():
    """
    Scrapes the CIA index page and returns a list of country slugs.

    A slug is the URL segment for a country, e.g:
      "philippines", "north-korea", "bosnia-and-herzegovina"

    We use BeautifulSoup here because the index page is server-rendered HTML.
    We target <a class="inline-link"> tags — confirmed by inspecting the page.

    Returns:
        list of str — e.g. ["albania", "algeria", "andorra", ...]
    """
    print("Fetching index page...")
    resp = fetch_with_retry(INDEX_URL)
    soup = BeautifulSoup(resp.text, "lxml")

    slugs = []
    for a_tag in soup.find_all("a", class_="inline-link", href=True):
        href = a_tag["href"]

        # Only process country detail page links
        if "/foreign-governments/" in href:
            # "/resources/world-leaders/foreign-governments/albania/" → "albania"
            slug = href.rstrip("/").split("/")[-1]
            if slug:
                slugs.append(slug)

    print(f"Found {len(slugs)} countries\n")
    return slugs


def get_country_data(slug):
    """
    Fetches structured data for one country using the Gatsby page-data.json endpoint.
    This avoids HTML parsing entirely — the data is already clean JSON.

    Args:
        slug: country URL slug, e.g. "philippines"

    Returns:
        dict with country info and list of leaders, or None if fetch fails
    """
    url = f"{BASE_JSON_URL}/{slug}/page-data.json"
    resp = fetch_with_retry(url)

    data = resp.json()
    page = data["result"]["data"]["page"]

    date_updated, time_updated = split_datetime(page.get("date_updated", ""))

    return {
        "country": page["country"],
        "country_code": page["code"],
        "date_updated": date_updated,        # "2024-10-09"
        "time_updated": time_updated,        # "21:27:30"
        "caveat": page.get("caveat", ""),    # contextual notes e.g. rival governments
        "diplomatic_exchange": page.get("diplomatic_exchange", ""),
        "leaders": page["leaders"],          # list of {name, title, honorific}
    }


# ─── Main Run Function ────────────────────────────────────────────────────────

def run(limit=None):
    """
    Scrapes all country pages and saves results to cia_raw.json.

    Args:
        limit: number of countries to scrape (None = all ~199)
                Set to a small number like 2 or 5 when testing.

    Returns:
        list of country dicts (so FastAPI / pipeline can use this data directly)

    Output files:
        cia_raw.json      — all successfully scraped countries
        cia_failed.json   — slugs that failed after all retries
    """
    slugs = get_country_slugs()
    slugs_to_process = slugs[:limit]  # limit=None returns the full list

    print(f"Scraping {len(slugs_to_process)} countries...\n")

    results = []
    failed = []

    for i, slug in enumerate(slugs_to_process, start=1):
        print(f"  [{i}/{len(slugs_to_process)}] {slug}")
        try:
            country_data = get_country_data(slug)
            results.append(country_data)
            print(f"  OK — {country_data['country']}: {len(country_data['leaders'])} leaders")
        except Exception as e:
            # One failure should not stop the whole scrape
            failed.append(slug)
            print(f"  FAILED — {slug}: {e}")

        # Polite delay — skip delay after the last item
        if i < len(slugs_to_process):
            time.sleep(DELAY_SECONDS)

    # ── Save results ──────────────────────────────────────────────────────────
    with open("ingest/cia_raw.json", "w") as f:
        json.dump(results, f, indent=2)

    if failed:
        with open("ingest/cia_failed.json", "w") as f:
            json.dump(failed, f, indent=2)
        print(f"\n{len(failed)} countries failed — see cia_failed.json")

    print(f"\nDone. {len(results)} countries saved to cia_raw.json")

    # Return data so FastAPI pipeline can use it without reading the file
    return results


# ─── Entry Point ──────────────────────────────────────────────────────────────
# Only runs when you execute this file directly: python cia_scraper.py
# Does NOT run when imported by another module (e.g. the FastAPI app)

if __name__ == "__main__":
    run(limit=None)  # Change to limit=None for the full scrape