from __future__ import annotations

from pathlib import Path

import aiosqlite

from src.models import Trade, PortfolioSnapshot


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        await self._conn.commit()

    async def save_trade(self, trade: Trade):
        await self._conn.execute(
            "INSERT OR REPLACE INTO trades (id, data) VALUES (?, ?)",
            (trade.id, trade.model_dump_json()),
        )
        await self._conn.commit()

    async def get_trades(self, limit: int = 100) -> list[Trade]:
        cursor = await self._conn.execute(
            "SELECT data FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [Trade.model_validate_json(row[0]) for row in rows]

    async def save_snapshot(self, snapshot: PortfolioSnapshot):
        await self._conn.execute(
            "INSERT INTO snapshots (data) VALUES (?)",
            (snapshot.model_dump_json(),),
        )
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
