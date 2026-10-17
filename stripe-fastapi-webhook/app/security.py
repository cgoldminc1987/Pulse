import hashlib
import hmac
import time
from dataclasses import dataclass
from redis.asyncio import Redis
from .config import get_settings

@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after: int

class RateLimiter:
    def __init__(self, redis: Redis):
        self.redis = redis
        self.settings = get_settings()

    async def check(self, bucket: str, limit: int | None = None, window: int | None = None) -> RateLimitResult:
        limit = limit or self.settings.rate_limit_requests
        window = window or self.settings.rate_limit_window_seconds
        now = int(time.time())
        key = f"ratelimit:{bucket}:{now // window}"
        try:
            count = await self.redis.incr(key)
            if count == 1:
                await self.redis.expire(key, window + 1)
        except Exception:
            # Fail closed: returning 503 lets Stripe retry instead of admitting
            # unbounded traffic when the limiter is unavailable.
            raise
        allowed = count <= limit
        retry_after = window - (now % window) if not allowed else 0
        return RateLimitResult(allowed, max(0, limit - count), retry_after)

def digest_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

def valid_internal_api_key(raw_key: str | None) -> bool:
    if not raw_key:
        return False
    candidate = digest_api_key(raw_key)
    return any(hmac.compare_digest(candidate, configured) for configured in get_settings().internal_api_key_hashes)
