# ABI Healthcare Hackathon — Agent Instructions

## Project Overview
Build a wound care billing triage pipeline for Medicare Part B eligibility.
Pull patient data from a mock PointClickCare API, extract wound details from
clinical notes, and route each patient to `auto_accept`, `flag_for_review`,
or `reject` with a plain-English reason.

---

## Repo Structure
```
/
├── fast_fetcher.py   # Stage 1 — async API ingestion → saves to data/
├── fetcher.py        # Stage 1 alt — sync fetcher; also exports load_all_patient_data()
├── pipeline.py       # Stage 2+3 — extraction + routing → data/results.json (imports from fetcher.py)
├── dashboard.py      # Stage 4 — Streamlit biller dashboard
├── requirements.txt
└── data/             # Local cache (gitignored) — never re-fetch if file exists
    ├── all_patients.json
    ├── patients_facility_101.json
    ├── diagnoses_FA-001.json     # string patient_id
    ├── coverage_FA-001.json      # string patient_id
    ├── notes_1.json              # INTEGER id
    ├── assessments_1.json        # INTEGER id
    └── results.json              # final output
```

---

## Critical Rules — Read Before Touching Any Code

### Two patient identifiers — never mix them
| Identifier | Type | Used for |
|---|---|---|
| `patient_id` | string e.g. `FA-001` | `/diagnoses`, `/coverage` |
| `id` | integer e.g. `1` | `/notes`, `/assessments` |
Both are returned by `/pcc/patients`. Mixing them causes silent 422 errors.

### API rate limiting
Every request has a 30% chance of HTTP 429. Always use exponential backoff.
Never make a bare `requests.get()` — always go through `fetch()` or `fetch_with_retry()`.

### Cache-first, always
Before any fetch, check if the file exists in `data/`. If it does, load from disk.
Never re-fetch data that is already cached. This is what makes the pipeline fast
and re-runnable without burning time on retries.

### Extraction priority order
1. Structured assessment (`raw_json`) — most reliable
2. SOAP note — regex
3. Prose note — regex
4. Envive narrative — Claude API (slowest, last resort)

---

## Stage 1 — API Ingestion (`fast_fetcher.py`)

### Goal
Fetch all patients and their details from the mock PCC API. Cache everything to disk.

### API base URL
`https://hackathon.prod.pulsefoundry.ai`

### Endpoints
```
GET /health                                      → sanity check
GET /pcc/patients?facility_id=101                → list patients (also 102, 103)
GET /pcc/diagnoses?patient_id=FA-001             → ICD-10 diagnoses
GET /pcc/coverage?patient_id=FA-001              → insurance coverage
GET /pcc/notes?patient_id=1                      → free-text clinical notes
GET /pcc/assessments?patient_id=1                → structured wound assessments
```

### Tasks for agent
- [ ] Verify `/health` returns 200 before starting
- [ ] Fetch patients for facilities 101, 102, 103
- [ ] For each patient, fetch diagnoses + coverage (string ID) and notes + assessments (int ID)
- [ ] All 4 detail fetches per patient must fire concurrently (asyncio + aiohttp)
- [ ] Semaphore limit: 10 concurrent requests max
- [ ] Retry on 429 with exponential backoff: `wait = Retry-After * (2 ** attempt)`
- [ ] Save each response as a separate JSON file in `data/`
- [ ] Skip fetch entirely if cache file already exists
- [ ] Print progress every 20 patients
- [ ] **Bonus:** use `since=<ISO timestamp>` param on `/patients`, `/notes`, `/assessments` for incremental re-sync

### `since` parameter (incremental sync)
Supported on `/pcc/patients` (filters on `last_modified_at`), `/pcc/notes` (on `effective_date`),
and `/pcc/assessments` (on `assessment_date`). Not supported on `/diagnoses` or `/coverage`.
Use this for a second run to pick up only records changed since your last fetch.

### Expected output
```
data/patients_facility_101.json   (120 patients)
data/patients_facility_102.json   (90 patients)
data/patients_facility_103.json   (90 patients)
data/all_patients.json            (300 patients combined)
data/diagnoses_FA-001.json        (one per patient, string ID)
data/coverage_FA-001.json
data/notes_1.json                 (one per patient, integer ID)
data/assessments_1.json
... × 300 patients
```

---

## Stage 2 — Wound Data Extraction (`pipeline.py`)

### Goal
For each patient, extract structured wound fields from their notes and assessments.

### Fields to extract
| Field | Type | Notes |
|---|---|---|
| `wound_type` | string | pressure_ulcer, diabetic_foot_ulcer, venous_stasis_ulcer, arterial_ulcer, surgical_site_infection, abscess, burn |
| `stage` | string | 2, 3, 4, unstageable (pressure ulcers only) |
| `location` | string | Sacrum, heel, etc. |
| `length_cm` | float | |
| `width_cm` | float | |
| `depth_cm` | float | |
| `drainage` | string | none / light / moderate / heavy |

### Note formats — handle each differently
| Format | Detection | Strategy |
|---|---|---|
| SOAP | Has `Location:` and `Wound Type:` labels | Regex — fast, free |
| Prose | Has `Meas 4.2x3.1x1.5cm` shorthand pattern | Regex — fast, free |
| Envive | Pure narrative paragraph (no labeled fields) | Claude API — last resort |
| Multi-wound | Any format above describing 2+ wounds | Post-process: pick highest stage |

> **Multi-wound is not a separate format** — it's a characteristic that can appear
> inside SOAP, prose, or Envive notes. After extraction, if multiple wounds are
> found, keep the one with the highest stage number (or largest area if no stage).

