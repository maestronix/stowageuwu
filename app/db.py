from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterable

import asyncpg

DATABASE_URL = os.environ["DATABASE_URL"]
_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    attachment_id BIGINT PRIMARY KEY,
    message_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    message_created_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_attachments_message_created_at ON attachments (message_created_at);
CREATE INDEX IF NOT EXISTS idx_attachments_channel_id ON attachments (channel_id);

CREATE TABLE IF NOT EXISTS scan_state (
    channel_id BIGINT PRIMARY KEY,
    channel_name TEXT NOT NULL,
    channel_type TEXT NOT NULL,
    last_message_id BIGINT,
    last_successful_scan TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS storage_stats_hourly (
    bucket_start TIMESTAMPTZ PRIMARY KEY,
    attachment_count BIGINT NOT NULL DEFAULT 0,
    bytes_added BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS storage_stats_daily (
    bucket_start TIMESTAMPTZ PRIMARY KEY,
    attachment_count BIGINT NOT NULL DEFAULT 0,
    bytes_added BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scanner_status (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    phase TEXT NOT NULL DEFAULT 'idle',
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    channels_total INTEGER NOT NULL DEFAULT 0,
    channels_done INTEGER NOT NULL DEFAULT 0,
    messages_scanned BIGINT NOT NULL DEFAULT 0,
    attachments_found BIGINT NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO scanner_status (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
"""

async def init_db() -> None:
    global _pool
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10, command_timeout=60)
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)

async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized")
    return _pool

async def ingest_attachments(records: Iterable[dict]) -> int:
    rows = list(records)
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    values = [(
        int(r["attachment_id"]), int(r["message_id"]), int(r["channel_id"]),
        int(r["size_bytes"]), r["message_created_at"], now,
    ) for r in rows]
    async with pool().acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetch("""
                INSERT INTO attachments
                    (attachment_id, message_id, channel_id, size_bytes, message_created_at, first_seen_at)
                SELECT x.attachment_id, x.message_id, x.channel_id, x.size_bytes,
                       x.message_created_at, x.first_seen_at
                FROM UNNEST($1::bigint[], $2::bigint[], $3::bigint[], $4::bigint[],
                           $5::timestamptz[], $6::timestamptz[])
                     AS x(attachment_id, message_id, channel_id, size_bytes, message_created_at, first_seen_at)
                ON CONFLICT (attachment_id) DO NOTHING
                RETURNING size_bytes, message_created_at
            """, *[list(col) for col in zip(*values)])
            if not inserted:
                return 0
            hourly: dict[datetime, list[int]] = {}
            daily: dict[datetime, list[int]] = {}
            for row in inserted:
                ts = row["message_created_at"].astimezone(timezone.utc)
                h = ts.replace(minute=0, second=0, microsecond=0)
                d = ts.replace(hour=0, minute=0, second=0, microsecond=0)
                hourly.setdefault(h, [0, 0])
                daily.setdefault(d, [0, 0])
                hourly[h][0] += 1
                hourly[h][1] += row["size_bytes"]
                daily[d][0] += 1
                daily[d][1] += row["size_bytes"]
            for table, aggregates in (("storage_stats_hourly", hourly), ("storage_stats_daily", daily)):
                await conn.executemany(
                    f"""INSERT INTO {table} (bucket_start, attachment_count, bytes_added)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (bucket_start) DO UPDATE SET
                            attachment_count = {table}.attachment_count + EXCLUDED.attachment_count,
                            bytes_added = {table}.bytes_added + EXCLUDED.bytes_added""",
                    [(bucket, counts[0], counts[1]) for bucket, counts in aggregates.items()],
                )
            await conn.execute(
                "UPDATE scanner_status SET attachments_found = attachments_found + $1, updated_at = now() WHERE id = 1",
                len(inserted),
            )
            return len(inserted)

async def upsert_scan_state(channel_id: int, channel_name: str, channel_type: str, *, cursor: int | None = None,
                            status: str = "scanning", error: str | None = None, success: bool = False) -> None:
    async with pool().acquire() as conn:
        await conn.execute("""
            INSERT INTO scan_state (channel_id, channel_name, channel_type, last_message_id, last_successful_scan, status, last_error)
            VALUES ($1, $2, $3, $4, CASE WHEN $7 THEN now() ELSE NULL END, $5, $6)
            ON CONFLICT (channel_id) DO UPDATE SET
                channel_name = EXCLUDED.channel_name,
                channel_type = EXCLUDED.channel_type,
                last_message_id = COALESCE(EXCLUDED.last_message_id, scan_state.last_message_id),
                last_successful_scan = CASE WHEN $7 THEN now() ELSE scan_state.last_successful_scan END,
                status = EXCLUDED.status,
                last_error = EXCLUDED.last_error,
                updated_at = now()
        """, channel_id, channel_name, channel_type, cursor, status, error, success)

async def get_scan_cursor(channel_id: int) -> int | None:
    async with pool().acquire() as conn:
        return await conn.fetchval("SELECT last_message_id FROM scan_state WHERE channel_id = $1", channel_id)

async def increment_messages_scanned(count: int) -> None:
    if count <= 0:
        return
    async with pool().acquire() as conn:
        await conn.execute("UPDATE scanner_status SET messages_scanned = messages_scanned + $1, updated_at = now() WHERE id = 1", count)

async def update_scanner_status(**fields) -> None:
    allowed = {"phase", "started_at", "completed_at", "channels_total", "channels_done", "messages_scanned", "attachments_found", "last_error"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{key} = ${i}" for i, key in enumerate(fields, 1))
    async with pool().acquire() as conn:
        await conn.execute(f"UPDATE scanner_status SET {assignments}, updated_at = now() WHERE id = 1", *fields.values())
