# stowageuwu

Discord attachment storage analytics. The bot inventories every accessible attachment in a server, records its original message timestamp and byte size, and exposes historical growth analytics through a small web dashboard.

## Project status

The repository is being bootstrapped. The first implementation targets a single Discord guild and a self-hosted Docker Compose deployment.

## Goals

- Backfill the accessible message history of every guild text channel and thread, including archived public/private threads where the bot has permission.
- Store attachment metadata only. Attachment bodies are never downloaded.
- Treat attachments as immutable historical records: an attachment is inserted once, keyed by Discord's attachment ID, and is not removed when a Discord message later disappears.
- Attribute every byte to the Discord message's created_at timestamp, never to the time the scanner discovered it.
- Resume an interrupted backfill safely.
- Continue tracking new attachments from gateway events and periodic per-channel reconciliation.
- Show lifetime uploaded bytes and interval growth in one combined chart: cumulative line plus growth bars.
- Support 24h, 7d, 30d, 1y, and arbitrary custom date/time ranges.

## Explicit non-goals

- Downloading attachment contents.
- Classifying binary attachments by MIME type or file extension.
- Searching/browsing individual attachment filenames.
- Per-channel analytics UI (channel IDs remain in the database for scanner cursors and diagnostics).
- Deletion tracking or subtracting deleted attachments from historical totals.
- Storing attachment CDN URLs.

## Architecture

- Scanner: Python 3.12, discord.py. Handles initial backfill, gateway events, and reconciliation.
- API: FastAPI, read-only analytics endpoints plus health endpoint.
- Database: PostgreSQL. Raw attachment rows are the source of truth; hourly/daily buckets are rebuildable query accelerators.
- Dashboard: React + TypeScript + Vite + Recharts.
- Deployment: Docker Compose. The dashboard/API can sit behind an existing reverse proxy; no TLS termination is performed in the stack.

The scanner and API are separate processes so long-running Discord history scans cannot block dashboard requests.

## Data model

### attachments

One row per Discord attachment, unique on attachment_id:

- attachment_id (Discord snowflake, primary key)
- message_id
- channel_id
- filename (opaque/random name; retained as metadata, not exposed as a search feature)
- size_bytes (exact byte count from Discord metadata)
- message_created_at (UTC timestamp from the parent message)
- first_seen_at (UTC timestamp when this installation discovered it)

The original message timestamp is the event time used for every chart. first_seen_at is operational metadata only.

### scan_state

One row per channel/thread with the latest fully processed message snowflake, last successful scan time, status, and error information. The cursor advances only after a page has been committed. Reconciliation uses a small overlap and ON CONFLICT DO NOTHING, so retries are safe.

### storage_stats_hourly and storage_stats_daily

Rebuildable aggregates keyed by UTC bucket timestamp. They store the number of attachments and sum of bytes whose message creation time falls inside the bucket. They are not the source of truth and can be rebuilt from attachments.

The cumulative line is calculated as the sum of all historical bytes through each point. Growth bars show bytes added inside each bucket. The initial backfill is therefore attributed to the original posting dates instead of appearing as growth on the day the bot was installed.

## Scanning lifecycle

1. On startup, discover the guild's accessible text channels and active threads.
2. Discover archived public/private threads where Discord permits listing them.
3. Backfill each channel/thread in descending message order using Discord history pagination.
4. Insert attachment rows idempotently and persist each channel cursor after successful page processing.
5. Once backfill is complete, keep gateway message events enabled for low-latency ingestion.
6. Periodically reconcile each channel from its saved cursor with an overlap. Events and reconciliation can race safely because attachment IDs are unique.
7. Rebuild affected hourly/daily buckets transactionally after each ingest batch (or in bounded batches during backfill).

Discord permissions and API visibility define the maximum inventory: the bot needs View Channel and Read Message History for every target channel. Some archived/private threads may not be listable unless the bot has the required access. Messages or attachments deleted before the bot can see them cannot be recovered through the API.

## Analytics semantics

- All timestamps and buckets are UTC.
- Storage is decimal bytes summed exactly in PostgreSQL BIGINT; presentation converts to GB/TB.
- Lifetime uploaded storage is the sum of all unique attachment sizes ever observed, grouped by original message timestamp. Given the server's stated append-only behavior, this is also the current total inventory.
- Growth is the sum of sizes of attachments whose parent messages were created in the selected interval.
- Presets: last 24 hours, 7 days, 30 days, and 1 year. Custom ranges accept explicit start/end timestamps.
- Buckets are hourly for ranges up to 24 hours and daily for longer ranges. Empty buckets are returned as zero so charts do not have gaps.
- The line and bars share one time axis. A composed chart uses a cumulative line and per-bucket growth bars; separate axes may be used because cumulative totals and interval increments have different scales.

## Operational notes

- Set DISCORD_TOKEN and DISCORD_GUILD_ID; never commit secrets.
- The bot does not need the privileged Message Content intent to inspect attachment metadata.
- The bot needs the guilds and messages gateway intents, plus channel/history permissions.
- Discord API rate limits must be respected. Backfill concurrency should be conservative and configurable.
- PostgreSQL is intentionally unpartitioned initially. One million or several million metadata rows are ordinary for PostgreSQL; add partitioning only if measured query/maintenance pressure justifies it.
- Keep raw attachment rows permanently. Aggregate tables can always be regenerated from them.
