from datetime import UTC, datetime
from pathlib import Path

from pdd_listing_automation.storage.database import connect


class DraftLedger:
    def __init__(self, path: Path, shop: str, market: str):
        self.path, self.shop, self.market = path, shop.strip(), market.strip().lower()

    def status(self, goods_id: str) -> str:
        with connect(self.path) as db:
            return self._status(db, goods_id)

    def _status(self, db, goods_id: str) -> str:
        row = db.execute(
            "SELECT status FROM draft_submissions WHERE target_shop=? AND goods_id=? "
            "AND (source_market=? OR source_market='' OR ?='') "
            "ORDER BY (status='uncertain') DESC LIMIT 1",
            (self.shop, goods_id, self.market, self.market),
        ).fetchone()
        return row[0] if row else ""

    def reserve(self, goods_id: str) -> str:
        """原子检查兼容站点记录并预写提交意图，返回已存在的状态。"""
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._status(db, goods_id)
            if not previous:
                db.execute(
                    "INSERT INTO draft_submissions VALUES (?, ?, ?, ?, ?, ?)",
                    (self.shop, self.market, goods_id, "uncertain", "已进入草稿提交阶段",
                     datetime.now(UTC).isoformat()),
                )
            return previous

    def save(self, goods_id: str, status: str, detail: str = "") -> None:
        with connect(self.path) as db:
            db.execute("""
                INSERT INTO draft_submissions VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_shop, source_market, goods_id) DO UPDATE SET
                    status=excluded.status, detail=excluded.detail, updated_at=excluded.updated_at
            """, (self.shop, self.market, goods_id, status, detail, datetime.now(UTC).isoformat()))

    def resolve(self, goods_id: str, outcome: str) -> bool:
        with connect(self.path) as db:
            params = (self.shop, self.market, goods_id)
            if outcome == "not-submitted":
                result = db.execute(
                    "DELETE FROM draft_submissions WHERE target_shop=? AND source_market=? AND goods_id=?",
                    params,
                )
            else:
                result = db.execute(
                    "UPDATE draft_submissions SET status='submitted' "
                    "WHERE target_shop=? AND source_market=? AND goods_id=?", params,
                )
        return result.rowcount > 0
