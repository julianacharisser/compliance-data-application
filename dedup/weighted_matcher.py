"""
dedup/weighted_matcher.py
=========================
File 2 of 2 — Weighted multi-signal matcher. This is the main approach.

Extends the fuzzy baseline with:
    - Transliteration normalisation for Arabic/common name variants
    - Three-signal weighted scoring (name + country + position)
    - Richer match_signals column in the report for explainability

SCORING:
    name_score    × 0.60  — primary signal, token_sort_ratio
    country_match × 0.30  — ISO alpha-2 code comparison
    position_match× 0.10  — shared token overlap between position strings

    combined = (name × 60) + (country × 30) + (position × 10)
    range: 0–100

THRESHOLDS:
    >= 85  AUTO_MERGE   — all three signals agree
    >= 65  REVIEW       — name matches but supporting signals weak
    <  65  no output

TRANSLITERATION:
    Common Arabic name variants mapped to canonical form before comparing.
    e.g. MOHAMMED / MOHAMED / MOHAMAD → MUHAMMAD
    This boosts name scores for transliteration variants that fuzzy
    matching alone would score ~70 (borderline) into confident territory.
    Documented limitation: only covers common variants, not exhaustive.

OUTPUT:
    dedup/weighted_results.json
    dedup/weighted_report.csv    ← this is the main dedup_report.csv

Run:
    python dedup/weighted_matcher.py
"""

import csv
import json
import os
import time
from collections import defaultdict

from rapidfuzz import fuzz

# ─── Paths ────────────────────────────────────────────────────────────────────

CIA_PATH     = "transform/cia_normalized.json"
OFAC_PATH    = "transform/ofac_normalized.json"
OUTPUT_JSON  = "dedup/weighted_results.json"
OUTPUT_CSV   = "dedup/weighted_report.csv"

# ─── Thresholds ───────────────────────────────────────────────────────────────

THRESHOLD_AUTO   = 85   # >= this → AUTO_MERGE
THRESHOLD_REVIEW = 65   # >= this → REVIEW

# ─── Transliteration Map ──────────────────────────────────────────────────────
# Maps common variant spellings to a single canonical form.
# Applied before fuzzy comparison so variants score higher.
#
# Sources: Arabic romanisation variants, common Western database differences.
# Limitation: not exhaustive — purely transliterated names with zero token
# overlap (e.g. QADDAFI vs GADDAFI) won't be candidates at the blocking stage
# regardless of this map, because they share no tokens.

TRANSLITERATION_MAP = {
    # Muhammad variants
    "mohammed":  "muhammad",
    "mohamed":   "muhammad",
    "mohamad":   "muhammad",
    "mohammad":  "muhammad",
    "muhammed":  "muhammad",
    "mehmet":    "muhammad",   # Turkish form

    # Abdullah variants
    "abdallah":  "abdullah",
    "abdalla":   "abdullah",
    "abdollah":  "abdullah",

    # Ali variants
    "aly":       "ali",

    # Hassan variants
    "hasan":     "hassan",

    # Hussein variants
    "husain":    "hussein",
    "husayn":    "hussein",
    "hossein":   "hussein",

    # Omar variants
    "umar":      "omar",
    "omer":      "omar",

    # Osama variants
    "usama":     "osama",

    # Common prefix variants
    "al":        "al",   # already canonical, but normalise spacing issues
    "bin":       "bin",
    "bint":      "bint",

    # Qaddafi — note: blocking will miss QADDAFI vs GADDAFI entirely
    # because they share no tokens. Documented in responses.md.
    "qaddafi":   "gaddafi",
    "qadhafi":   "gaddafi",
    "kaddafi":   "gaddafi",
    "kadafi":    "gaddafi",
}


# ─── Name Helpers ─────────────────────────────────────────────────────────────

