#!/usr/bin/env python3
"""Classify wound diagnoses and map notes to active wound diagnoses."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv()
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

WOUND_TYPE_PATTERNS = (
    ("pressure_ulcer", re.compile(r"\bpressure\s+ulcer\b|decubitus|bedsore|\bL89\.", re.I)),
    ("diabetic_foot_ulcer", re.compile(r"diabetic.*(?:foot|ulcer)|\bE11\.62[12]\b", re.I)),
    ("venous_stasis_ulcer", re.compile(r"venous.*ulcer|stasis\s+ulcer|\bI83\.", re.I)),
    ("arterial_ulcer", re.compile(r"arterial.*ulcer|\bI70\.23", re.I)),
    ("surgical_site_infection", re.compile(r"surgical\s+site\s+infection|\bT81\.3", re.I)),
    ("abscess", re.compile(r"\babscess\b|\bL02\.", re.I)),
    ("burn", re.compile(r"\bburn\b|\bT(?:2[0-9]|3[0-2])\.", re.I)),
)

LOCATION_ALIASES = {
    "sacrum": ["sacrum", "sacral"],
    "right hip": ["right hip", "r hip"],
    "left hip": ["left hip", "l hip"],
    "right buttock": ["right buttock", "rightbuttock", "r buttock"],
    "left buttock": ["left buttock", "leftbuttock", "l buttock"],
    "left foot": ["left foot", "leftfoot", "l foot"],
    "right foot": ["right foot", "rightfoot", "r foot"],
    "right plantar": ["right plantar", "rightplantar", "r plantar"],
    "left plantar": ["left plantar", "leftplantar", "l plantar"],
    "left lower leg": ["left lower leg", "leftlowerleg", "l lower leg"],
    "right lower leg": ["right lower leg", "rightlowerleg", "r lower leg"],
    "left lower extremity": ["left lower extremity", "leftlowerextremity", "l lower extremity"],
    "right lower extremity": ["right lower extremity", "rightlowerextremity", "r lower extremity"],
    "abdominal wall": ["abdominal wall", "abdominalwall", "abdominal"],
    "left upper arm": ["left upper arm", "leftupperarm", "l upper arm"],
    "right upper arm": ["right upper arm", "rightupperarm", "r upper arm"],
    "right cervical": ["right cervical", "rightcervical", "r cervical"],
    "left cervical": ["left cervical", "leftcervical", "l cervical"],
}

NOTE_WOUND_TERMS = {
    "pressure_ulcer": ["pressure ulcer", "stage 2", "stage 3", "stage 4", "unstageable"],
    "diabetic_foot_ulcer": ["diabetic"],
    "venous_stasis_ulcer": ["venous"],
    "arterial_ulcer": ["arterial"],
    "surgical_site_infection": ["surgical"],
    "abscess": ["abscess"],
    "burn": ["burn"],
}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


def latest_snapshot(raw_dir: Path) -> Path:
    snapshots = sorted(path for path in raw_dir.iterdir() if path.is_dir())
    if not snapshots:
        raise FileNotFoundError(f"No snapshots found under {raw_dir}")
    return snapshots[-1]


def load_patients(snapshot_dir: Path) -> list[dict[str, Any]]:
    return json.loads((snapshot_dir / "all_patients.json").read_text(encoding="utf-8"))


def deterministic_wound_classification(diagnosis: dict[str, Any]) -> dict[str, Any]:
    code = diagnosis.get("icd10_code") or ""
    description = diagnosis.get("icd10_description") or ""
    text = f"{code} {description}"
    for wound_type, pattern in WOUND_TYPE_PATTERNS:
        if pattern.search(text):
            return {
                "is_wound": True,
                "wound_type": wound_type,
                "confidence": 0.95,
                "reason": f"Matched wound pattern for {wound_type}.",
                "source": "deterministic",
            }
    return {
        "is_wound": False,
        "wound_type": None,
        "confidence": 0.95,
        "reason": "No wound-related ICD-10 description or code pattern found.",
        "source": "deterministic",
    }


def classify_with_llm(
    diagnoses: list[dict[str, Any]],
    model: str,
    timeout_seconds: float,
) -> dict[str, dict[str, Any]]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    compact_diagnoses = [
        {
            "icd10_code": dx.get("icd10_code"),
            "icd10_description": dx.get("icd10_description"),
        }
        for dx in diagnoses
    ]

    prompt = {
        "task": "Classify whether each ICD-10 diagnosis corresponds to an active wound condition for Medicare Part B wound-care triage.",
        "allowed_wound_types": [
            "pressure_ulcer",
            "diabetic_foot_ulcer",
            "venous_stasis_ulcer",
            "arterial_ulcer",
            "surgical_site_infection",
            "abscess",
            "burn",
            "other_wound",
        ],
        "rules": [
            "Return is_wound=false for chronic conditions that are not wounds, such as hypertension, dementia, COPD, osteoporosis, hyperlipidemia, CKD, or uncomplicated diabetes.",
            "Use the ICD-10 description primarily; use the ICD-10 code as supporting context.",
            "Return one result per input diagnosis.",
        ],
        "diagnoses": compact_diagnoses,
    }

    body = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": "You are a clinical data extraction assistant. Return only JSON.",
            },
            {
                "role": "user",
                "content": json.dumps(prompt),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "wound_diagnosis_classifications",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "icd10_code": {"type": "string"},
                                    "is_wound": {"type": "boolean"},
                                    "wound_type": {
                                        "type": ["string", "null"],
                                        "enum": [
                                            "pressure_ulcer",
                                            "diabetic_foot_ulcer",
                                            "venous_stasis_ulcer",
                                            "arterial_ulcer",
                                            "surgical_site_infection",
                                            "abscess",
                                            "burn",
                                            "other_wound",
                                            None,
                                        ],
                                    },
                                    "confidence": {"type": "number"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["icd10_code", "is_wound", "wound_type", "confidence", "reason"],
                            },
                        }
                    },
                    "required": ["results"],
                },
            }
        },
    }

    request = Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urlopen(request, timeout=timeout_seconds) as response:
        raw_response = json.loads(response.read().decode("utf-8"))

    text = raw_response.get("output_text")
    if not text:
        text_parts: list[str] = []
        for item in raw_response.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    text_parts.append(content.get("text", ""))
        text = "".join(text_parts)

    parsed = json.loads(text)
    return {
        result["icd10_code"]: {
            "is_wound": result["is_wound"],
            "wound_type": result["wound_type"],
            "confidence": result["confidence"],
            "reason": result["reason"],
            "source": "llm",
        }
        for result in parsed["results"]
    }


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def infer_location(text: str) -> str | None:
    normalized = normalize_text(text)
    for canonical, aliases in LOCATION_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            return canonical
    return None


def note_score_for_diagnosis(note_text: str, diagnosis: dict[str, Any], classification: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    normalized_note = normalize_text(note_text)
    wound_type = classification.get("wound_type")

    if wound_type:
        terms = NOTE_WOUND_TERMS.get(wound_type, [])
        if any(term in normalized_note for term in terms):
            score += 4
            reasons.append(f"note mentions {wound_type.replace('_', ' ')}")

    diagnosis_location = infer_location(diagnosis.get("icd10_description") or "")
    if diagnosis_location:
        aliases = LOCATION_ALIASES[diagnosis_location]
        if any(alias in normalized_note for alias in aliases):
            score += 5
            reasons.append(f"note location matches {diagnosis_location}")

    if re.search(r"\b(?:wound|ulcer|burn|abscess|drainage|dressing|periwound)\b", normalized_note):
        score += 1
        reasons.append("note contains wound-care terms")

    return score, reasons


def map_notes_to_wound_diagnoses(
    notes: list[dict[str, Any]],
    active_wound_diagnoses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    for note in notes:
        scored = []
        for wound_dx in active_wound_diagnoses:
            score, reasons = note_score_for_diagnosis(
                note.get("note_text") or "",
                wound_dx["diagnosis"],
                wound_dx["classification"],
            )
            if score > 0:
                scored.append((score, wound_dx, reasons))

        scored.sort(key=lambda item: item[0], reverse=True)
        if scored:
            best_score, best_dx, best_reasons = scored[0]
            mappings.append(
                {
                    "note_id": note.get("id"),
                    "note_type": note.get("note_type"),
                    "effective_date": note.get("effective_date"),
                    "mapped_icd10_code": best_dx["diagnosis"].get("icd10_code"),
                    "mapped_icd10_description": best_dx["diagnosis"].get("icd10_description"),
                    "score": best_score,
                    "reasons": best_reasons,
                    "ambiguous": len(scored) > 1 and scored[1][0] == best_score,
                    "candidate_count": len(scored),
                }
            )
        else:
            mappings.append(
                {
                    "note_id": note.get("id"),
                    "note_type": note.get("note_type"),
                    "effective_date": note.get("effective_date"),
                    "mapped_icd10_code": None,
                    "mapped_icd10_description": None,
                    "score": 0,
                    "reasons": ["no active wound diagnosis matched this note"],
                    "ambiguous": False,
                    "candidate_count": 0,
                }
            )
    return mappings


def unique_diagnoses(all_diagnoses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    for diagnosis in all_diagnoses:
        key = (diagnosis.get("icd10_code"), diagnosis.get("icd10_description"))
        by_key.setdefault(key, diagnosis)
    return list(by_key.values())


def analyze(args: argparse.Namespace) -> Path:
    snapshot_dir = args.snapshot_dir or latest_snapshot(args.raw_dir)
    output_dir = args.output_dir / snapshot_dir.name
    patients = load_patients(snapshot_dir)

    all_diagnoses: list[dict[str, Any]] = []
    for patient in patients:
        patient_id = patient["patient_id"]
        diagnoses_path = snapshot_dir / "patients" / patient_id / "diagnoses.json"
        for diagnosis in json.loads(diagnoses_path.read_text(encoding="utf-8")):
            all_diagnoses.append(diagnosis)

    classifications: dict[str, dict[str, Any]] = {}
    for diagnosis in unique_diagnoses(all_diagnoses):
        code = diagnosis.get("icd10_code")
        if code:
            classifications[code] = deterministic_wound_classification(diagnosis)

    llm_error: str | None = None
    if args.use_llm:
        try:
            llm_classifications = classify_with_llm(
                unique_diagnoses(all_diagnoses),
                model=args.model,
                timeout_seconds=args.openai_timeout,
            )
            classifications.update(llm_classifications)
        except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            llm_error = str(exc)
            if args.require_llm:
                raise
            print(f"LLM classification failed; using deterministic fallback. Error: {llm_error}")

    patient_outputs: list[dict[str, Any]] = []
    wound_diagnosis_rows: list[dict[str, Any]] = []
    active_wound_counts = Counter()

    for patient in patients:
        patient_id = patient["patient_id"]
        patient_dir = snapshot_dir / "patients" / patient_id
        diagnoses = json.loads((patient_dir / "diagnoses.json").read_text(encoding="utf-8"))
        notes = json.loads((patient_dir / "notes.json").read_text(encoding="utf-8"))

        diagnosis_rows: list[dict[str, Any]] = []
        active_wound_diagnoses: list[dict[str, Any]] = []

        for diagnosis in diagnoses:
            code = diagnosis.get("icd10_code")
            classification = classifications.get(code or "", deterministic_wound_classification(diagnosis))
            row = {
                "diagnosis": diagnosis,
                "classification": classification,
                "is_active_wound": diagnosis.get("clinical_status") == "active" and classification["is_wound"],
            }
            diagnosis_rows.append(row)
            if row["is_active_wound"]:
                active_wound_diagnoses.append(row)
                wound_diagnosis_rows.append(
                    {
                        "patient_id": patient_id,
                        "icd10_code": code,
                        "icd10_description": diagnosis.get("icd10_description"),
                        "wound_type": classification.get("wound_type"),
                        "classification_source": classification.get("source"),
                    }
                )

        active_wound_counts[len(active_wound_diagnoses)] += 1
        note_mappings = map_notes_to_wound_diagnoses(notes, active_wound_diagnoses)
        all_notes_mapped = all(mapping["mapped_icd10_code"] for mapping in note_mappings) if notes else False

        patient_outputs.append(
            {
                "patient_id": patient_id,
                "internal_id": patient["id"],
                "facility_id": patient["facility_id"],
                "active_wound_diagnosis_count": len(active_wound_diagnoses),
                "has_single_active_wound_diagnosis": len(active_wound_diagnoses) == 1,
                "diagnoses": diagnosis_rows,
                "note_mappings": note_mappings,
                "all_notes_mapped_to_active_wound_diagnosis": all_notes_mapped,
            }
        )

    summary = {
        "snapshot": str(snapshot_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "diagnosis_count": len(all_diagnoses),
        "unique_icd10_code_count": len(classifications),
        "active_wound_diagnosis_count_by_patient": dict(sorted(active_wound_counts.items())),
        "patients_with_exactly_one_active_wound_diagnosis": active_wound_counts[1],
        "patients_with_multiple_active_wound_diagnoses": sum(
            count for wound_count, count in active_wound_counts.items() if wound_count > 1
        ),
        "patients_with_no_active_wound_diagnosis": active_wound_counts[0],
        "llm_used": args.use_llm and llm_error is None,
        "llm_error": llm_error,
        "model": args.model if args.use_llm else None,
    }

    write_json(output_dir / "diagnosis_classifications.json", classifications)
    write_json(output_dir / "patient_diagnosis_note_mappings.json", patient_outputs)
    write_json(output_dir / "active_wound_diagnoses.json", wound_diagnosis_rows)
    write_json(output_dir / "summary.json", summary)
    print(f"Wrote analysis to {output_dir}")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return output_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Map active wound diagnoses to notes.")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed/diagnosis_note_mapping"))
    parser.add_argument("--use-llm", action="store_true", help="Use OpenAI to classify unique ICD-10 diagnoses.")
    parser.add_argument("--require-llm", action="store_true", help="Fail instead of falling back if the LLM call fails.")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--openai-timeout", type=float, default=60.0)
    return parser.parse_args()


def main() -> None:
    analyze(parse_args())


if __name__ == "__main__":
    main()
