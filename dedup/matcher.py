"""
dedup/matcher.py
================
Final deduplication matcher — weighted multi-signal approach.

Identifies persons appearing on both the CIA world leaders list and the
OFAC sanctions list.  A sanctioned head of state would appear on both.

APPROACH:
    1. Blocking — build an inverted index on normalised OFAC name tokens,
       including all aliases.  Only pairs sharing at least one token are
       compared.  Avoids naive 5273 × 7506 = 39M comparison problem.

       Aliases are indexed because OFAC stores 4,317 persons with a.k.a.
       entries.  Without alias indexing, a person whose primary name doesn't
       match but whose alias does would never be a candidate.
       Example: Lukashenko's family members have alias entries that would
       otherwise be missed.

    2. Transliteration normalisation — common Arabic/cross-language name
       variants mapped to canonical forms before comparison.
       e.g. MOHAMMED / MOHAMED / MOHAMAD → MUHAMMAD
       e.g. LUKASHENKA (Belarusian) → LUKASHENKO (Russian)

    3. Three-signal weighted scoring:
         best name similarity  60%  — highest score across all name+alias pairs
         country match         30%  — ISO alpha-2 code comparison
         position overlap      10%  — shared tokens in position strings

       Using best score across all aliases means a CIA person matching an
       OFAC alias scores as well as matching the primary name.

    4. Thresholds:
         name_score == 100      → AUTO_MERGE (identical name, always merge)
         combined   >= 85       → AUTO_MERGE
         combined   >= 65       → REVIEW
         combined   <  65       → ignored

    COMPARISON AGAINST BASELINE (matcher_fuzzy.py):
    Method              | Candidates | AUTO_MERGE | REVIEW | Comparisons | Time
    --------------------|------------|------------|--------|-------------|------
    Fuzzy baseline      | 2,518      | 82         | 2,436  | 220,885     | 1.04s
    Weighted (no alias) | 4,209      | 84         | 4,125  | 302,040     | 3.02s
    Weighted + aliases  | 5,945      | 117        | 5,828  | 423,403     | 9.94s

    Fixes applied after demotion analysis:
      - name=100 override: identical names always AUTO_MERGE (fixed 6 pairs)
      - rehman/haydar added to transliteration map (fixed 3 pairs)
      - lukashenka → lukashenko added (fixed Belarusian romanisation)
      - alias indexing and scoring added (improved recall)

KNOWN LIMITATIONS:
    - Zero token overlap pairs never reach scoring (QADDAFI vs GADDAFI)
      even with transliteration map — they share no tokens at all
    - Transliteration map covers common variants only, not exhaustive
    - 21% of OFAC persons have no nationality — country signal unavailable
    - Position strings rarely share tokens across sources
    - Kim Jong Un stored as "Jong Un Kim" in CIA (Korean name order) —
      token "un" filtered by 2-char minimum, reducing match confidence

OUTPUT:
    dedup/dedup_report.csv   — all candidates above REVIEW threshold
    dedup/dedup_results.json — same data as JSON for loader

Run:
    python dedup/matcher.py
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
OUTPUT_JSON  = "dedup/dedup_results.json"
OUTPUT_CSV   = "dedup/dedup_report.csv"

# ─── Thresholds ───────────────────────────────────────────────────────────────

THRESHOLD_AUTO   = 85   # combined score >= this → AUTO_MERGE
THRESHOLD_REVIEW = 65   # combined score >= this → REVIEW

# ─── Transliteration Map ──────────────────────────────────────────────────────
# Maps variant spellings to canonical forms before fuzzy comparison.
# Tokens not in this map are left as-is.
#
# Limitation: purely transliterated pairs with zero token overlap
# (e.g. QADDAFI vs GADDAFI) are never candidates regardless of this map
# because blocking never pairs them. Documented in responses.md.

TRANSLITERATION_MAP = {
    # Muhammad variants
    "mohammed":   "muhammad",
    "mohamed":    "muhammad",
    "mohamad":    "muhammad",
    "mohammad":   "muhammad",
    "muhammed":   "muhammad",
    "mehmet":     "muhammad",   # Turkish form

    # Abdullah variants
    "abdallah":   "abdullah",
    "abdalla":    "abdullah",
    "abdollah":   "abdullah",

    # Ali variants
    "aly":        "ali",

    # Hassan variants
    "hasan":      "hassan",

    # Hussein variants
    "husain":     "hussein",
    "husayn":     "hussein",
    "hossein":    "hussein",

    # Omar variants
    "umar":       "omar",
    "omer":       "omar",

    # Osama variants
    "usama":      "osama",

    # Rahman variants — added after demotion analysis
    "rehman":     "rahman",

    # Haidar variants — added after demotion analysis
    "haydar":     "haidar",

    # Lukashenko — Belarusian romanisation vs Russian romanisation
    # OFAC uses "Lukashenka", CIA uses "Lukashenko" — zero token overlap
    # without this entry. Discovered during recall evaluation.
    "lukashenka": "lukashenko",

    # Qaddafi variants
    "qaddafi":    "gaddafi",
    "qadhafi":    "gaddafi",
    "kaddafi":    "gaddafi",
    "kadafi":     "gaddafi",
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


def get_all_names(entity: dict) -> list[str]:
    """
    Return all name strings for an entity — primary name plus all aliases.

    Used for both blocking index construction and scoring.  Indexing aliases
    means a CIA person can match an OFAC record via an alias token even if
    the primary names don't share any tokens.

    OFAC has 4,317 persons with aliases.  Without this function, all of those
    alias-only matches would be missed entirely.

    Args:
        entity: FtM Person dict.

    Returns:
        List of name strings, all lowercased and stripped.
        First entry is always the primary name (if present).
    """
    names   = []
    primary = get_name(entity)
    if primary:
        names.append(primary)

    for alias in entity.get("properties", {}).get("alias", []):
        cleaned = alias.lower().strip()
        if cleaned and cleaned not in names:
            names.append(cleaned)

    return names


def normalize_name(name: str) -> str:
    """
    Apply transliteration normalisation to a name string.

    Lowercases, replaces hyphens/commas with spaces, maps each token
    through TRANSLITERATION_MAP. Unmapped tokens left as-is.

    Args:
        name: Raw name string.

    Returns:
        Normalised name with variants replaced by canonical forms.

    Example:
        "MOHAMMED Al-Zawahiri" → "muhammad al zawahiri"
        "Lukashenka"           → "lukashenko"
    """
    cleaned = name.lower().replace("-", " ").replace(",", " ")
    tokens  = cleaned.split()
    mapped  = [TRANSLITERATION_MAP.get(t, t) for t in tokens]
    return " ".join(mapped)


def tokenize(name: str) -> set[str]:
    """
    Split a name into tokens for blocking index lookup.

    Args:
        name: Name string, already lowercased.

    Returns:
        Set of token strings, minimum length 2.
    """
    cleaned = name.replace("-", " ").replace(",", " ")
    return {t for t in cleaned.split() if len(t) >= 2}


def get_position_tokens(entity: dict) -> set[str]:
    """
    Extract significant tokens from an entity's position field.

    Filters tokens shorter than 4 chars to avoid false positives
    from common words like "of", "the", "for", "and".

    Args:
        entity: FtM Person dict.

    Returns:
        Set of significant position token strings.
    """
    props     = entity.get("properties", {})
    positions = props.get("position", [])
    tokens: set[str] = set()
    for pos in positions:
        for token in pos.lower().split():
            if len(token) >= 4:
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
    Build an inverted token → OFAC record index.

    Indexes on ALL names (primary + aliases) after transliteration
    normalisation.  This means:
      - Alias variants surface matches the primary name alone would miss
      - Transliteration variants of aliases are also covered

    Args:
        ofac_persons: List of normalised OFAC Person dicts.

    Returns:
        Dict of token → list of indices into ofac_persons.
    """
    index: dict[str, list[int]] = defaultdict(list)

    for i, person in enumerate(ofac_persons):
        for name in get_all_names(person):       # primary + all aliases
            norm_name = normalize_name(name)
            tokens    = tokenize(norm_name)
            for token in tokens:
                index[token].append(i)

    return dict(index)


