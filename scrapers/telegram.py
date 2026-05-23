import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from telethon import TelegramClient
from telethon.tl.types import Message

from utils import retry_async

logger = logging.getLogger(__name__)


class TelegramScraper:
    def __init__(self, api_id: int, api_hash: str, data_dir: str = "./data"):
        self.api_id = api_id
        self.api_hash = api_hash
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = str(self.data_dir / "telethon_session")
        self.state_file = self.data_dir / "last_ids.json"
        self.client = TelegramClient(self.session_file, api_id, api_hash)
        self.last_ids = self._load_state()

    def _load_state(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {}

    def _save_state(self):
        self.state_file.write_text(json.dumps(self.last_ids, indent=2))

    async def connect(self, phone: str | None = None):
        await self.client.start(phone=phone)
        me = await self.client.get_me()
        logger.info(f"Logged in as {me.first_name}")

    async def disconnect(self):
        await self.client.disconnect()

    @retry_async(max_retries=3, base_delay=2.0, max_delay=30.0)
    async def _get_entity(self, channel_username: str):
        return await self.client.get_entity(channel_username)

    async def fetch_new_messages(
        self,
        channels: list[str],
        max_per_channel: int = 50,
        first_run: bool = False,
        first_run_days: int = 7,
        first_run_max: int = 200,
    ) -> list[dict]:
        messages = []
        since_date = (
            datetime.now(timezone.utc) - timedelta(days=first_run_days)
            if first_run
            else None
        )

        for channel_username in channels:
            try:
                entity = await self._get_entity(channel_username)
                channel_name = getattr(entity, "title", channel_username)
            except Exception as e:
                logger.error(f"Could not resolve {channel_username}: {e}")
                continue

            if first_run:
                logger.info(
                    f"FIRST RUN: Fetching {channel_name} "
                    f"(last {first_run_days} days, max {first_run_max})"
                )
            else:
                min_id = self.last_ids.get(channel_username, 0)
                logger.info(
                    f"Fetching {channel_name} (since msg_id={min_id})"
                )

            try:
                fetched = 0
                msg_count = 0
                limit = first_run_max if first_run else max_per_channel

                async for msg in self.client.iter_messages(
                    entity, limit=limit * 3 if first_run else limit
                ):
                    if not isinstance(msg, Message) or not msg.text:
                        continue

                    if first_run and since_date and msg.date < since_date:
                        continue

                    messages.append(
                        {
                            "channel": channel_name,
                            "channel_username": channel_username,
                            "msg_id": msg.id,
                            "date": msg.date.isoformat(),
                            "text": msg.text,
                            "has_media": msg.media is not None,
                        }
                    )
                    fetched += 1
                    msg_count += 1

                    if first_run and fetched >= first_run_max:
                        break
                    elif not first_run and fetched >= max_per_channel:
                        break

                if fetched > 0:
                    if first_run:
                        self.last_ids[channel_username] = max(
                            m["msg_id"]
                            for m in messages
                            if m["channel_username"] == channel_username
                        )
                        self._save_state()
                        logger.info(
                            f"  {channel_name}: {fetched} messages "
                            f"(saved checkpoint)"
                        )
                    else:
                        self.last_ids[channel_username] = max(
                            m["msg_id"]
                            for m in messages
                            if m["channel_username"] == channel_username
                        )
                        self._save_state()
                        logger.info(
                            f"  {channel_name}: {fetched} new messages"
                        )
                else:
                    logger.info(f"  {channel_name}: no new messages")

            except Exception as e:
                logger.error(f"Error fetching from {channel_name}: {e}")

        return messages

    @property
    def is_first_run(self) -> bool:
        return len(self.last_ids) == 0
