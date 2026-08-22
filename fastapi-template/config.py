from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    angel_api_key: str = ""
    angel_client_code: str = ""
    angel_password: str = ""
    angel_totp_secret: str = ""

    frontend_url: str = "http://localhost:5173"
    database_url: str = "sqlite+aiosqlite:///./trading.db"

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()