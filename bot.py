#!/usr/bin/env python3
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

from scrapers.telegram import TelegramScraper
from sanitizer import AISanitizer
from notifiers.telegram import TelegramNotifier

from logging.handlers import RotatingFileHandler

load_dotenv()

LOG_DIR = Path(os.environ.get("DATA_DIR", "./data"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            LOG_DIR / "bot.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
        ),
    ],
)
logger = logging.getLogger("scraper-bot")
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)


def normalize_link(link: str) -> str:
    """Normalize URLs for dedup: lowercase, strip trailing slash, remove tracking params."""
    if not link:
        return ""
    link = link.strip().lower()
    parsed = urlparse(link)
    clean = urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path.rstrip("/") or "/",
        parsed.params,
        "",
        "",
    ))
    return clean

def is_valid_link(link: str) -> bool:
    """Reject non-application links: WhatsApp redirects, LinkedIn posts, emails."""
    if not link:
        return True  # no link is fine
    if "@" in link and not link.startswith("http"):
        return False
    if "whatsapp.com/channel" in link:
        return False
    if "linkedin.com/posts/" in link:
        return False
    if "linkedin.com/feed/" in link:
        return False
    return True


def deduplicate_listings_by_link(listings: list[dict]) -> list[dict]:
    seen = {}
    for item in listings:
        link = item.get("apply_link", "")
        key = normalize_link(link) if link else hashlib.sha256(
            json.dumps(item, sort_keys=True).encode()
        ).hexdigest()[:16]

        if key not in seen:
            seen[key] = item
        elif link and len(item.get("description", "")) > len(seen[key].get("description", "")):
            seen[key] = item

    deduped = list(seen.values())
    logger.info(
        f"Link dedup: {len(deduped)} kept, "
        f"{len(listings) - len(deduped)} link-duplicates removed"
    )
    return deduped


def filter_by_batch_year(listings: list[dict]) -> list[dict]:
    """Post-AI safety net: strip listings with ineligible batch years."""
    filtered = []
    for item in listings:
        eligibility = item.get("eligibility", "").lower()

        if "2027" in eligibility:
            filtered.append(item)
            continue

        has_any_year = bool(re.search(r"\b20\d{2}\b", eligibility))

        if has_any_year:
            continue

        if any(w in eligibility for w in [
            "experienced", "1+ year", "2+ year", "1-2 year",
            "2 year", "3+ year", "senior", "lead",
        ]):
            continue

        filtered.append(item)

    skipped = len(listings) - len(filtered)
    if skipped:
        logger.info(f"Batch-year filter: {len(filtered)} kept, {skipped} non-2027 removed")
    return filtered


def msg_hash(text: str) -> str:
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def load_seen_hashes(data_dir: Path, window_days: int) -> dict[str, str]:
    file = data_dir / "seen_hashes.json"
    if not file.exists():
        return {}
    try:
        data = json.loads(file.read_text())
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=window_days)
        ).isoformat()
        pruned = {h: ts for h, ts in data.items() if ts >= cutoff}
        if len(pruned) < len(data):
            file.write_text(json.dumps(pruned, indent=2))
        return pruned
    except (json.JSONDecodeError, OSError):
        return {}


def save_seen_hashes(data_dir: Path, hashes: dict[str, str]):
    file = data_dir / "seen_hashes.json"
    file.write_text(json.dumps(hashes, indent=2))


def deduplicate(
    messages: list[dict],
    seen_hashes: dict[str, str],
) -> tuple[list[dict], dict[str, str]]:
    fresh = []
    now = datetime.now(timezone.utc).isoformat()
    for msg in messages:
        h = msg_hash(msg["text"])
        if h not in seen_hashes:
            seen_hashes[h] = now
            fresh.append(msg)
    logger.info(
        f"Dedup: {len(fresh)} kept, {len(messages) - len(fresh)} skipped"
    )
    return fresh, seen_hashes


