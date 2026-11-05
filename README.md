# stowageuwu

Self-hosted Discord attachment storage analytics. The scanner inventories accessible attachments without downloading their contents, records the parent message's original timestamp and exact byte size, and exposes historical growth through a web dashboard.

## Features

- Initial historical backfill of accessible text channels, forum posts, active threads, and listable archived threads.
- Idempotent attachment ingestion keyed by Discord attachment ID.
- Persistent per-channel/thread cursors; an interrupted scan resumes from the last committed page.
- Gateway ingestion for new messages and attachment additions on message edits.
- Periodic reconciliation (default: every 6 hours) to recover events missed while the bot/API/database was unavailable.
- PostgreSQL source-of-truth records plus rebuildable hourly and daily aggregates.
- Dashboard with lifetime storage, attachment count, 24h/30d growth, scanner progress, and a combined cumulative-line + growth-bar chart.
- Time presets: 24h, 7d, 30d, 1y, plus custom start/end date-times.
- No attachment downloads, content inspection, file-type classification, filename search, or per-channel analytics UI.

## Quick start

1. Create a Discord application and add a Bot user.
2. Invite it to the target server with the `bot` OAuth scope and these permissions:
   - View Channels
   - Read Message History
   - Manage Threads is recommended if you want the bot to enumerate archived private threads it has not joined.
3. No privileged gateway intents need to be enabled for stowageuwu. It uses guild and message events and does not read message content.
4. Copy `.env.example` to `.env` and fill in:
   - `DISCORD_TOKEN`
   - `DISCORD_GUILD_ID`
   - `POSTGRES_PASSWORD` (use a long URL-safe value, e.g. hexadecimal)
5. Start the stack:

   ```bash
   docker compose up -d --build
   ```

6. The dashboard listens on `127.0.0.1:8098` by default. Put it behind your reverse proxy and add authentication/access restrictions there. The API is only exposed inside the Compose network.

The PostgreSQL data directory is `./data/postgres`. Back it up as part of the deployment. Never commit `.env` or the database directory.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DISCORD_TOKEN` | required | Bot token |
| `DISCORD_GUILD_ID` | required | Single target server ID |
| `POSTGRES_PASSWORD` | required | PostgreSQL password |
| `RECONCILE_MINUTES` | `360` | Periodic full channel-cursor reconciliation interval; minimum 5 minutes |
| `DASHBOARD_BIND` | `127.0.0.1` | Host bind address for the dashboard port |
| `DASHBOARD_PORT` | `8098` | Host port for the dashboard |
| `LOG_LEVEL` | `INFO` | Scanner log level |

If the reverse proxy itself runs in Docker, either route to the host's reachable address/port or adjust `DASHBOARD_BIND` and firewall rules accordingly. Do not expose the unauthenticated dashboard/API directly to the public internet.

## Data and analytics

- Each attachment row stores only attachment ID, message ID, channel ID, size in bytes, original message timestamp, and first-seen timestamp. Filenames, CDN URLs, MIME types, and file bodies are not stored.
- Attachment IDs are unique. Replaying a page or processing the same attachment through both a gateway event and reconciliation cannot double-count it.
- All timestamps and buckets are UTC. Charts use the message's creation timestamp, never the scanner's discovery timestamp.
- Hourly buckets power ranges up to 24 hours; daily buckets power longer ranges. Partial first/last buckets for custom ranges are calculated against raw attachment rows so the requested bounds are respected.
- Lifetime storage is the sum of all unique attachment sizes ever observed. The server is treated as append-only: records are never deleted or decremented.
- PostgreSQL is initially unpartitioned. One million or several million rows are an ordinary workload; measure before adding partitioning.

See [docs/architecture.md](docs/architecture.md) for the implementation model, failure handling, and design decisions.

## Maintenance

Rebuild the hourly/daily aggregates from the immutable attachment table if needed:

```bash
docker compose exec scanner python -m app.rebuild_stats
```

Health endpoint (inside the Compose network): `GET /health`.
