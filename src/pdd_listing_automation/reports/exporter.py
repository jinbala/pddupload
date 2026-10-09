from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def export_models(
    models: list[BaseModel],
    output_dir: Path,
    stem: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if not models:
        rows: list[dict[str, Any]] = []
    else:
        rows = [model.model_dump(mode="json") for model in models]

    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"

    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fieldnames = list(rows[0].keys()) if rows else []
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(rows)

    return json_path, csv_path
