import requests
from bs4 import BeautifulSoup
import json
import time
from playwright.sync_api import sync_playwright


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
    Scrapes the CIA index page and returns a list of all country slugs.

    The index page shows only 12 countries by default. It has a dropdown
    with an "All" option (value="-1") that loads the full list via JavaScript.
    We use Playwright to select "All" from the dropdown and wait for all 199
    country links to render, then extract the slugs with BeautifulSoup.

    Note: We rename the Playwright browser tab to pw_page to avoid confusion
    with the "page" variable used in get_country_data() which is a dict.

    Returns:
        list of str — e.g. ["albania", "algeria", "andorra", ...]
    """
    print("[Playwright] Starting browser...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # Set to True to run headless (no browser window)
        pw_page = browser.new_page()

        print(f"[Playwright] Navigating to index page...")
        pw_page.goto(INDEX_URL)

        print("[Playwright] Waiting for page to load (networkidle)...")
        pw_page.wait_for_load_state("networkidle")

        print("[Playwright] Waiting for first country link to appear...")
        pw_page.wait_for_selector("a.inline-link", timeout=15000)  # wait until at least one link appears

        # Count links before selecting All — should be 12
        before = pw_page.evaluate("document.querySelectorAll('a.inline-link').length")
        print(f"[Playwright] Country links BEFORE selecting All: {before}")

        print("[Playwright] Selecting 'All' from dropdown...")
        pw_page.select_option("select", value="-1")

        print("[Playwright] Waiting for all country links to appear...")
        pw_page.wait_for_selector("a.inline-link", timeout=15000)  # wait until all links appear

        # Count links after selecting All — should be 199
        after = pw_page.evaluate("document.querySelectorAll('a.inline-link').length")
        print(f"[Playwright] Country links AFTER selecting All: {after}")

        print("[Playwright] Grabbing full page HTML...")
        html = pw_page.content()

        browser.close()
        print("[Playwright] Browser closed.\n")

    soup = BeautifulSoup(html, "lxml")

    slugs = []
    for a_tag in soup.find_all("a", class_="inline-link", href=True):
        href = a_tag["href"]
        if "/foreign-governments/" in href:
            # "/resources/world-leaders/foreign-governments/albania/" → "albania"
            slug = href.rstrip("/").split("/")[-1]
            if slug:
                slugs.append(slug)

    print(f"Found {len(slugs)} country slugs\n")
    return slugs


def get_country_data(slug):
    """
    Fetches structured data for one country using the Gatsby page-data.json endpoint.
    This avoids HTML parsing entirely — the data is already clean JSON.

    Note: The variable "page" here is a plain Python dict from the JSON response.
    It has nothing to do with Playwright's page object in get_country_slugs().

    Args:
        slug: country URL slug, e.g. "philippines"

    Returns:
        dict with country info and list of leaders
    """
    url = f"{BASE_JSON_URL}/{slug}/page-data.json"
    resp = fetch_with_retry(url)

    data = resp.json()
    page = data["result"]["data"]["page"]  # plain dict, not a Playwright object

    date_updated, time_updated = split_datetime(page.get("date_updated", ""))

    return {
        "country": page["country"],
        "country_code": page["code"],
        "date_updated": date_updated,               # "2024-10-09"
        "time_updated": time_updated,               # "21:27:30"
        "caveat": page.get("caveat", ""),           # contextual notes e.g. rival governments
        "diplomatic_exchange": page.get("diplomatic_exchange", ""),
        "leaders": page["leaders"],                 # list of {name, title, honorific}
    }


# ─── Main Run Function ────────────────────────────────────────────────────────

def run(limit=None):
    """
    Scrapes all country pages and saves results to cia_raw.json.

    Args:
        limit: number of countries to scrape (None = all ~199)
               Set to a small number like 2 or 5 when testing.

    Returns:
        list of country dicts (so FastAPI / pipeline can use this directly)

    Output files:
        ingest/cia_raw.json    — all successfully scraped countries
        ingest/cia_failed.json — slugs that failed after all retries (only created if there are failures)
    """
    start_time = time.time()

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
            # One failure should not stop the whole scrape — log and continue
            failed.append(slug)
            print(f"  FAILED — {slug}: {e}")

        # Polite delay — skip after the last item
        if i < len(slugs_to_process):
            time.sleep(DELAY_SECONDS)

    # ── Save results ──────────────────────────────────────────────────────────
    with open("ingest/cia_raw.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {len(results)} countries to ingest/cia_raw.json")

    if failed:
        with open("ingest/cia_failed.json", "w") as f:
            json.dump(failed, f, indent=2)
        print(f"{len(failed)} countries failed — see ingest/cia_failed.json")

    # ── Print runtime ─────────────────────────────────────────────────────────
    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    print(f"Completed in {minutes}m {seconds}s")

    # Return data so FastAPI pipeline can use it without reading the file
    return results


# ─── Entry Point ──────────────────────────────────────────────────────────────
# Only runs when you execute: python cia_scraper.py
# Does NOT run when another module imports this file (e.g. the FastAPI app)

if __name__ == "__main__":
    run(limit=None)  # Change to limit=None for the full scrape