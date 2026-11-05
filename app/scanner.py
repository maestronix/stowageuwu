from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from app.db import close_db, get_scan_cursor, increment_messages_scanned, ingest_attachments, init_db, update_scanner_status, upsert_scan_state

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("stowageuwu.scanner")
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"])
RECONCILE_MINUTES = max(5, int(os.getenv("RECONCILE_MINUTES", "360")))

intents = discord.Intents.none()
intents.guilds = True
intents.messages = True

class StorageBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.backfill_lock = asyncio.Lock()
        self.initial_scan_started = False

    async def setup_hook(self):
        await init_db()

    async def close(self):
        if self.reconcile_loop.is_running():
            self.reconcile_loop.cancel()
        await close_db()
        await super().close()

    async def on_ready(self):
        log.info("Connected as %s; guild=%s", self.user, GUILD_ID)
        guild = self.get_guild(GUILD_ID)
        if guild is None:
            log.error("Configured guild %s is not available to this bot", GUILD_ID)
            return
        if not self.initial_scan_started:
            self.initial_scan_started = True
            asyncio.create_task(self.run_initial_scan(guild))

    async def run_initial_scan(self, guild: discord.Guild):
        try:
            await self.scan_guild(guild, initial=True)
        finally:
            if not self.reconcile_loop.is_running():
                self.reconcile_loop.start()

    async def on_message(self, message: discord.Message):
        if message.guild is None or message.guild.id != GUILD_ID or not message.attachments:
            return
        await self.store_message(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if after.guild is None or after.guild.id != GUILD_ID or not after.attachments:
            return
        await self.store_message(after)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id != GUILD_ID:
            return
        raw_attachments = payload.data.get("attachments", [])
        if not raw_attachments:
            return
        created = discord.utils.snowflake_time(payload.message_id).astimezone(timezone.utc)
        records = [{
            "attachment_id": int(item["id"]),
            "message_id": payload.message_id,
            "channel_id": payload.channel_id,
            "size_bytes": int(item.get("size", 0)),
            "message_created_at": created,
        } for item in raw_attachments]
        await ingest_attachments(records)

    async def store_message(self, message: discord.Message) -> int:
        created = message.created_at.replace(tzinfo=timezone.utc) if message.created_at.tzinfo is None else message.created_at.astimezone(timezone.utc)
        records = [{
            "attachment_id": a.id,
            "message_id": message.id,
            "channel_id": message.channel.id,
            "size_bytes": a.size,
            "message_created_at": created,
        } for a in message.attachments]
        return await ingest_attachments(records)

    async def discover_channels(self, guild: discord.Guild) -> list:
        found: dict[int, object] = {}
        for channel in guild.channels:
            if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                found[channel.id] = channel
        for thread in guild.threads:
            found[thread.id] = thread
        # Archived public/private threads are exposed only where the bot has permission.
        for channel in list(found.values()):
            if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                continue
            if isinstance(channel, discord.ForumChannel):
                try:
                    async for thread in channel.archived_threads(limit=None):
                        found[thread.id] = thread
                except (discord.Forbidden, discord.HTTPException, AttributeError) as exc:
                    log.debug("Could not enumerate archived forum posts for %s: %s", channel.id, exc)
            else:
                for private, joined in ((False, False), (True, False), (True, True)):
                    try:
                        async for thread in channel.archived_threads(limit=None, private=private, joined=joined):
                            found[thread.id] = thread
                    except (discord.Forbidden, discord.HTTPException, AttributeError, TypeError) as exc:
                        log.debug("Could not enumerate archived threads for %s (private=%s joined=%s): %s", channel.id, private, joined, exc)
        return list(found.values())

    async def scan_channel(self, channel, *, initial: bool = False) -> tuple[int, int]:
        cursor = await get_scan_cursor(channel.id)
        name = getattr(channel, "name", str(channel.id))
        kind = type(channel).__name__
        await upsert_scan_state(channel.id, name, kind, status="scanning")
        scanned = added = 0
        last_id = cursor
        try:
            # Explicit 100-message pages let us persist a cursor only after the page is committed.
            while True:
                kwargs = {"limit": 100, "oldest_first": True}
                if last_id is not None:
                    kwargs["after"] = discord.Object(id=max(0, last_id - 1))
                page = [m async for m in channel.history(**kwargs)]
                if not page:
                    break
                batch = []
                for message in page:
                    scanned += 1
                    if message.attachments:
                        created = message.created_at.replace(tzinfo=timezone.utc) if message.created_at.tzinfo is None else message.created_at.astimezone(timezone.utc)
                        for a in message.attachments:
                            batch.append({"attachment_id": a.id, "message_id": message.id, "channel_id": channel.id,
                                          "size_bytes": a.size, "message_created_at": created})
                added += await ingest_attachments(batch)
                last_id = page[-1].id
                await upsert_scan_state(channel.id, name, kind, cursor=last_id, status="scanning")
                await increment_messages_scanned(len(page))
                if len(page) < 100:
                    break
            await upsert_scan_state(channel.id, name, kind, cursor=last_id, status="idle", success=True)
            return scanned, added
        except (discord.Forbidden, discord.NotFound) as exc:
            log.warning("Skipping inaccessible channel/thread %s (%s): %s", name, channel.id, exc)
            await upsert_scan_state(channel.id, name, kind, status="inaccessible", error=str(exc))
            return scanned, added
        except Exception as exc:
            log.exception("Scan failed for %s (%s)", name, channel.id)
            await upsert_scan_state(channel.id, name, kind, cursor=last_id, status="error", error=str(exc))
            raise

    async def scan_guild(self, guild: discord.Guild, *, initial: bool = False):
        async with self.backfill_lock:
            channels = await self.discover_channels(guild)
            log.info("Starting %s scan across %d channels/threads", "initial backfill" if initial else "reconciliation", len(channels))
            await update_scanner_status(phase="backfill" if initial else "reconciliation", started_at=datetime.now(timezone.utc),
                                        completed_at=None, channels_total=len(channels), channels_done=0,
                                        messages_scanned=0, attachments_found=0, last_error=None)
            done = 0
            for channel in channels:
                try:
                    scanned, added = await self.scan_channel(channel, initial=initial)
                    log.info("Scanned %s: %d messages, %d new attachments", getattr(channel, "name", channel.id), scanned, added)
                except Exception as exc:
                    await update_scanner_status(last_error=f"{getattr(channel, 'name', channel.id)}: {exc}")
                    # Continue with other channels; failed channel keeps its last committed cursor.
                done += 1
                await update_scanner_status(channels_done=done)
            await update_scanner_status(phase="idle", completed_at=datetime.now(timezone.utc))
            log.info("Scan finished: %d channels/threads processed", done)

    @tasks.loop(minutes=RECONCILE_MINUTES)
    async def reconcile_loop(self):
        guild = self.get_guild(GUILD_ID)
        if guild:
            await self.scan_guild(guild, initial=False)

    @reconcile_loop.before_loop
    async def before_reconcile(self):
        await self.wait_until_ready()
        # tasks.loop runs its first iteration immediately; delay it so the first
        # reconciliation happens one full interval after the initial backfill.
        await asyncio.sleep(RECONCILE_MINUTES * 60)

async def main():
    bot = StorageBot()
    async with bot:
        await bot.start(os.environ["DISCORD_TOKEN"])

if __name__ == "__main__":
    asyncio.run(main())
