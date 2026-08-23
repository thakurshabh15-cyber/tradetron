"""Application configuration via environment variables.

All settings are loaded from ``.env`` using pydantic-settings.  Secrets are
never hard-coded — the ``.env.example`` file ships with safe placeholders.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # fastapi-template/


class Settings(BaseSettings):
    """Centralised, validated configuration for the entire platform."""

    # ── Security ─────────────────────────────────────────────────────
    jwt_secret: str = ""  # REQUIRED — must be set in .env

    # ── Broker credentials (Angel One) ───────────────────────────────
    angel_api_key: str = ""
    angel_client_code: str = ""
    angel_password: str = ""
    angel_totp_secret: str = ""
    broker_mode: str = "simulated"  # "simulated" | "live"

    # ── Broker credentials (Zerodha Kite) ────────────────────────────
    zerodha_api_key: str = ""
    zerodha_api_secret: str = ""

    # ── Broker credentials (Binance) ─────────────────────────────────
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True  # True = testnet, False = production

    # ── OAuth ────────────────────────────────────────────────────────
    google_oauth_client_id: str = ""  # Google Cloud Console OAuth client ID

    # ── Monitoring & Alerting ────────────────────────────────────────
    sentry_dsn: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_otp_template_name: str = ""

    # ── Resend & SMTP Email Dispatch (OTP & Notifications) ───────────
    resend_api_key: str = ""
    resend_from_email: str = "onboarding@resend.dev"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@tradetron.io"
    smtp_tls: bool = True

    # SMS Providers (Twilio or MSG91)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    msg91_auth_key: str = ""
    msg91_sender_id: str = ""
    # ── Razorpay Payment Gateway ─────────────────────────────────────
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # Registration policy: whether registration requires email/phone OTP verification before activation
    require_registration_verification: bool = True

    # ── Market Data Feed Modes ───────────────────────────────────────
    feed_mode_equity: str = "demo"   # "demo" | "live"
    feed_mode_crypto: str = "demo"   # "demo" | "live"
    feed_mode_forex: str = "demo"    # always demo (no free real-time forex API)

    # ── Application ──────────────────────────────────────────────────
    app_name: str = "Tradetron"
    port: int = 8080
    frontend_url: str = "http://localhost:5173"
    allowed_origins: str = "*"  # Comma-separated list or "*"
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR / 'trading.db'}"
    log_level: str = "INFO"
    environment: str = "production"  # "development" | "production"

    # ── Risk management ──────────────────────────────────────────────
    max_position_size: int = 100
    max_daily_loss: float = 10_000.0
    max_orders_per_minute: int = 30

    # ── Market data simulator ────────────────────────────────────────
    sim_symbols: str = "AAPL,MSFT,NVDA,GOOGL,AMZN"
    sim_tick_interval: float = 1.0  # seconds between simulated ticks

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def sim_symbol_list(self) -> list[str]:
        """Parse comma-separated symbol string into a list."""
        return [s.strip().upper() for s in self.sim_symbols.split(",") if s.strip()]


settings = Settings()
