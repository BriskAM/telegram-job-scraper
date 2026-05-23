import json
import logging
import time

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 240000  # ~60K tokens (4 chars/token conservative)
MAX_OUTPUT_TOKENS = 32768

SYSTEM_PROMPT = """You are a job/internship listing curator. Your task is to filter Telegram messages and output structured JSON.

CRITICAL — ERR ON THE SIDE OF INCLUSION. If unsure whether a listing matches, INCLUDE it. False positives are better than false negatives.

RULES:
1. Surface ALL internship and entry-level job listings from the messages below.
2. BATCH YEAR FILTER:
   - INCLUDE if eligibility mentions "2027" in ANY form (e.g. "2027 batch", "2026/2027")
   - INCLUDE if eligibility mentions NO batch year at all AND the role sounds entry-level (intern, fresher, SDE-1, 0-N years, "0 experience", "students", "any batch")
   - INCLUDE if batch is "2025/2026/2027" or similar — 2027 is present, so include it
   - INCLUDE roles described as "Intern", "Fresher", "Trainee", "Entry level", "0-1 years", "0 experience" — regardless of batch mention
   - EXCLUDE ONLY if: batch is explicitly "2026" alone, "2028" alone, "2025" alone, or requires "2+ years experience", "senior", "lead"
3. Remove: spam, scams, crypto offers, coaching course ads, bank account offers, referral requests, "DM me for job", competition ads, "Win prizes", "register here to win", empty job descriptions, and any listing where the apply_link is an email address

OUTPUT FORMAT:
[
  {
    "role": "Role Title",
    "company": "Company Name",
    "description": "One-line summary",
    "eligibility": "Batch year / experience level",
    "location": "City",
    "apply_link": "https://...",
    "posted_date": "2026-05-23"
  }
]

IMPORTANT:
- apply_link must be from the message. Never make one up. If none, set to ""
- posted_date: copy the date from the message header exactly
- Keep descriptions ONE line
- Do NOT include a "source" field
- If in doubt whether to include, INCLUDE IT"""


class AISanitizer:
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-3.1-flash-lite",
        fallback_model: str = "gemini-3.5-flash",
        preferences: str = "",
        exclude_keywords: str = "",
    ):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.fallback_model = fallback_model
        self.preferences = preferences
        self.exclude_keywords = exclude_keywords

    def _try_generate(self, model: str, system: str, contents: str) -> str:
        response = self.client.models.generate_content(
            model=model,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.2,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
            contents=contents,
        )
        return response.text.strip() if response.text else "[]"

    def sanitize(self, messages: list[dict]) -> list[dict]:
        if not messages:
            return []

        system = SYSTEM_PROMPT
        if self.preferences:
            system += (
                f"\n\nUSER PREFERENCES (these are the ONLY listings you should include):\n"
                f"{self.preferences}"
                + (
                    f"\n\nABSOLUTELY EXCLUDE listings matching these keywords: {self.exclude_keywords}"
                    if self.exclude_keywords
                    else ""
                )
            )

        chunks = self._chunk_messages(messages, MAX_INPUT_CHARS)
        all_listings = []
        batch_delay = 12

        for i, chunk in enumerate(chunks):
            conversation = "\n".join(
                f"[{m['channel']}] ({m['date']})\n{m['text']}\n---"
                for m in chunk
            )
            est_tokens = len(conversation) // 4

            logger.info(
                f"Batch {i + 1}/{len(chunks)}: "
                f"{len(chunk)} msgs, ~{est_tokens} tokens"
            )

            if i > 0:
                logger.info(
                    f"Waiting {batch_delay}s between batches..."
                )
                time.sleep(batch_delay)

            raw = self._generate_with_fallback(system, conversation)

            if raw is not None:
                batch_listings = self._parse_response(raw)
                logger.info(
                    f"  Batch {i + 1}: {len(batch_listings)} listings"
                )
                all_listings.extend(batch_listings)
            else:
                logger.error(
                    f"  Batch {i + 1}: failed after all attempts, skipping"
                )

        logger.info(
            f"Total: {len(all_listings)} listings from {len(messages)} msgs"
        )
        return all_listings

    def _generate_with_fallback(
        self, system: str, conversation: str
    ) -> str | None:
        for attempt in range(3):
            try:
                return self._try_generate(self.model, system, conversation)
            except Exception as e:
                err = str(e)
                if attempt < 2 and (
                    "429" in err
                    or "503" in err
                    or "RESOURCE_EXHAUSTED" in err
                    or "UNAVAILABLE" in err
                    or "timed out" in err.lower()
                ):
                    if attempt == 0:
                        wait = 20
                    else:
                        wait = 40
                    logger.warning(
                        f"Primary model attempt {attempt + 1} failed: "
                        f"{err[:100]}... Waiting {wait}s"
                    )
                    time.sleep(wait)
                else:
                    logger.warning(
                        f"Primary model failed: {err[:100]}..."
                    )

        logger.info(
            f"Trying fallback model: {self.fallback_model}"
        )
        try:
            return self._try_generate(
                self.fallback_model, system, conversation
            )
        except Exception as e:
            logger.error(
                f"Fallback model also failed: {str(e)[:100]}..."
            )
            return None

    def _chunk_messages(
        self, messages: list[dict], max_chars: int
    ) -> list[list[dict]]:
        chunks = []
        current_chunk = []
        current_chars = 0

        for msg in messages:
            formatted = (
                f"[{msg['channel']}] ({msg['date']})\n{msg['text']}\n---"
            )
            msg_chars = len(formatted)

            if (
                current_chars + msg_chars > max_chars
                and current_chunk
            ):
                chunks.append(current_chunk)
                current_chunk = []
                current_chars = 0

            current_chunk.append(msg)
            current_chars += msg_chars

        if current_chunk:
            chunks.append(current_chunk)

        logger.info(
            f"Split {len(messages)} messages into {len(chunks)} chunks "
            f"(max {max_chars} chars/chunk)"
        )
        return chunks

    def _parse_response(self, raw: str) -> list[dict]:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("[")
            end = raw.rfind("]")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    pass
            logger.warning(f"Could not parse JSON: {raw[:200]}...")
            return []
