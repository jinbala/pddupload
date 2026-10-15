from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pdd_listing_automation.models import ShopNewArrival
from pdd_listing_automation.storage.database import connect


class BatchJournal:
    """逐款提交检查点；只有 pending/running 可恢复，已完成项永不自动重跑。"""

    def __init__(self, database_path: Path, batch_id: str):
        self.database_path = database_path
        self.batch_id = batch_id

    @classmethod
    def create(cls, database_path: Path, options: dict, arrivals: list[ShopNewArrival]):
        batch_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:8]
        with connect(database_path) as db:
            db.execute(
                "INSERT INTO batches(batch_id, options_json, created_at) VALUES (?, ?, ?)",
                (batch_id, json.dumps(options, ensure_ascii=False), datetime.now(UTC).isoformat()),
            )
            db.executemany(
                "INSERT INTO batch_items(batch_id, position, arrival_json) VALUES (?, ?, ?)",
                [(batch_id, i, arrival.model_dump_json()) for i, arrival in enumerate(arrivals)],
            )
        return cls(database_path, batch_id)

    def load(self) -> tuple[dict, list[dict]]:
        with connect(self.database_path) as db:
            batch = db.execute("SELECT * FROM batches WHERE batch_id = ?", (self.batch_id,)).fetchone()
            if batch is None:
                raise ValueError(f"找不到批次：{self.batch_id}")
            rows = db.execute(
                "SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position", (self.batch_id,),
            ).fetchall()
        return json.loads(batch["options_json"]), [
            {"position": row["position"], "arrival": ShopNewArrival.model_validate_json(row["arrival_json"]),
             "state": row["state"], "result": json.loads(row["result_json"]) if row["result_json"] else None}
            for row in rows
        ]

    def start(self, position: int) -> None:
        with connect(self.database_path) as db:
            db.execute(
                "UPDATE batch_items SET state='running' WHERE batch_id=? AND position=? AND state!='done'",
                (self.batch_id, position),
            )
            db.execute("UPDATE batches SET state='running', finished_at='' WHERE batch_id=?", (self.batch_id,))

    def record(self, position: int, result: dict) -> None:
        with connect(self.database_path) as db:
            db.execute(
                "UPDATE batch_items SET state='done', result_json=? WHERE batch_id=? AND position=?",
                (json.dumps(result, ensure_ascii=False), self.batch_id, position),
            )

    def finish(self) -> None:
        with connect(self.database_path) as db:
            pending = db.execute(
                "SELECT COUNT(*) FROM batch_items WHERE batch_id=? AND state!='done'", (self.batch_id,),
            ).fetchone()[0]
            db.execute(
                "UPDATE batches SET state=?, finished_at=? WHERE batch_id=?",
                ("interrupted" if pending else "complete", datetime.now(UTC).isoformat(), self.batch_id),
            )

    @staticmethod
    def recent(database_path: Path) -> list[dict]:
        with connect(database_path) as db:
            return [dict(row) for row in db.execute("""
                SELECT b.batch_id, b.options_json, b.state, COUNT(*) AS total,
                       SUM(i.state='done') AS processed
                FROM batches b JOIN batch_items i ON i.batch_id=b.batch_id
                GROUP BY b.batch_id ORDER BY b.created_at DESC LIMIT 30
            """)]
