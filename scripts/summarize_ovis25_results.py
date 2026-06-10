#!/usr/bin/env python
"""Summarize Ovis2.5 local evaluation JSONL results."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any


EXPECTED_KEYS = {"scene_type", "people_count_range", "activity", "risks", "needs_review"}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def validate_payload(payload: Any) -> list[str]:
    issues: list[str] = []
    if not isinstance(payload, dict):
        return ["parsed_output is not an object"]
    missing = EXPECTED_KEYS - set(payload)
    if missing:
        issues.append(f"missing keys: {sorted(missing)}")
    people = payload.get("people_count_range")
    if not isinstance(people, dict) or not {"min", "max"} <= set(people):
        issues.append("people_count_range must contain min/max")
    risks = payload.get("risks")
    if not isinstance(risks, list):
        issues.append("risks must be a list")
    else:
        for idx, risk in enumerate(risks):
            if not isinstance(risk, dict):
                issues.append(f"risks[{idx}] is not an object")
    if not isinstance(payload.get("needs_review"), bool):
        issues.append("needs_review must be boolean")
    return issues


def risk_names(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    risks = payload.get("risks")
    if not isinstance(risks, list):
        return []
    names: list[str] = []
    for risk in risks:
        if isinstance(risk, dict):
            names.append(str(risk.get("risk_type", "missing")))
        else:
            names.append(str(risk))
    return names


def render_report(rows: list[dict[str, Any]], title: str, notes: list[str]) -> str:
    total_times = [row.get("total_seconds") for row in rows if isinstance(row.get("total_seconds"), (int, float))]
    generate_times = [row.get("generate_seconds") for row in rows if isinstance(row.get("generate_seconds"), (int, float))]
    parse_errors = [row for row in rows if row.get("parse_error")]
    schema_issues: dict[str, list[str]] = {}
    scene_counter: Counter[str] = Counter()
    risk_counter: Counter[str] = Counter()
    review_count = 0

    for row in rows:
        image_id = str(row.get("image_id", "unknown"))
        payload = row.get("parsed_output")
        issues = validate_payload(payload)
        if issues:
            schema_issues[image_id] = issues
        if isinstance(payload, dict):
            scene_counter[str(payload.get("scene_type", "missing"))] += 1
            if payload.get("needs_review") is True:
                review_count += 1
        risk_counter.update(risk_names(payload))

    lines = [
        f"# {title}",
        "",
        "## Runtime Summary",
        "",
        f"- Images processed: {len(rows)}",
        f"- Parse errors: {len(parse_errors)}",
        f"- Schema/shape issues: {len(schema_issues)}",
        f"- Needs review count: {review_count}",
        f"- Average total seconds/image: {mean(total_times):.2f}" if total_times else "- Average total seconds/image: n/a",
        f"- Average generation seconds/image: {mean(generate_times):.2f}" if generate_times else "- Average generation seconds/image: n/a",
        f"- Min/Max total seconds/image: {min(total_times):.2f} / {max(total_times):.2f}" if total_times else "- Min/Max total seconds/image: n/a",
        "",
        "## Scene Distribution",
        "",
    ]
    for scene, count in scene_counter.most_common():
        lines.append(f"- `{scene}`: {count}")

    lines.extend(["", "## Risk Distribution", ""])
    if risk_counter:
        for risk, count in risk_counter.most_common():
            lines.append(f"- `{risk}`: {count}")
    else:
        lines.append("- No risks emitted.")

    if schema_issues:
        lines.extend(["", "## Schema/Shape Issues", ""])
        for image_id, issues in schema_issues.items():
            lines.append(f"- `{image_id}`: {'; '.join(issues)}")

    lines.extend(["", "## Per-image Outputs", ""])
    for row in rows:
        payload = row.get("parsed_output") or {}
        lines.append(
            f"- `{row.get('image_id')}`: {row.get('total_seconds')}s, "
            f"scene=`{payload.get('scene_type') if isinstance(payload, dict) else 'invalid'}`, "
            f"review=`{payload.get('needs_review') if isinstance(payload, dict) else 'invalid'}`, "
            f"risks={risk_names(payload)}"
        )

    lines.extend(["", "## Notes", ""])
    for note in notes:
        lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize Ovis2.5 evaluation JSONL results.")
    parser.add_argument("--input-jsonl", type=Path, default=Path("outputs/ovis25_2b_data15_results.jsonl"))
    parser.add_argument("--output-md", type=Path, default=Path("outputs/ovis25_2b_data15_report.md"))
    parser.add_argument("--title", default="Ovis2.5-2B Local Evaluation Report")
    parser.add_argument(
        "--note",
        action="append",
        default=[],
        help="Add a bullet to the report Notes section. Can be passed multiple times.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = load_jsonl(args.input_jsonl)
    notes = args.note or [
        "This run used the compact Ovis prompt to keep CPU inference practical.",
        "The current machine ran on CPU float32; no NVIDIA GPU was detected.",
        "JSON parse success does not guarantee business correctness. Schema/shape issues and false positives still require manual review.",
    ]
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(render_report(rows, args.title, notes), encoding="utf-8")
    print(f"Wrote {args.output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