def get_name(entity: dict) -> str:
    """
    Extract the primary display name from an FtM entity.

    Tries `name` property first, falls back to firstName + lastName join.

    Args:
        entity: FtM Person dict.

    Returns:
        Name string, lowercased and stripped.
    """
    props     = entity.get("properties", {})
    name_list = props.get("name", [])
    if name_list:
        return name_list[0].lower().strip()

    first = (props.get("firstName") or [""])[0]
    last  = (props.get("lastName")  or [""])[0]
    return f"{first} {last}".lower().strip()


def normalize_name(name: str) -> str:
    """
    Apply transliteration normalisation to a name string.

    Lowercases, replaces hyphens with spaces, then maps each token
    through TRANSLITERATION_MAP. Unmapped tokens are left as-is.

    Args:
        name: Raw name string.

    Returns:
        Normalised name string with variants replaced by canonical forms.

    Example:
        "MOHAMMED Al-Zawahiri" → "muhammad al zawahiri"
    """
    cleaned = name.lower().replace("-", " ").replace(",", " ")
    tokens  = cleaned.split()
    mapped  = [TRANSLITERATION_MAP.get(t, t) for t in tokens]
    return " ".join(mapped)


def tokenize(name: str) -> set[str]:
    """
    Split a name into tokens for blocking index lookup.

    Args:
        name: Display name string (already lowercased).

    Returns:
        Set of token strings, min length 2.
    """
    cleaned = name.replace("-", " ").replace(",", " ")
    return {t for t in cleaned.split() if len(t) >= 2}


def get_position_tokens(entity: dict) -> set[str]:
    """
    Extract significant tokens from an entity's position field.

    Used for position overlap scoring. Short tokens and common words
    are filtered out to avoid false positives ("of", "the", "for").

    Args:
        entity: FtM Person dict.

    Returns:
        Set of significant position tokens (length >= 4).
    """
    props     = entity.get("properties", {})
    positions = props.get("position", [])
    tokens: set[str] = set()
    for pos in positions:
        for token in pos.lower().split():
            if len(token) >= 4:   # filters out "of", "the", "for", "and"
                tokens.add(token)
    return tokens


def get_country_codes(entity: dict, source: str) -> set[str]:
    """
    Get ISO alpha-2 country codes from an entity.

    CIA stores country_code as top-level metadata.
    OFAC stores nationality as a list inside properties.

    Args:
        entity: FtM Person dict.
        source: "cia" or "ofac".

    Returns:
        Set of ISO alpha-2 code strings.
    """
    if source == "cia":
        code = entity.get("country_code", "")
        return {code.lower()} if code else set()
    else:
        codes = entity["properties"].get("nationality", [])
        return {c.lower() for c in codes if c}


# ─── Blocking Index ───────────────────────────────────────────────────────────

def build_ofac_index(ofac_persons: list[dict]) -> dict[str, list[int]]:
    """
    Build an inverted token → OFAC index positions map.

    Indexes on normalised name tokens (after transliteration) so that
    variant spellings of the same name token map to the same bucket.

    Args:
        ofac_persons: List of normalised OFAC Person dicts.

    Returns:
        Dict of token → list of indices into ofac_persons.
    """
    index: dict[str, list[int]] = defaultdict(list)

    for i, person in enumerate(ofac_persons):
        raw_name  = get_name(person)
        norm_name = normalize_name(raw_name)
        tokens    = tokenize(norm_name)
        for token in tokens:
            index[token].append(i)

    return dict(index)


# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_pair(
    cia: dict,
    ofac: dict,
    cia_name_norm: str,
    ofac_name_norm: str,
) -> tuple[float, float, float, float, str]:
    """
    Score a CIA/OFAC candidate pair across three signals.

    Signals:
        name_score     — rapidfuzz token_sort_ratio on normalised names
        country_score  — 1.0 if any country code overlaps, else 0.0
        position_score — 1.0 if any position token overlaps, else 0.0

    Weights:
        name     60%
        country  30%
        position 10%

    Args:
        cia:            CIA Person dict.
        ofac:           OFAC Person dict.
        cia_name_norm:  Transliteration-normalised CIA name.
        ofac_name_norm: Transliteration-normalised OFAC name.

    Returns:
        Tuple of (name_score, country_score, position_score,
                  combined_score, match_signals_string).
    """
    # Signal 1: name similarity
    name_score = fuzz.token_sort_ratio(cia_name_norm, ofac_name_norm)

    # Signal 2: country match
    cia_countries  = get_country_codes(cia,  "cia")
    ofac_countries = get_country_codes(ofac, "ofac")
    country_match  = 1.0 if cia_countries & ofac_countries else 0.0

    # Signal 3: position token overlap
    cia_pos_tokens  = get_position_tokens(cia)
    ofac_pos_tokens = get_position_tokens(ofac)
    shared_pos      = cia_pos_tokens & ofac_pos_tokens
    position_match  = 1.0 if shared_pos else 0.0

    # Weighted combined score
    combined = (name_score * 0.60) + (country_match * 30) + (position_match * 10)

    # Human-readable match signals for the report
    signals = []
    signals.append(f"name={name_score:.0f}")
    if country_match:
        signals.append(f"country={sorted(cia_countries & ofac_countries)}")
    if shared_pos:
        signals.append(f"position_overlap={sorted(shared_pos)[:2]}")  # first 2 only
    match_signals = "; ".join(signals)

    return name_score, country_match, position_match, combined, match_signals


def recommend(combined: float) -> str:
    """Map a combined score to a recommended action string."""
    if combined >= THRESHOLD_AUTO:
        return "AUTO_MERGE"
    if combined >= THRESHOLD_REVIEW:
        return "REVIEW"
    return "NO_MATCH"


# ─── Main Matcher ─────────────────────────────────────────────────────────────

def run() -> list[dict]:
    """
    Load both normalized files, score candidate pairs, write output.

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

    # ── Build blocking index on normalised OFAC names ─────────────────────────
    print("Building OFAC blocking index (with transliteration) ...")
    ofac_index = build_ofac_index(ofac_persons)
    print(f"  {len(ofac_index)} unique tokens indexed\n")

    # ── Compare ───────────────────────────────────────────────────────────────
    print("Scoring candidate pairs ...")
    candidates  = []
    comparisons = 0
    seen_pairs: set[tuple[str, str]] = set()   # avoid scoring same pair twice

    for cia in cia_persons:
        cia_name_raw  = get_name(cia)
        cia_name_norm = normalize_name(cia_name_raw)
        cia_tokens    = tokenize(cia_name_norm)

        if not cia_tokens:
            continue

        # Candidate OFAC indices via blocking index
        candidate_indices: set[int] = set()
        for token in cia_tokens:
            candidate_indices.update(ofac_index.get(token, []))

        for idx in candidate_indices:
            ofac = ofac_persons[idx]

            # Skip if we've already scored this pair (can happen with
            # multiple shared tokens pointing to the same OFAC record)
            pair_key = (cia["id"], ofac["id"])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            ofac_name_raw  = get_name(ofac)
            ofac_name_norm = normalize_name(ofac_name_raw)
            comparisons   += 1

            name_score, country_score, position_score, combined, signals = score_pair(
                cia, ofac, cia_name_norm, ofac_name_norm
            )

            if combined < THRESHOLD_REVIEW:
                continue

            candidates.append({
                "cia_id":           cia["id"],
                "ofac_id":          ofac["id"],
                "cia_name":         cia_name_raw,
                "ofac_name":        ofac_name_raw,
                "cia_country":      cia.get("country", ""),
                "ofac_nationality": (
                    ofac["properties"].get("nationality") or [""]
                )[0],
                "name_score":       round(name_score,    2),
                "country_match":    int(country_score),
                "position_match":   int(position_score),
                "combined_score":   round(combined,      2),
                "match_signals":    signals,
                "recommended_action": recommend(combined),
            })

    elapsed = time.time() - start

    # Sort by combined score descending
    candidates.sort(key=lambda x: x["combined_score"], reverse=True)

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
    print(f"  WEIGHTED MATCHER RESULTS")
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