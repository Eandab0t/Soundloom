"""Retry decorator with exponential backoff for transient errors."""
import asyncio
import functools
import logging
import random

logger = logging.getLogger("bigpickle.retry")


def retry(max_attempts: int = 3, base_delay: float = 1.0,
          max_delay: float = 30.0, exceptions: tuple = (Exception,)):
    """Decorator that retries a function on specified exceptions.

    Args:
        max_attempts: Total attempts (1 = no retry).
        base_delay: Initial delay between retries in seconds.
        max_delay: Maximum delay cap.
        exceptions: Tuple of exception types to catch and retry.
    """
    def decorator(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt == max_attempts:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    delay *= (0.5 + random.random())
                    logger.warning(
                        f"Retry {attempt}/{max_attempts} for {func.__name__}: {e}. "
                        f"Waiting {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
            raise last_exc

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            import time
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt == max_attempts:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    delay *= (0.5 + random.random())
                    logger.warning(
                        f"Retry {attempt}/{max_attempts} for {func.__name__}: {e}. "
                        f"Waiting {delay:.1f}s"
                    )
                    time.sleep(delay)
            raise last_exc

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper
    return decorator
