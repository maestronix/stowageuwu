# Architecture and implementation notes

## Product boundary

stowageuwu measures the total byte size of attachments posted to one Discord guild. The guild is treated as an attachment-only, append-only source. Attachments are opaque binary objects: their bodies are never downloaded or inspected, and no MIME/file-extension classification is performed.

The UI is intentionally global. Channel IDs are retained for cursor management and diagnostics, but there is no channel analytics page, attachment browser, filename search, largest-file list, or file-type breakdown.

## Components

| Component | Responsibility | Runtime |
|---|---|---|
| Scanner | Gateway events, channel/thread discovery, historical backfill, reconciliation | Python + discord.py |
| API | Summary, growth series, health | FastAPI + asyncpg |
| Database | Immutable attachment rows, scan cursors, hourly/daily aggregates, scanner status | PostgreSQL 17 |
| Dashboard | Summary cards, scan progress, composed chart, date controls | React + TypeScript + Vite + Recharts |
| Web server | Static assets and same-origin API proxy | Nginx |

Scanner and API run as separate containers. A slow Discord backfill cannot block chart requests.

## Data model

### attachments

One row per unique Discord attachment ID:

- `attachment_id BIGINT PRIMARY KEY`
- `message_id BIGINT NOT NULL`
- `channel_id BIGINT NOT NULL`
- `size_bytes BIGINT NOT NULL`
- `message_created_at TIMESTAMPTZ NOT NULL`
- `first_seen_at TIMESTAMPTZ NOT NULL`

There is an index on `message_created_at` for bounded custom-range edge queries and summary growth windows, plus an index on `channel_id` for diagnostics. No filename, attachment URL, content type, or body is retained.

### scan_state

One row per channel/thread, containing its name/type for diagnostics, last committed message snowflake, last successful scan, current status, and last error. Cursor writes happen only after the page's attachment inserts have committed.

### storage_stats_hourly / storage_stats_daily

These are rebuildable aggregates keyed by UTC bucket start. Each stores attachment count and total bytes attributed to the parent message's timestamp. The raw attachment table remains the source of truth.

### scanner_status

A single row exposes the current scan phase, run start/completion, channels total/done, messages scanned, newly inserted attachments, and the last error for the dashboard.

## Ingestion and scan lifecycle

1. On connection, discover accessible text/news channels, forum channels, active threads, and archived threads the bot is permitted to enumerate.
2. Backfill each channel/thread in ascending message order, requesting pages of 100 messages.
3. For each page, insert all attachments with `ON CONFLICT (attachment_id) DO NOTHING`.
4. In the same database transaction as the inserts, update hourly and daily aggregates only for rows that were actually inserted.
5. After the insert transaction commits, persist the page's final message snowflake as that channel's cursor.
6. Continue until no later messages remain. A restart repeats at most the last uncommitted page; duplicate attachment IDs are harmless.
7. Gateway events ingest new messages immediately. Both cached and raw message-edit events are handled so newly attached files on an existing message can be picked up.
8. Periodic reconciliation starts just before the saved cursor (one-message overlap) and is idempotent. This catches missed gateway events and races with the backfill.

Discord.py handles Discord's route/global rate limits. The scanner deliberately processes channels serially to avoid creating unnecessary API pressure. The database writes are batched per 100-message page.

### Visibility limits

The bot needs the privileged Message Content Intent enabled in the Developer Portal, plus View Channel and Read Message History in every target channel. Discord gates attachment metadata behind Message Content Intent, including for historical message reads. Archived private thread enumeration depends on Discord permissions (Manage Threads or membership in the thread). The bot can only inventory messages/attachments that Discord still exposes to it. Content deleted before the first scan cannot be recovered.

## Historical semantics

The event time is always `message.created_at` (UTC). `first_seen_at` is operational metadata only and never affects a chart.

- `growth_bytes`: sum of attachment sizes whose parent messages fall in the selected interval.
- `cumulative_bytes`: all historical attachment bytes through the current plotted bucket, including the pre-range baseline.
- The initial backfill is therefore placed at the original post dates, not at the day the bot was installed.
- The chart uses a composed view: growth as bars, lifetime cumulative storage as a line, on one time axis and separate Y axes.
- Ranges up to 24 hours use hourly buckets. Longer ranges use daily buckets. Empty buckets are returned as zero.
- Custom range endpoints are exact and end-exclusive. If the first or last bucket is partial, that edge is calculated from raw rows; fully covered buckets use the aggregate tables.

The summary totals are read from daily aggregates instead of repeatedly scanning the full million-row attachment table. The 24-hour and 30-day growth cards use indexed timestamp windows with aggregate buckets and exact raw-row edge corrections.

## Durability and recovery

- Attachment IDs are primary keys; event/reconciliation/backfill duplication cannot inflate totals.
- Attachment insert and aggregate updates share one transaction.
- Cursor advancement occurs only after that transaction commits.
- A process crash between attachment commit and cursor commit causes a safe replay, not data loss or double-counting.
- Raw attachment rows are never deleted by the application.
- Aggregates can be rebuilt with `python -m app.rebuild_stats`.
- PostgreSQL is not partitioned initially. Add partitioning only after measured operational need.

## Security and deployment

The dashboard is unauthenticated in the application. It is intended to be published only behind the user's reverse proxy with authentication or an access policy. The API is not published as a host port; Nginx proxies it over the private Compose network. Secrets are supplied through `.env` and must not be committed.

## Deliberate non-goals

- Downloading attachment data.
- Attachment/file-type classification.
- Search or browsing of random hexadecimal filenames.
- Per-channel analytics UI.
- Delete events, tombstones, or subtracting storage after deletion.
- Multi-guild support (the initial target is one configured guild).