async def main():
    start = time.time()
    logger.info(f"Starting scrape run at {datetime.now()}")

    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone = os.environ.get("TELEGRAM_PHONE", "")
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    gemini_key = os.environ["GEMINI_API_KEY"]
    gemini_model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    gemini_fallback = os.environ.get("FALLBACK_MODEL", "gemini-3.5-flash")
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    channels = [
        c.strip()
        for c in os.environ.get("CHANNELS_TO_SCRAPE", "").split(",")
        if c.strip()
    ]
    max_per_channel = int(os.environ.get("MAX_MESSAGES_PER_CHANNEL", "50"))
    first_run_max = int(os.environ.get("MAX_MESSAGES_FIRST_RUN", "200"))
    first_run_days = int(os.environ.get("FIRST_RUN_DAYS", "7"))
    preferences = os.environ.get("PREFERENCES", "")
    exclude_keywords = os.environ.get("EXCLUDE_KEYWORDS", "")
    enable_dedup = os.environ.get("ENABLE_DEDUP", "true").lower() != "false"
    dedup_window_days = int(os.environ.get("DEDUP_WINDOW_DAYS", "3"))

    for name, val in [
        ("TELEGRAM_API_ID", api_id),
        ("TELEGRAM_API_HASH", api_hash),
        ("TELEGRAM_BOT_TOKEN", bot_token),
        ("TELEGRAM_CHAT_ID", chat_id),
        ("GEMINI_API_KEY", gemini_key),
    ]:
        if isinstance(val, str) and "your_" in val.lower():
            logger.error(f"Please set {name} in .env file")
            sys.exit(1)

    if not channels:
        logger.error("CHANNELS_TO_SCRAPE is empty")
        sys.exit(1)

    data_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Channels: {channels}")
    logger.info(f"Model: {gemini_model} (fallback: {gemini_fallback})")
    logger.info(f"Destination: {chat_id}")

    scraper = TelegramScraper(
        api_id=api_id, api_hash=api_hash, data_dir=str(data_dir)
    )
    sanitizer = AISanitizer(
        api_key=gemini_key,
        model=gemini_model,
        fallback_model=gemini_fallback,
        preferences=preferences,
        exclude_keywords=exclude_keywords,
    )
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    seen_hashes = (
        load_seen_hashes(data_dir, dedup_window_days)
        if enable_dedup
        else {}
    )

    await scraper.connect(phone=phone if phone else None)

    first_run = scraper.is_first_run
    if first_run:
        logger.info(
            f"First run detected — scanning last {first_run_days} days "
            f"per channel (max {first_run_max} msgs/channel)"
        )

    try:
        messages = await scraper.fetch_new_messages(
            channels,
            max_per_channel=max_per_channel,
            first_run=first_run,
            first_run_days=first_run_days,
            first_run_max=first_run_max,
        )
        logger.info(f"Fetched {len(messages)} total messages")
        (data_dir / "debug_raw.json").write_text(
            json.dumps(messages, indent=2, ensure_ascii=False)[:500000]
        )

        if not messages:
            logger.info("No new messages found")
            return

        if enable_dedup:
            messages, _ = deduplicate(messages, seen_hashes)
            save_seen_hashes(data_dir, seen_hashes)

        logger.info(f"After dedup: {len(messages)} messages")
        (data_dir / "debug_deduped.json").write_text(
            json.dumps(messages, indent=2, ensure_ascii=False)[:500000]
        )

        if not messages:
            logger.info("All messages were duplicates")
            return

        listings = sanitizer.sanitize(messages)
        (data_dir / "debug_ai_listings.json").write_text(
            json.dumps(listings, indent=2, ensure_ascii=False)[:500000]
        )

        if listings:
            listings = filter_by_batch_year(listings)
            before_link_filter = len(listings)
            listings = [l for l in listings if is_valid_link(l.get("apply_link", ""))]
            listings = [
                l for l in listings
                if not any(w in (l.get("company", "")).lower()
                          for w in ["& many more", "and many more", "and more", "top companies"])
            ]
            if len(listings) < before_link_filter:
                logger.info(f"Link filter: {len(listings)} kept, {before_link_filter - len(listings)} removed")
            listings = deduplicate_listings_by_link(listings)
            (data_dir / "debug_final.json").write_text(
                json.dumps(listings, indent=2, ensure_ascii=False)
            )
            await notifier.send_listings(listings)
        else:
            logger.info("AI returned no matching listings")

    except Exception as e:
        logger.error(f"Run failed: {e}", exc_info=True)
        try:
            await notifier.send_error(str(e)[:500])
        except Exception:
            pass
        sys.exit(1)

    finally:
        await scraper.disconnect()

    elapsed = time.time() - start
    logger.info(f"Done in {elapsed:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
