from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.db import close_db, init_db, pool

app = FastAPI(title="stowageuwu API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_credentials=False, allow_methods=["GET"], allow_headers=["*"])

async def sum_growth_window(conn, start_at: datetime, end_at: datetime, table: str, step: timedelta) -> int:
    floor = lambda dt: dt.replace(minute=0, second=0, microsecond=0) if step == timedelta(hours=1) else dt.replace(hour=0, minute=0, second=0, microsecond=0)
    first_bucket = floor(start_at)
    last_bucket = floor(end_at - timedelta(microseconds=1))
    rows = await conn.fetch(f"SELECT bucket_start, bytes_added FROM {table} WHERE bucket_start >= $1 AND bucket_start <= $2",
                            first_bucket, last_bucket)
    totals = {r["bucket_start"]: int(r["bytes_added"]) for r in rows}
    total = sum(totals.values())
    if first_bucket == last_bucket:
        total -= totals.get(first_bucket, 0)
        total += await conn.fetchval("SELECT COALESCE(SUM(size_bytes), 0) FROM attachments WHERE message_created_at >= $1 AND message_created_at < $2",
                                     start_at, end_at)
        return int(total)
    if start_at > first_bucket:
        total -= totals.get(first_bucket, 0)
        total += await conn.fetchval("SELECT COALESCE(SUM(size_bytes), 0) FROM attachments WHERE message_created_at >= $1 AND message_created_at < $2",
                                     start_at, min(first_bucket + step, end_at))
    if end_at < last_bucket + step:
        total -= totals.get(last_bucket, 0)
        total += await conn.fetchval("SELECT COALESCE(SUM(size_bytes), 0) FROM attachments WHERE message_created_at >= $1 AND message_created_at < $2",
                                     max(last_bucket, start_at), end_at)
    return int(total)

@app.on_event("startup")
async def startup():
    await init_db()

@app.on_event("shutdown")
async def shutdown():
    await close_db()

@app.get("/health")
async def health():
    async with pool().acquire() as conn:
        await conn.fetchval("SELECT 1")
    return {"status": "ok"}

@app.get("/api/summary")
async def summary():
    async with pool().acquire() as conn:
        totals = await conn.fetchrow("SELECT COALESCE(SUM(attachment_count), 0) AS attachment_count, COALESCE(SUM(bytes_added), 0) AS total_bytes FROM storage_stats_daily")
        now = datetime.now(timezone.utc)
        growth_24h = await sum_growth_window(conn, now - timedelta(hours=24), now, "storage_stats_hourly", timedelta(hours=1))
        growth_30d = await sum_growth_window(conn, now - timedelta(days=30), now, "storage_stats_daily", timedelta(days=1))
        status = await conn.fetchrow("SELECT phase, started_at, completed_at, channels_total, channels_done, messages_scanned, attachments_found, last_error, updated_at FROM scanner_status WHERE id = 1")
    return {"attachment_count": totals["attachment_count"], "total_bytes": totals["total_bytes"],
            "growth_24h_bytes": growth_24h, "growth_30d_bytes": growth_30d, "scanner": dict(status) if status else None}

@app.get("/api/growth")
async def growth(start: datetime | None = Query(default=None), end: datetime | None = Query(default=None)):
    now = datetime.now(timezone.utc)

    def to_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    end_at = to_utc(end) if end else now
    start_at = to_utc(start) if start else end_at - timedelta(days=30)
    if start_at >= end_at:
        raise HTTPException(status_code=400, detail="start must be earlier than end")
    if end_at - start_at > timedelta(days=3660):
        raise HTTPException(status_code=400, detail="Maximum range is 10 years")

    hourly = end_at - start_at <= timedelta(hours=24)
    table = "storage_stats_hourly" if hourly else "storage_stats_daily"
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    floor = lambda dt: dt.replace(minute=0, second=0, microsecond=0) if hourly else dt.replace(hour=0, minute=0, second=0, microsecond=0)
    first_bucket = floor(start_at)
    last_bucket = floor(end_at - timedelta(microseconds=1))

    async with pool().acquire() as conn:
        baseline = await conn.fetchval(f"SELECT COALESCE(SUM(bytes_added), 0) FROM {table} WHERE bucket_start < $1", first_bucket)
        if start_at > first_bucket:
            baseline += await conn.fetchval(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM attachments WHERE message_created_at >= $1 AND message_created_at < $2",
                first_bucket, start_at)
        bucket_rows = await conn.fetch(f"""SELECT bucket_start, attachment_count, bytes_added FROM {table}
                                         WHERE bucket_start >= $1 AND bucket_start <= $2 ORDER BY bucket_start""",
                                       first_bucket, last_bucket)
        raw_edges = []
        if first_bucket == last_bucket:
            raw_edges.extend(await conn.fetch(
                "SELECT message_created_at, size_bytes FROM attachments WHERE message_created_at >= $1 AND message_created_at < $2 ORDER BY message_created_at",
                start_at, end_at))
        else:
            if start_at > first_bucket:
                raw_edges.extend(await conn.fetch(
                    "SELECT message_created_at, size_bytes FROM attachments WHERE message_created_at >= $1 AND message_created_at < $2 ORDER BY message_created_at",
                    start_at, min(first_bucket + step, end_at)))
            if end_at < last_bucket + step:
                raw_edges.extend(await conn.fetch(
                    "SELECT message_created_at, size_bytes FROM attachments WHERE message_created_at >= $1 AND message_created_at < $2 ORDER BY message_created_at",
                    max(last_bucket, start_at), end_at))

    by_bucket = {r["bucket_start"]: [int(r["attachment_count"]), int(r["bytes_added"])] for r in bucket_rows}
    for bucket in {first_bucket, last_bucket}:
        if bucket in by_bucket and (start_at > bucket or end_at < bucket + step):
            by_bucket.pop(bucket)
    for row in raw_edges:
        ts = row["message_created_at"].astimezone(timezone.utc)
        bucket = floor(ts)
        by_bucket.setdefault(bucket, [0, 0])
        by_bucket[bucket][0] += 1
        by_bucket[bucket][1] += int(row["size_bytes"])

    points = []
    cumulative = int(baseline or 0)
    bucket = first_bucket
    while bucket <= last_bucket:
        counts, growth_bytes = by_bucket.get(bucket, [0, 0])
        cumulative += growth_bytes
        points.append({"timestamp": bucket.isoformat(), "growth_bytes": growth_bytes,
                       "cumulative_bytes": cumulative, "attachment_count": counts})
        bucket += step
    return {"start": start_at.isoformat(), "end": end_at.isoformat(),
            "granularity": "hour" if hourly else "day", "points": points}
