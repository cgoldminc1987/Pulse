from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    database_url: str = Field(default="postgresql+psycopg://stripe:stripe@db:5432/stripe_webhooks")
    redis_url: str = Field(default="redis://redis:6379/0")
    stripe_webhook_secret: str
    stripe_livemode: bool = False
    webhook_tolerance_seconds: int = 300
    processing_timeout_seconds: int = 900
    internal_api_key_hashes_raw: str = Field(default="", validation_alias="INTERNAL_API_KEY_HASHES")
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60
    trusted_proxy_hops: int = 0
    allowed_event_types: str = "checkout.session.completed,payment_intent.succeeded,payment_intent.payment_failed,invoice.paid,invoice.payment_failed,customer.subscription.updated,customer.subscription.deleted"
    @property
    def event_types(self) -> set[str]:
        values = {x.strip() for x in self.allowed_event_types.split(",") if x.strip()}
        return values

    @property
    def accepts_all_event_types(self) -> bool:
        return "*" in self.event_types

    @property
    def internal_api_key_hashes(self) -> set[str]:
        return {x.strip().lower() for x in self.internal_api_key_hashes_raw.split(",") if x.strip()}

@lru_cache
def get_settings() -> Settings:
    return Settings()