### Extraction priority
```python
# 1. Try structured assessment first
for assessment in assessments:
    if assessment["status"] == "Complete" and assessment["is_current"]:
        result = parse_raw_json(assessment["raw_json"])
        if result: return result

# 2. Try SOAP and prose notes via regex
for note in soap_notes + prose_notes:
    result = extract_regex(note["note_text"])
    if result: return result

# 3. Fall back to Claude API for Envive notes
for note in envive_notes:
    result = extract_llm(note["note_text"])
    if result: return result

return None  # no data found → will become reject
```

### Claude API prompt for Envive notes
```
Extract wound care data from this clinical note.
Return ONLY valid JSON with these keys (use null if not found):
wound_type, stage, location, length_cm, width_cm, depth_cm, drainage

Drainage must be one of: none, light, moderate, heavy

Clinical note:
{note_text}

JSON only, no other text:
```

### Tasks for agent
- [ ] Implement `detect_format(note_text)` → "soap" | "prose" | "envive"
- [ ] Implement `extract_regex(note_text)` for SOAP and prose
- [ ] Implement `extract_from_assessment(assessment)` parsing `raw_json`
- [ ] Implement `extract_llm(note_text)` using Claude API (`claude-sonnet-4-6`)
- [ ] Handle multi-wound notes: pick the wound with highest stage number
- [ ] Mark Envive extractions with `low_confidence: True`
- [ ] Never crash on malformed data — catch all exceptions, return None

---

## Stage 3 — Eligibility Routing (`pipeline.py`)

### Goal
For each patient, apply the routing decision tree and produce one output row.

### Decision tree (evaluate in order — first match wins)

```
Gate 1: primary_payer_code == "MCB"?
  NO  → reject: "Not Medicare Part B"

Gate 2: Active MCB coverage? (effective_to is null OR future date)
  NO  → reject: "Medicare Part B coverage expired or inactive"

Gate 3: Active wound ICD-10 diagnosis?
  ICD-10 prefixes: L89, E10, E11, I83, L97, L98, T14, L03, T20, T21, T22, T23, T24, T25, T81
  clinical_status must be "active"
  NO  → reject: "No active qualifying wound diagnosis"

Gate 4: Clinical documentation exists? (at least 1 note or assessment)
  NO  → reject: "No clinical notes or assessments found"

Gate 5: Extraction succeeded? (no error key in extraction result)
  NO  → reject: "Extraction failed: {error}"

Gate 6: All measurements present? (wound_type, location, length_cm, width_cm, depth_cm, drainage)
  NO  → flag_for_review: "Missing fields: {list of missing}"

Gate 7: High confidence extraction? (not Envive format, not low_confidence)
  NO  → flag_for_review: "Narrative note — verify extracted values"

PASS → auto_accept: "All required fields documented and verified"
```

### Output row schema
```python
{
    "patient_id":   str,    # e.g. "FA-001"
    "name":         str,    # "Agnes Dunbar"
    "facility_id":  int,    # 101 | 102 | 103
    "payer":        str,    # "MCB"
    "decision":     str,    # "auto_accept" | "flag_for_review" | "reject"
    "reason":       str,    # plain English for biller
    "wound_type":   str,
    "stage":        str,
    "location":     str,
    "length_cm":    float,
    "width_cm":     float,
    "depth_cm":     float,
    "drainage":     str,
    "source":       str,    # "assessment" | "note_regex" | "envive" (llm extraction sets source_format not source)
}
```

### Tasks for agent
- [ ] Implement `route(patient, coverage, diagnoses, extraction)` → (decision, reason)
- [ ] Check coverage dates correctly: `effective_to is None` means ACTIVE
- [ ] Accept any ICD-10 starting with wound prefixes above
- [ ] Save all rows to `data/results.json`
- [ ] Print summary counts at end

---

## Stage 4 — Biller Dashboard (`dashboard.py`)

### Goal
A Streamlit UI that a non-technical biller can read at a glance.

### Requirements
- [ ] Load `data/results.json` on startup
- [ ] Show 4 summary metrics: total / auto_accept / flag_for_review / reject
- [ ] Color-coded table: green = auto_accept, yellow = flag_for_review, red = reject
- [ ] Filters: decision type, facility, free-text patient search
- [ ] Sort order: auto_accept first, then flag, then reject
- [ ] Patient detail panel: click a patient to see all extracted fields
- [ ] No network calls — reads only from disk

### Run command
```bash
streamlit run dashboard.py
```

---

## How to Run Everything

```bash
# 0. Install deps
pip install aiohttp requests anthropic pandas streamlit

# 1. Fetch all data — use fast_fetcher for speed (~2-4 min)
python fast_fetcher.py
# Note: fast_fetcher.py and fetcher.py both populate the same data/ directory.
# pipeline.py imports load_all_patient_data() from fetcher.py (sync helper).
# Run either fetcher — the data/ cache format is identical.

# 2. Run extraction + routing
python pipeline.py

# 3. Launch biller dashboard
streamlit run dashboard.py
```

---

## Environment Variables
```bash
export ANTHROPIC_API_KEY=your_key_here  # required for Envive note extraction
```

---

## Payer Codes Reference
| Code | Meaning | Eligible for this pipeline |
|---|---|---|
| `MCB` | Medicare Part B | YES — primary target |
| `MCA` | Medicare Part A | NO |
| `MCD` | Medicaid | NO |
| `HMO` | HMO / Managed Care | NO |

---

## Do Not
- Do not re-fetch data that exists in `data/`
- Do not use bare `requests.get()` without retry logic
- Do not crash on null fields — every field in the API can be null
- Do not mix string `patient_id` with integer `id`
- Do not send more than 10 concurrent requests
- Do not modify files in `data/` manually
