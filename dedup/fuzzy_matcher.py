"""
dedup/fuzzy_matcher.py
======================
File 1 of 2 — Baseline fuzzy matcher.

Pure blocked fuzzy name matching using rapidfuzz.
No weighted scoring, no transliteration, no country/position signals.
This is the baseline — every other matcher is compared against this.

APPROACH:
    1. Build a blocking index — group records by shared name tokens
       so we only compare pairs that share at least one word.
       Avoids the naive 5800 × 7500 = 43M comparison problem.
    2. For each candidate pair, score with rapidfuzz token_sort_ratio.
       token_sort_ratio sorts tokens before comparing so word order
       doesn't matter ("Marcos Ferdinand" vs "Ferdinand Marcos" = 100).
    3. Pairs above threshold → candidate match.

THRESHOLDS:
    >= 90  AUTO_MERGE   — very high name similarity, confident match
    >= 70  REVIEW       — possible match, flag for human review
    <  70  no output    — not a candidate

WHY token_sort_ratio:
    CIA and OFAC store names in different orders.
    token_sort_ratio handles this by sorting tokens alphabetically
    before comparing, making order irrelevant.

OUTPUT:
    dedup/fuzzy_results.json   — list of candidate pair dicts
    dedup/fuzzy_report.csv     — same data as CSV for inspection

Run:
    python dedup/fuzzy_matcher.py
"""

import csv
import json
import os
import time
from collections import defaultdict

from rapidfuzz import fuzz

# ─── Paths ────────────────────────────────────────────────────────────────────

CIA_PATH        = "transform/cia_normalized.json"
OFAC_PATH       = "transform/ofac_normalized.json"
OUTPUT_JSON     = "dedup/fuzzy_results.json"
OUTPUT_CSV      = "dedup/fuzzy_report.csv"

# ─── Thresholds ───────────────────────────────────────────────────────────────

THRESHOLD_AUTO   = 90   # >= this → AUTO_MERGE
THRESHOLD_REVIEW = 70   # >= this → REVIEW
                        # <  this → ignored

# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_name(entity: dict) -> str:
    """
    Extract the primary display name from an FtM entity.

    Tries `name` property first, falls back to joining firstName + lastName.
    Returns empty string if nothing found — caller handles the skip.

    Args:
        entity: FtM Person dict with a `properties` key.

    Returns:
        Name string, lowercased and stripped.
    """
    props = entity.get("properties", {})

    name_list = props.get("name", [])
    if name_list:
        return name_list[0].lower().strip()

    # Fallback: join firstName + lastName
    first = (props.get("firstName") or [""])[0]
    last  = (props.get("lastName")  or [""])[0]
    return f"{first} {last}".lower().strip()


def tokenize(name: str) -> set[str]:
    """
    Split a name into a set of tokens for blocking index lookup.

    Lowercased, split on whitespace and hyphens.
    Tokens shorter than 2 chars are dropped (avoids noise from
    initials and particles like "a", "o").

    Args:
        name: Display name string.

    Returns:
        Set of token strings.
    """
    # Replace hyphens with spaces so "Al-Zawahiri" → {"al", "zawahiri"}
    cleaned = name.replace("-", " ").replace(",", " ")
    return {t for t in cleaned.split() if len(t) >= 2}


# ─── Blocking Index ───────────────────────────────────────────────────────────

def build_ofac_index(ofac_persons: list[dict]) -> dict[str, list[int]]:
    """
    Build an inverted index mapping name tokens → OFAC record positions.

    This is the blocking step. Instead of comparing every CIA person
    against every OFAC person (43M pairs), we only compare pairs that
    share at least one name token. Reduces comparison space by ~95%.

    Example index:
        {"putin":   [42, 891],
         "vladimir": [42, 1203, 4521], ...}

    Args:
        ofac_persons: List of normalised OFAC Person dicts.

    Returns:
        Dict of token → list of indices into ofac_persons.
    """
    index: dict[str, list[int]] = defaultdict(list)

    for i, person in enumerate(ofac_persons):
        name   = get_name(person)
        tokens = tokenize(name)
        for token in tokens:
            index[token].append(i)

    return dict(index)


# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_pair(cia_name: str, ofac_name: str) -> float:
    """
    Score a CIA/OFAC name pair using rapidfuzz token_sort_ratio.

    token_sort_ratio sorts tokens before comparing, so word order
    differences ("Marcos Ferdinand" vs "Ferdinand Marcos") don't
    penalise the score.

    Args:
        cia_name:  Normalised CIA display name.
        ofac_name: Normalised OFAC display name.

    Returns:
        Float 0–100. Higher = more similar.
    """
    return fuzz.token_sort_ratio(cia_name, ofac_name)


