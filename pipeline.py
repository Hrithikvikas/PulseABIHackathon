#!/usr/bin/env python3
"""Build patient-level wound billing triage results from a raw snapshot."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.analyze_diagnoses_notes import (
    OPENAI_RESPONSES_URL,
    classify_with_llm,
    deterministic_wound_classification,
    latest_snapshot,
    map_notes_to_wound_diagnoses,
    unique_diagnoses,
)


WOUND_PREFIXES = ("L89", "E10", "E11", "I83", "L97", "L98", "T14", "L03", "T20", "T21", "T22", "T23", "T24", "T25", "T81", "I70", "L02")
REQUIRED_FIELDS = ("wound_type", "location", "length_cm", "width_cm", "depth_cm", "drainage")


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()
DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_wound_type(value: str | None) -> str | None:
    if not value:
        return None
    text = value.lower().replace("-", " ").replace("_", " ")
    if "pressure" in text:
        return "pressure_ulcer"
    if "diabetic" in text:
        return "diabetic_foot_ulcer"
    if "venous" in text:
        return "venous_stasis_ulcer"
    if "arterial" in text:
        return "arterial_ulcer"
    if "surgical" in text:
        return "surgical_site_infection"
    if "abscess" in text:
        return "abscess"
    if "burn" in text:
        return "burn"
    return text.strip().replace(" ", "_") or None


def normalize_stage(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"n/a", "na", "none", "null"}:
        return None
    match = re.search(r"(unstageable|stage\s*[234]|[234])", text, re.I)
    if not match:
        return text
    stage = match.group(1).lower().replace("stage", "").strip()
    return "unstageable" if stage == "unstageable" else stage


def normalize_drainage(value: str | None) -> str | None:
    if not value:
        return None
    text = value.lower()
    if "none" in text or "no drainage" in text:
        return "none"
    if "heavy" in text or "large" in text:
        return "heavy"
    if "moderate" in text or re.search(r"\bmod\b", text):
        return "moderate"
    if "light" in text or "minimal" in text or re.search(r"\bmin\b", text) or "slight" in text:
        return "light"
    return None


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def parse_assessment_raw_json(raw_json: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return None

    answers: dict[str, str] = {}
    narrative: str | None = None
    for section in raw.get("sections", []):
        for question in section.get("questions", []):
            key = str(question.get("question") or "").strip().lower()
            answer = str(question.get("answer") or "").strip()
            answers[key] = answer
            if key == "wound narrative":
                narrative = answer

    if narrative:
        extracted = extract_regex(narrative)
        if extracted:
            extracted["source"] = "assessment"
            extracted["source_format"] = "assessment_narrative"
            return extracted
        return None

    result = {
        "wound_type": normalize_wound_type(answers.get("wound type")),
        "stage": normalize_stage(answers.get("stage")),
        "location": answers.get("location") or None,
        "length_cm": as_float(answers.get("length (cm)")),
        "width_cm": as_float(answers.get("width (cm)")),
        "depth_cm": as_float(answers.get("depth (cm)")),
        "drainage": normalize_drainage(answers.get("drainage amount")),
        "source": "assessment",
        "source_format": "structured_assessment",
        "low_confidence": False,
    }
    return result if any(result.get(field) is not None for field in REQUIRED_FIELDS) else None


def extract_from_assessment(assessments: list[dict[str, Any]]) -> dict[str, Any] | None:
    current_complete = [
        assessment for assessment in assessments
        if assessment.get("status") == "Complete" and assessment.get("is_current") is True
    ]
    for assessment in current_complete:
        result = parse_assessment_raw_json(assessment.get("raw_json") or "")
        if result:
            return result
    return None


def detect_format(note_text: str) -> str:
    if re.search(r"(?im)^(location|wound type|length|width|depth|drainage)\s*:", note_text):
        return "soap"
    if all(token in note_text.lower() for token in ("subjective:", "objective:", "assessment:", "plan:")):
        return "soap"
    if re.search(r"(?i)\bmeas(?:ures?|ured)?\b.*\d+(?:\.\d+)?\s*x\s*\d+", note_text):
        return "prose"
    return "envive"


def stage_rank(stage: str | None) -> int:
    if not stage:
        return 0
    if stage == "unstageable":
        return 5
    return int(stage) if str(stage).isdigit() else 0


def wound_area(wound: dict[str, Any]) -> float:
    return float(wound.get("length_cm") or 0) * float(wound.get("width_cm") or 0)


def choose_primary_wound(wounds: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not wounds:
        return None
    return sorted(wounds, key=lambda wound: (stage_rank(wound.get("stage")), wound_area(wound)), reverse=True)[0]


def extract_regex(note_text: str) -> dict[str, Any] | None:
    wounds = []
    text = note_text.replace("\n", " ")

    narrative = re.search(
        r"(?:Wound Status:\s*)?(?P<type>Pressure Ulcer|Diabetic|Venous|Arterial|Surgical|Abscess|Burn|[^/]+?)\s+to\s+(?P<location>.*?)\s*/\s*Measures\s+"
        r"(?P<length>\d+(?:\.\d+)?)\s*cm\s*x\s*(?P<width>\d+(?:\.\d+)?)\s*cm"
        r"(?:\s*x\s*(?P<depth>\d+(?:\.\d+)?)\s*cm)?\s*/\s*Stage:\s*(?P<stage>.*?)\s*/\s*"
        r"(?:Drainage:\s*|Drainage present\s*-\s*)(?P<drainage>[^./|]+)",
        text,
        re.I,
    )
    if narrative:
        wounds.append(
            {
                "wound_type": normalize_wound_type(narrative.group("type")),
                "stage": normalize_stage(narrative.group("stage")),
                "location": narrative.group("location").strip(),
                "length_cm": as_float(narrative.group("length")),
                "width_cm": as_float(narrative.group("width")),
                "depth_cm": as_float(narrative.group("depth")),
                "drainage": normalize_drainage(narrative.group("drainage")),
            }
        )

    soap = re.search(
        r"(?:Wound assessment performed\.\s*)?(?P<type>Stage\s*[234]\s+pressure ulcer|Pressure Ulcer|Diabetic|Venous|Arterial|Surgical|Abscess|Burn)"
        r".*?\s+(?P<location>[A-Z][A-Za-z ]+?)\s+measures\s+"
        r"(?P<length>\d+(?:\.\d+)?)\s*cm\s*x\s*(?P<width>\d+(?:\.\d+)?)\s*cm\s*x\s*(?P<depth>\d+(?:\.\d+)?)\s*cm.*?"
        r"Drainage:\s*(?P<drainage>\w+)",
        text,
        re.I,
    )
    if soap:
        wounds.append(
            {
                "wound_type": normalize_wound_type(soap.group("type")),
                "stage": normalize_stage(soap.group("type")),
                "location": soap.group("location").strip(),
                "length_cm": as_float(soap.group("length")),
                "width_cm": as_float(soap.group("width")),
                "depth_cm": as_float(soap.group("depth")),
                "drainage": normalize_drainage(soap.group("drainage")),
            }
        )

    prose = re.search(
        r"Wound note\s*-\s*(?P<location>[A-Za-z]+)\.\s*Meas\s+"
        r"(?P<length>\d+(?:\.\d+)?)\s*x\s*(?P<width>\d+(?:\.\d+)?)\s*x\s*(?P<depth>\d+(?:\.\d+)?)\s*cm\.\s*"
        r"(?P<drainage>[^.]*drainage)",
        text,
        re.I,
    )
    if prose:
        wounds.append(
            {
                "wound_type": None,
                "stage": None,
                "location": prose.group("location").strip(),
                "length_cm": as_float(prose.group("length")),
                "width_cm": as_float(prose.group("width")),
                "depth_cm": as_float(prose.group("depth")),
                "drainage": normalize_drainage(prose.group("drainage")),
            }
        )

    multi = re.search(
        r"(?P<type>Pressure Ulcer|Diabetic|Venous|Arterial|Surgical|Abscess|Burn)\s+"
        r"(?P<location>[A-Z][A-Za-z ]+?)\s+measures\s+aprx\s+"
        r"(?P<length>\d+(?:\.\d+)?)\s*x\s*(?P<width>\d+(?:\.\d+)?)\s*cm,\s*depth\s+(?P<depth>\d+(?:\.\d+)?)\s*cm.*?"
        r"(?P<drainage>Min|Moderate|Heavy|Light|None|Slight)\s+drainage",
        text,
        re.I,
    )
    if multi:
        wounds.append(
            {
                "wound_type": normalize_wound_type(multi.group("type")),
                "stage": normalize_stage(multi.group("type")),
                "location": multi.group("location").strip(),
                "length_cm": as_float(multi.group("length")),
                "width_cm": as_float(multi.group("width")),
                "depth_cm": as_float(multi.group("depth")),
                "drainage": normalize_drainage(multi.group("drainage")),
            }
        )

    result = choose_primary_wound(wounds)
    if not result:
        return None
    result.update({"source": "note_regex", "source_format": detect_format(note_text), "low_confidence": False})
    return result


def extract_llm(note_text: str, model: str, timeout_seconds: float) -> dict[str, Any] | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "wound_type": {"type": ["string", "null"]},
            "stage": {"type": ["string", "null"]},
            "location": {"type": ["string", "null"]},
            "length_cm": {"type": ["number", "null"]},
            "width_cm": {"type": ["number", "null"]},
            "depth_cm": {"type": ["number", "null"]},
            "drainage": {"type": ["string", "null"], "enum": ["none", "light", "moderate", "heavy", None]},
        },
        "required": ["wound_type", "stage", "location", "length_cm", "width_cm", "depth_cm", "drainage"],
    }
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": "Extract wound-care fields from clinical notes. Return only JSON."},
            {"role": "user", "content": f"Extract wound care data from this clinical note:\n\n{note_text}"},
        ],
        "text": {"format": {"type": "json_schema", "name": "wound_extraction", "strict": True, "schema": schema}},
    }
    request = Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw_response = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError):
        return None

    text = raw_response.get("output_text")
    if not text:
        parts = []
        for item in raw_response.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    parts.append(content.get("text", ""))
        text = "".join(parts)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return None
    result["wound_type"] = normalize_wound_type(result.get("wound_type"))
    result["stage"] = normalize_stage(result.get("stage"))
    result["drainage"] = normalize_drainage(result.get("drainage"))
    result.update({"source": "envive", "source_format": "envive", "low_confidence": True})
    return result


def extract_from_notes(notes: list[dict[str, Any]], model: str, timeout_seconds: float) -> dict[str, Any] | None:
    soap_or_prose = [note for note in notes if detect_format(note.get("note_text") or "") in {"soap", "prose"}]
    envive = [note for note in notes if detect_format(note.get("note_text") or "") == "envive"]
    for note in soap_or_prose:
        try:
            result = extract_regex(note.get("note_text") or "")
            if result:
                return result
        except Exception:
            continue
    for note in envive:
        result = extract_regex(note.get("note_text") or "")
        if result:
            result["low_confidence"] = True
            result["source_format"] = "envive"
            return result
        result = extract_llm(note.get("note_text") or "", model=model, timeout_seconds=timeout_seconds)
        if result:
            return result
    return None


def enrich_from_diagnosis(extraction: dict[str, Any] | None, active_wound_diagnoses: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not extraction:
        return None
    if not active_wound_diagnoses:
        return extraction
    primary = active_wound_diagnoses[0]
    classification = primary["classification"]
    diagnosis = primary["diagnosis"]
    if not extraction.get("wound_type"):
        extraction["wound_type"] = classification.get("wound_type")
    if not extraction.get("location"):
        desc = diagnosis.get("icd10_description") or ""
        match = re.search(r"[–-]\s*(.+)$", desc)
        if match:
            extraction["location"] = match.group(1).strip()
    return extraction


def has_active_mcb_coverage(coverage: list[dict[str, Any]]) -> bool:
    today = date.today()
    for item in coverage:
        if item.get("payer_code") != "MCB" and item.get("payer_type") != "Medicare B":
            continue
        effective_to = item.get("effective_to")
        if not effective_to:
            return True
        try:
            end_date = datetime.fromisoformat(effective_to.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        if end_date >= today:
            return True
    return False


def route(
    patient: dict[str, Any],
    coverage: list[dict[str, Any]],
    diagnoses: list[dict[str, Any]],
    active_wound_diagnoses: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    extraction: dict[str, Any] | None,
) -> tuple[str, str]:
    if patient.get("primary_payer_code") != "MCB":
        return "reject", "Not Medicare Part B"
    if not has_active_mcb_coverage(coverage):
        return "reject", "Medicare Part B coverage expired or inactive"
    if not active_wound_diagnoses:
        return "reject", "No active qualifying wound diagnosis"
    if not notes and not assessments:
        return "reject", "No clinical notes or assessments found"
    if not extraction:
        return "reject", "Extraction failed: no wound details could be parsed"
    missing = [field for field in REQUIRED_FIELDS if extraction.get(field) in {None, ""}]
    if extraction.get("wound_type") == "pressure_ulcer" and extraction.get("stage") in {None, ""}:
        missing.append("stage")
    if missing:
        return "flag_for_review", f"Missing fields: {', '.join(missing)}"
    if extraction.get("low_confidence") or extraction.get("source_format") == "envive":
        return "flag_for_review", "Narrative note - verify extracted values"
    if len(active_wound_diagnoses) > 1:
        return "flag_for_review", "Multiple active wound diagnoses - verify primary wound"
    return "auto_accept", "All required fields documented and verified"


def summarize_patient_history(
    diagnoses: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    active_wound_diagnoses: list[dict[str, Any]],
) -> str:
    wound_codes = {row["diagnosis"].get("icd10_code") for row in active_wound_diagnoses}
    active_non_wound = [
        diagnosis.get("icd10_description")
        for diagnosis in diagnoses
        if diagnosis.get("clinical_status") == "active"
        and diagnosis.get("icd10_code") not in wound_codes
        and diagnosis.get("icd10_description")
    ]
    resolved_non_wound = [
        diagnosis.get("icd10_description")
        for diagnosis in diagnoses
        if diagnosis.get("clinical_status") != "active"
        and diagnosis.get("icd10_code") not in wound_codes
        and diagnosis.get("icd10_description")
    ]
    active_payers = [
        item.get("payer_name") or item.get("payer_type") or item.get("payer_code")
        for item in coverage
        if not item.get("effective_to")
    ]

    parts = []
    if active_non_wound:
        parts.append(f"Active non-wound conditions: {', '.join(active_non_wound[:4])}.")
    else:
        parts.append("No active non-wound diagnoses documented.")

    if resolved_non_wound:
        parts.append(f"Resolved/inactive history includes {', '.join(resolved_non_wound[:2])}.")

    if active_payers:
        parts.append(f"Active coverage on file: {', '.join(active_payers)}.")

    parts.append(f"Clinical record includes {len(notes)} wound note(s) and {len(assessments)} assessment(s).")
    return " ".join(parts)


def classify_all_diagnoses(
    patients: list[dict[str, Any]],
    snapshot_dir: Path,
    use_llm: bool,
    require_llm: bool,
    model: str,
    timeout_seconds: float,
) -> tuple[dict[str, dict[str, Any]], str | None]:
    all_diagnoses = []
    for patient in patients:
        all_diagnoses.extend(load_json(snapshot_dir / "patients" / patient["patient_id"] / "diagnoses.json"))
    classifications = {}
    unique = unique_diagnoses(all_diagnoses)
    for diagnosis in unique:
        code = diagnosis.get("icd10_code")
        if code:
            classifications[code] = deterministic_wound_classification(diagnosis)

    llm_error = None
    if use_llm:
        try:
            classifications.update(classify_with_llm(unique, model=model, timeout_seconds=timeout_seconds))
        except (HTTPError, URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
            llm_error = str(exc)
            if require_llm:
                raise
            print(f"LLM diagnosis classification failed; using deterministic fallback. Error: {llm_error}")

    return classifications, llm_error


def build_results(args: argparse.Namespace) -> list[dict[str, Any]]:
    snapshot_dir = args.snapshot_dir or latest_snapshot(args.raw_dir)
    patients = load_json(snapshot_dir / "all_patients.json")
    classifications, llm_error = classify_all_diagnoses(
        patients,
        snapshot_dir,
        use_llm=args.use_llm,
        require_llm=args.require_llm,
        model=args.model,
        timeout_seconds=args.openai_timeout,
    )
    results = []

    for patient in patients:
        patient_id = patient["patient_id"]
        patient_dir = snapshot_dir / "patients" / patient_id
        diagnoses = load_json(patient_dir / "diagnoses.json")
        coverage = load_json(patient_dir / "coverage.json")
        notes = load_json(patient_dir / "notes.json")
        assessments = load_json(patient_dir / "assessments.json")

        diagnosis_rows = []
        active_wound_diagnoses = []
        for diagnosis in diagnoses:
            code = diagnosis.get("icd10_code") or ""
            classification = classifications.get(code, deterministic_wound_classification(diagnosis))
            starts_with_wound_prefix = code.startswith(WOUND_PREFIXES)
            is_active_wound = (
                diagnosis.get("clinical_status") == "active"
                and classification.get("is_wound")
                and starts_with_wound_prefix
            )
            row = {"diagnosis": diagnosis, "classification": classification, "is_active_wound": is_active_wound}
            diagnosis_rows.append(row)
            if is_active_wound:
                active_wound_diagnoses.append(row)

        note_mappings = map_notes_to_wound_diagnoses(notes, active_wound_diagnoses)
        extraction = extract_from_assessment(assessments)
        if not extraction:
            extraction = extract_from_notes(notes, model=args.model, timeout_seconds=args.openai_timeout)
        extraction = enrich_from_diagnosis(extraction, active_wound_diagnoses)

        decision, reason = route(patient, coverage, diagnoses, active_wound_diagnoses, notes, assessments, extraction)
        history_summary = summarize_patient_history(diagnoses, coverage, notes, assessments, active_wound_diagnoses)
        extraction = extraction or {}
        results.append(
            {
                "patient_id": patient_id,
                "name": f"{patient.get('first_name') or ''} {patient.get('last_name') or ''}".strip(),
                "facility_id": patient.get("facility_id"),
                "payer": patient.get("primary_payer_code"),
                "decision": decision,
                "reason": reason,
                "history_summary": history_summary,
                "wound_type": extraction.get("wound_type"),
                "stage": extraction.get("stage"),
                "location": extraction.get("location"),
                "length_cm": extraction.get("length_cm"),
                "width_cm": extraction.get("width_cm"),
                "depth_cm": extraction.get("depth_cm"),
                "drainage": extraction.get("drainage"),
                "source": extraction.get("source"),
                "source_format": extraction.get("source_format"),
                "low_confidence": extraction.get("low_confidence", False),
                "active_wound_diagnosis_count": len(active_wound_diagnoses),
                "active_wound_icd10_codes": [
                    row["diagnosis"].get("icd10_code") for row in active_wound_diagnoses
                ],
                "note_mappings": note_mappings,
            }
        )

    write_json(args.output, results)
    summary = {
        "snapshot": str(snapshot_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(results),
        "decisions": dict(Counter(row["decision"] for row in results)),
        "active_wound_diagnosis_counts": dict(Counter(row["active_wound_diagnosis_count"] for row in results)),
        "llm_diagnosis_classification_used": args.use_llm and llm_error is None,
        "llm_error": llm_error,
    }
    write_json(args.summary_output, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run wound-care billing triage pipeline.")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/results.json"))
    parser.add_argument("--summary-output", type=Path, default=Path("data/results_summary.json"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--openai-timeout", type=float, default=60.0)
    parser.add_argument("--use-llm", action="store_true", help="Use OpenAI for diagnosis classification before routing.")
    parser.add_argument("--require-llm", action="store_true", help="Fail if LLM diagnosis classification cannot run.")
    return parser.parse_args()


def main() -> None:
    build_results(parse_args())


if __name__ == "__main__":
    main()