# ─── Scoring ──────────────────────────────────────────────────────────────────

def best_name_score(cia_name_norm: str, ofac: dict) -> tuple[float, str]:
    """
    Score a CIA name against all OFAC names (primary + aliases).

    Returns the highest score found and the OFAC name that produced it.
    Using the best score means a CIA person matching an OFAC alias scores
    as well as matching the primary name — neither is privileged.

    Args:
        cia_name_norm: Transliteration-normalised CIA name.
        ofac:          OFAC Person dict.

    Returns:
        Tuple of (best_score, best_matching_ofac_name).
    """
    best_score = 0.0
    best_name  = get_name(ofac)
    cia_tokens = cia_name_norm.split()

    for name in get_all_names(ofac):
        norm       = normalize_name(name)
        ofac_tokens = norm.split()

        sort_score = fuzz.token_sort_ratio(cia_name_norm, norm)

        # Only apply token_set_ratio when:
        # 1. CIA name has at least 2 tokens (not a mononym)
        # 2. OFAC name is longer by 1-2 tokens only (patronymic case)
        # 3. Not when OFAC name is much longer — that's a different person
        token_diff = len(ofac_tokens) - len(cia_tokens)
        use_set = len(cia_tokens) >= 2 and token_diff == 1

        if use_set:
            score = max(sort_score, fuzz.token_set_ratio(cia_name_norm, norm))
        else:
            score = sort_score

        if score > best_score:
            best_score = score
            best_name  = name

    return best_score, best_name


