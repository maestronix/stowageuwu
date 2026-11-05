"""Rebuild hourly and daily growth aggregates from immutable attachment rows."""
import asyncio

from app.db import close_db, init_db, pool

async def main() -> None:
    await init_db()
    try:
        async with pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL TIME ZONE 'UTC'")
                await conn.execute("TRUNCATE storage_stats_hourly, storage_stats_daily")
                await conn.execute("""
                    INSERT INTO storage_stats_hourly (bucket_start, attachment_count, bytes_added)
                    SELECT date_trunc('hour', message_created_at), COUNT(*), SUM(size_bytes)
                    FROM attachments GROUP BY 1 ORDER BY 1
                """)
                await conn.execute("""
                    INSERT INTO storage_stats_daily (bucket_start, attachment_count, bytes_added)
                    SELECT date_trunc('day', message_created_at), COUNT(*), SUM(size_bytes)
                    FROM attachments GROUP BY 1 ORDER BY 1
                """)
        print("Hourly and daily aggregates rebuilt from attachments.")
    finally:
        await close_db()

if __name__ == "__main__":
    asyncio.run(main())