def recommend(score: float) -> str:
    """Map a score to a recommended action string."""
    if score >= THRESHOLD_AUTO:
        return "AUTO_MERGE"
    if score >= THRESHOLD_REVIEW:
        return "REVIEW"
    return "NO_MATCH"


# ─── Main Matcher ─────────────────────────────────────────────────────────────

def run() -> list[dict]:
    """
    Load both normalized files, find candidate pairs, write output.

    ALGORITHM:
        For each CIA person:
            1. Tokenize their name
            2. Look up each token in the OFAC blocking index
            3. Get the union of candidate OFAC indices
            4. Score each candidate with token_sort_ratio
            5. Keep pairs above THRESHOLD_REVIEW

    Returns list of candidate pair dicts (also written to disk).
    """
    start = time.time()

    # ── Load data ─────────────────────────────────────────────────────────────
    print("Loading CIA normalized data ...")
    with open(CIA_PATH, encoding="utf-8") as f:
        cia_data = json.load(f)
    cia_persons = cia_data["persons"]
    print(f"  {len(cia_persons)} CIA persons loaded")

    print("Loading OFAC normalized data ...")
    with open(OFAC_PATH, encoding="utf-8") as f:
        ofac_data = json.load(f)
    ofac_persons = ofac_data["persons"]
    print(f"  {len(ofac_persons)} OFAC persons loaded\n")

    # ── Build blocking index ───────────────────────────────────────────────────
    print("Building OFAC blocking index ...")
    ofac_index = build_ofac_index(ofac_persons)
    print(f"  {len(ofac_index)} unique tokens indexed\n")

    # ── Compare ───────────────────────────────────────────────────────────────
    print("Comparing pairs ...")
    candidates = []
    comparisons = 0

    for cia in cia_persons:
        cia_name   = get_name(cia)
        cia_tokens = tokenize(cia_name)

        if not cia_tokens:
            continue

        # Get candidate OFAC indices from blocking index
        # Union of all OFAC records that share any token with this CIA person
        candidate_indices: set[int] = set()
        for token in cia_tokens:
            candidate_indices.update(ofac_index.get(token, []))

        # Score each candidate
        for idx in candidate_indices:
            ofac       = ofac_persons[idx]
            ofac_name  = get_name(ofac)
            score      = score_pair(cia_name, ofac_name)
            comparisons += 1

            if score < THRESHOLD_REVIEW:
                continue

            candidates.append({
                "cia_id":      cia["id"],
                "ofac_id":     ofac["id"],
                "cia_name":    cia_name,
                "ofac_name":   ofac_name,
                "cia_country": cia.get("country", ""),
                "ofac_nationality": (
                    ofac["properties"].get("nationality") or [""]
                )[0],
                "name_score":         round(score, 2),
                "recommended_action": recommend(score),
            })

    elapsed = time.time() - start

    # Sort by score descending for easy inspection
    candidates.sort(key=lambda x: x["name_score"], reverse=True)

    # ── Write output ──────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUTPUT_JSON), exist_ok=True)

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        if candidates:
            writer = csv.DictWriter(f, fieldnames=candidates[0].keys())
            writer.writeheader()
            writer.writerows(candidates)

    # ── Summary ───────────────────────────────────────────────────────────────
    auto   = sum(1 for c in candidates if c["recommended_action"] == "AUTO_MERGE")
    review = sum(1 for c in candidates if c["recommended_action"] == "REVIEW")

    print(f"\n{'═' * 50}")
    print(f"  FUZZY BASELINE RESULTS")
    print(f"{'═' * 50}")
    print(f"  CIA persons:       {len(cia_persons)}")
    print(f"  OFAC persons:      {len(ofac_persons)}")
    print(f"  Comparisons made:  {comparisons:,}")
    print(f"  Candidates found:  {len(candidates)}")
    print(f"  AUTO_MERGE:        {auto}")
    print(f"  REVIEW:            {review}")
    print(f"  Time:              {elapsed:.2f}s")
    print(f"{'═' * 50}\n")

    print(f"Results written to:")
    print(f"  {OUTPUT_JSON}")
    print(f"  {OUTPUT_CSV}")

    return candidates


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()