def score_pair(
    cia: dict,
    ofac: dict,
    cia_name_norm: str,
) -> tuple[float, float, float, float, str, str]:
    """
    Score a CIA/OFAC candidate pair across three signals.

    Weights: name 60%, country 30%, position 10%.
    Combined score range: 0–100.

    Name score uses the best match across all OFAC names and aliases,
    not just the primary name.

    Args:
        cia:           CIA Person dict.
        ofac:          OFAC Person dict.
        cia_name_norm: Transliteration-normalised CIA name.

    Returns:
        Tuple of (name_score, country_score, position_score,
                  combined_score, match_signals_string,
                  best_matching_ofac_name).
    """
    # Signal 1 — best name similarity across all OFAC names + aliases
    name_score, matched_ofac_name = best_name_score(cia_name_norm, ofac)

    # Signal 2 — country match
    cia_countries  = get_country_codes(cia,  "cia")
    ofac_countries = get_country_codes(ofac, "ofac")
    country_match  = 1.0 if cia_countries & ofac_countries else 0.0

    # Signal 3 — position token overlap
    cia_pos_tokens  = get_position_tokens(cia)
    ofac_pos_tokens = get_position_tokens(ofac)
    shared_pos      = cia_pos_tokens & ofac_pos_tokens
    position_match  = 1.0 if shared_pos else 0.0

    # Weighted combined score
    combined = (name_score * 0.60) + (country_match * 30) + (position_match * 10)

    # Human-readable match signals for the report column
    signals = [f"name={name_score:.0f}"]
    if matched_ofac_name != get_name(ofac):
        signals.append(f"matched_via_alias='{matched_ofac_name}'")
    if country_match:
        signals.append(f"country={sorted(cia_countries & ofac_countries)}")
    if shared_pos:
        signals.append(f"position_overlap={sorted(shared_pos)[:2]}")
    match_signals = "; ".join(signals)

    return name_score, country_match, position_match, combined, match_signals, matched_ofac_name


def recommend(combined: float, name_score: float) -> str:
    """
    Map scores to a recommended action.

    Name score = 100 overrides everything — identical names always merge
    regardless of missing country/position signals.  This fixes the case
    where correct pairs were demoted because OFAC had no nationality data.

    Args:
        combined:   Weighted combined score (0–100).
        name_score: Raw name similarity score (0–100).

    Returns:
        "AUTO_MERGE", "REVIEW", or "NO_MATCH".
    """
    if name_score >= 100:
        return "AUTO_MERGE"   # identical name — always merge
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

    # ── Build blocking index on all OFAC names including aliases ───────────────
    print("Building OFAC blocking index (primary names + aliases) ...")
    ofac_index = build_ofac_index(ofac_persons)
    print(f"  {len(ofac_index)} unique tokens indexed\n")

    # ── Score candidate pairs ─────────────────────────────────────────────────
    print("Scoring candidate pairs ...")
    candidates  = []
    comparisons = 0
    seen_pairs: set[tuple[str, str]] = set()

    for cia in cia_persons:
        cia_name_raw  = get_name(cia)
        cia_name_norm = normalize_name(cia_name_raw)
        cia_tokens    = tokenize(cia_name_norm)

        if not cia_tokens:
            continue

        # Get candidate OFAC indices via shared tokens
        candidate_indices: set[int] = set()
        for token in cia_tokens:
            candidate_indices.update(ofac_index.get(token, []))

        for idx in candidate_indices:
            ofac = ofac_persons[idx]

            # Deduplicate — same pair can appear via multiple shared tokens
            pair_key = (cia["id"], ofac["id"])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            comparisons += 1

            name_score, country_score, position_score, combined, signals, matched_name = score_pair(
                cia, ofac, cia_name_norm
            )

            action = recommend(combined, name_score)
            if action == "NO_MATCH":
                continue

            candidates.append({
                "cia_id":             cia["id"],
                "ofac_id":            ofac["id"],
                "cia_name":           cia_name_raw,
                "ofac_name":          get_name(ofac),
                "ofac_matched_name":  matched_name,   # may be an alias
                "cia_country":        cia.get("country", ""),
                "ofac_nationality":   (ofac["properties"].get("nationality") or [""])[0],
                "name_score":         round(name_score,   2),
                "country_match":      int(country_score),
                "position_match":     int(position_score),
                "combined_score":     round(combined,     2),
                "match_signals":      signals,
                "recommended_action": action,
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
    alias_matches = sum(
        1 for c in candidates
        if c["ofac_matched_name"] != c["ofac_name"]
    )

    print(f"\n{'═' * 50}")
    print(f"  DEDUP RESULTS — WEIGHTED MATCHER")
    print(f"{'═' * 50}")
    print(f"  CIA persons:         {len(cia_persons)}")
    print(f"  OFAC persons:        {len(ofac_persons)}")
    print(f"  Comparisons made:    {comparisons:,}")
    print(f"  Candidates found:    {len(candidates)}")
    print(f"  AUTO_MERGE:          {auto}")
    print(f"  REVIEW:              {review}")
    print(f"  Matched via alias:   {alias_matches}")
    print(f"  Time:                {elapsed:.2f}s")
    print(f"{'═' * 50}\n")
    print(f"Results written to:")
    print(f"  {OUTPUT_JSON}")
    print(f"  {OUTPUT_CSV}")

    return candidates


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run()