from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5433/coffee"
    # A query running longer than this is cancelled, so one expensive request cannot
    # hold a pooled connection while every other request waits.
    statement_timeout_ms: int = 3000
    pool_max_size: int = 10
    # How long a request waits for a free connection before getting a 503.
    pool_timeout_s: float = 5.0


settings = Settings()
