#!/usr/bin/env python3
"""Fetch raw mock PCC data and store it as local JSON files."""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://hackathon.prod.pulsefoundry.ai"
FACILITY_IDS = (101, 102, 103)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class FetchStats:
    endpoint: str
    attempts: int
    status: str
    error: str | None = None


class ApiClient:
    def __init__(
        self,
        base_url: str,
        max_retries: int,
        base_delay_seconds: float,
        max_delay_seconds: float,
        timeout_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.base_delay_seconds = base_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.timeout_seconds = timeout_seconds
        self.stats: list[FetchStats] = []
        self._stats_lock = Lock()

    def get_json(self, path: str, params: dict[str, Any]) -> Any:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        endpoint = f"{path}?{query}" if query else path
        url = f"{self.base_url}{endpoint}"
        last_error: str | None = None

        for attempt in range(1, self.max_retries + 2):
            request = Request(url, headers={"Accept": "application/json"})
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = response.read().decode("utf-8")
                    self.record_stat(FetchStats(endpoint, attempt, "ok"))
                    return json.loads(payload)
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = f"HTTP {exc.code}: {body}"
                if exc.code not in RETRYABLE_STATUS_CODES or attempt > self.max_retries:
                    self.record_stat(FetchStats(endpoint, attempt, "failed", last_error))
                    raise

                retry_after = exc.headers.get("Retry-After")
                delay = self._next_delay(attempt, retry_after)
                print(f"Retrying {endpoint} after HTTP {exc.code}; sleeping {delay:.1f}s")
                time.sleep(delay)
            except (TimeoutError, URLError) as exc:
                last_error = str(exc)
                if attempt > self.max_retries:
                    self.record_stat(FetchStats(endpoint, attempt, "failed", last_error))
                    raise

                delay = self._next_delay(attempt, None)
                print(f"Retrying {endpoint} after network error; sleeping {delay:.1f}s")
                time.sleep(delay)

        self.record_stat(FetchStats(endpoint, self.max_retries + 1, "failed", last_error))
        raise RuntimeError(last_error or f"Failed to fetch {endpoint}")

    def record_stat(self, stat: FetchStats) -> None:
        with self._stats_lock:
            self.stats.append(stat)

    def _next_delay(self, attempt: int, retry_after: str | None) -> float:
        exponential_delay = self.base_delay_seconds * (2 ** (attempt - 1))
        server_delay = _parse_retry_after(retry_after)
        delay = max(exponential_delay, server_delay or 0)
        jitter = random.uniform(0, min(0.5, delay * 0.25))
        return min(delay + jitter, self.max_delay_seconds)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)


def fetch_patient_details(
    client: ApiClient,
    output_dir: Path,
    patient: dict[str, Any],
    since: str | None,
    fail_fast: bool,
) -> list[dict[str, Any]]:
    external_id = patient["patient_id"]
    internal_id = patient["id"]
    patient_dir = output_dir / "patients" / external_id
    patient_errors: list[dict[str, Any]] = []
    write_json(patient_dir / "patient.json", patient)

    fetches = (
        ("diagnoses", "/pcc/diagnoses", {"patient_id": external_id}),
        ("coverage", "/pcc/coverage", {"patient_id": external_id}),
        ("notes", "/pcc/notes", {"patient_id": internal_id, "since": since}),
        ("assessments", "/pcc/assessments", {"patient_id": internal_id, "since": since}),
    )

    for name, path, params in fetches:
        try:
            print(f"Fetching {name} for {external_id}")
            data = client.get_json(path, params)
            write_json(patient_dir / f"{name}.json", data)
        except Exception as exc:
            error = {
                "patient_id": external_id,
                "internal_id": internal_id,
                "dataset": name,
                "error": str(exc),
            }
            patient_errors.append(error)
            write_json(patient_dir / f"{name}.error.json", error)
            if fail_fast:
                raise

    return patient_errors


def fetch_snapshot(args: argparse.Namespace) -> Path:
    run_started_at = datetime.now(timezone.utc)
    run_id = run_started_at.strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    client = ApiClient(
        base_url=args.base_url,
        max_retries=args.max_retries,
        base_delay_seconds=args.base_delay,
        max_delay_seconds=args.max_delay,
        timeout_seconds=args.timeout,
    )

    all_patients: list[dict[str, Any]] = []

    for facility_id in args.facility_ids:
        print(f"Fetching patients for facility {facility_id}")
        patients = client.get_json("/pcc/patients", {"facility_id": facility_id, "since": args.since})
        write_json(output_dir / "facilities" / f"{facility_id}" / "patients.json", patients)
        all_patients.extend(patients)

    patient_errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(fetch_patient_details, client, output_dir, patient, args.since, args.fail_fast)
            for patient in all_patients
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            patient_errors.extend(future.result())
            if index % 25 == 0 or index == len(futures):
                print(f"Finished detail fetch for {index}/{len(futures)} patients")

    manifest = {
        "run_id": run_id,
        "base_url": args.base_url,
        "started_at": run_started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "facility_ids": args.facility_ids,
        "since": args.since,
        "patient_count": len(all_patients),
        "error_count": len(patient_errors),
        "errors": patient_errors,
        "request_stats": [asdict(stat) for stat in client.stats],
    }

    write_json(output_dir / "all_patients.json", all_patients)
    write_json(output_dir / "manifest.json", manifest)
    print(f"Wrote snapshot to {output_dir}")
    return output_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch hackathon mock PCC data into JSON files.")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--output-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--facility-ids", nargs="+", type=int, default=list(FACILITY_IDS))
    parser.add_argument("--since", help="Optional ISO timestamp/date for endpoints that support incremental fetching.")
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--base-delay", type=float, default=1.0)
    parser.add_argument("--max-delay", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--fail-fast", action="store_true")
    return parser.parse_args()


def main() -> None:
    fetch_snapshot(parse_args())


if __name__ == "__main__":
    main()
