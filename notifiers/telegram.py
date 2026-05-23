import re
import json
import asyncio
import logging
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from utils import retry_async

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
DELAY_BETWEEN_SENDS = 3.5
LISTINGS_PER_MESSAGE = 3

JSON_KEYS = ["role", "company", "description", "eligibility", "location", "apply_link", "posted_date"]


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id

    @retry_async(max_retries=3, base_delay=1.5, max_delay=20.0)
    async def _send(self, text: str) -> int:
        msg = await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        return msg.message_id

    async def _send_with_flood_control(self, text: str) -> bool:
        try:
            await self._send(text)
            return True
        except TelegramError as e:
            err_str = str(e)
            wait_match = re.search(r"retry after (\d+)", err_str.lower())
            if wait_match:
                wait = int(wait_match.group(1)) + 2
                logger.warning(
                    f"Flood control — waiting {wait}s before resuming..."
                )
                await asyncio.sleep(wait)
                try:
                    await self._send(text)
                    return True
                except TelegramError:
                    return False
            return False

    async def send_listings(self, listings: list[dict]):
        if not listings:
            logger.info("No listings to send")
            return

        total = len(listings)
        logger.info(f"Sending {total} listings to {self.chat_id}")

        batches = [
            listings[i : i + LISTINGS_PER_MESSAGE]
            for i in range(0, total, LISTINGS_PER_MESSAGE)
        ]
        sent = 0
        skipped = 0

        for batch_idx, batch in enumerate(batches):
            msg = self._format_batch(batch, batch_idx + 1, len(batches))
            if len(msg) > MAX_MESSAGE_LENGTH:
                msg = msg[:MAX_MESSAGE_LENGTH - 50] + "\n\n[...truncated]"

            success = await self._send_with_flood_control(msg)

            if success:
                sent += len(batch)
            else:
                skipped += len(batch)

            if batch_idx < len(batches) - 1:
                await asyncio.sleep(DELAY_BETWEEN_SENDS)

        summary = f"\n\nSent: {sent}/{total} | Skipped: {skipped}"
        try:
            await self._send(summary)
        except Exception:
            pass

        logger.info(f"Sent {sent} listings, skipped {skipped}")

    async def send_error(self, error: str):
        try:
            await self._send(f"Scraper error:\n\n<pre>{error[:500]}</pre>")
        except Exception:
            logger.error(f"Could not send error notification: {error}")

    def _format_batch(
        self, items: list[dict], batch_num: int, total_batches: int
    ) -> str:
        parts = []
        for item in items:
            clean = {k: item.get(k, "") for k in JSON_KEYS}
            block = "<pre>" + json.dumps(clean, indent=2, ensure_ascii=False) + "</pre>"
            if parts:
                block = "\n" + block
            parts.append(block)

        return f"Batch {batch_num}/{total_batches}:\n" + "".join(parts)
