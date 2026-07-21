"""Validate two local PhantomBuster profile exports before deploying the action.

Usage:
    python validate_exports.py company.csv stealth.csv
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from score_leads import (
    SOURCE_FIELD,
    build_scoring_payload,
    clean_profiles,
    validate_profile_export_schema,
)

LABELS = ("Company founders FR/BE", "Stealth founders FR/BE")


def load_export(path: Path, label: str) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    validate_profile_export_schema(rows, label)
    for row in rows:
        row[SOURCE_FIELD] = label
    return rows


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: python validate_exports.py COMPANY.csv STEALTH.csv")

    all_rows: list[dict[str, str]] = []
    for label, filename in zip(LABELS, sys.argv[1:]):
        rows = load_export(Path(filename), label)
        print(f"{label}: {len(rows)} raw rows")
        all_rows.extend(rows)

    profiles, stats = clean_profiles(all_rows, set())
    sizes = [
        len(json.dumps(build_scoring_payload(profile), ensure_ascii=False))
        for profile in profiles
    ]
    print(
        "Quality: "
        f"input={stats.input_rows} errors={stats.error_rows} "
        f"duplicates={stats.duplicate_in_run} unique={stats.accepted}"
    )
    if sizes:
        sizes.sort()
        print(
            "Scoring payload chars: "
            f"min={sizes[0]} median={sizes[len(sizes) // 2]} max={sizes[-1]}"
        )


if __name__ == "__main__":
    main()
