import asyncio
import functools
import logging
import random
import time
from typing import Callable, TypeVar

logger = logging.getLogger("utils")

T = TypeVar("T")


def retry_sync(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
):
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt == max_retries - 1:
                        raise
                    delay = min(base_delay * (2**attempt), max_delay)
                    jitter = random.uniform(0, delay * 0.3)
                    total = delay + jitter
                    logger.warning(
                        f"{func.__name__} attempt {attempt+1}/{max_retries} "
                        f"failed: {e}. Retrying in {total:.1f}s..."
                    )
                    time.sleep(total)
            raise last_exc

        return wrapper

    return decorator


def retry_async(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt == max_retries - 1:
                        raise
                    delay = min(base_delay * (2**attempt), max_delay)
                    jitter = random.uniform(0, delay * 0.3)
                    total = delay + jitter
                    logger.warning(
                        f"{func.__name__} attempt {attempt+1}/{max_retries} "
                        f"failed: {e}. Retrying in {total:.1f}s..."
                    )
                    await asyncio.sleep(total)
            raise last_exc

        return wrapper

    return decorator
