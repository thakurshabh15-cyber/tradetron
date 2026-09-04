"""Application configuration via environment variables.

All settings are loaded from ``.env`` using pydantic-settings.  Secrets are
never hard-coded — the ``.env.example`` file ships with safe placeholders.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # fastapi-template/


class Settings(BaseSettings):
    """Centralised, validated configuration for the entire platform."""

    # ── Security ─────────────────────────────────────────────────────
    jwt_secret: str = ""  # REQUIRED — must be set in .env
    jwt_algorithm: str = "HS256"  # Only HS256 (HMAC-SHA256) is implemented; others are rejected
    access_token_expire_minutes: int = 15  # Short-lived access token lifetime (production: 1440)

    # ── Broker credentials (Angel One) ───────────────────────────────
    angel_api_key: str = Field("", validation_alias=AliasChoices("angel_api_key", "ANGEL_API_KEY"))
    angel_client_id: str = Field("", validation_alias=AliasChoices("angel_client_id", "ANGEL_CLIENT_ID", "angel_client_code", "ANGEL_CLIENT_CODE"))
    angel_pin: str = Field("", validation_alias=AliasChoices("angel_pin", "ANGEL_PIN", "angel_password", "ANGEL_PASSWORD"))
    angel_totp_key: str = Field("", validation_alias=AliasChoices("angel_totp_key", "ANGEL_TOTP_KEY", "angel_totp_secret", "ANGEL_TOTP_SECRET"))
    broker_mode: str = "simulated"  # "simulated" | "live"
    environment: str = "development"  # "development" | "testing" | "production"


    @property
    def angel_client_code(self) -> str:
        return self.angel_client_id

    @property
    def angel_password(self) -> str:
        return self.angel_pin

    @property
    def angel_totp_secret(self) -> str:
        return self.angel_totp_key

    # ── Broker credentials (Zerodha Kite) ────────────────────────────
    zerodha_api_key: str = ""
    zerodha_api_secret: str = ""
    zerodha_access_token: str = ""

    # ── Broker credentials (Binance) ─────────────────────────────────
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True  # True = testnet, False = production

    # ── OAuth ────────────────────────────────────────────────────────
    google_oauth_client_id: str = ""  # Google Cloud Console OAuth client ID

    # ── Default Super-Admin account (seeded on first boot) ─────────────
    default_admin_email: str = "admin@tradethrone.com"
    # Bootstrap password for the seeded admin. Leave EMPTY (never hardcode a
    # runtime credential in source) — on first boot the seeder then generates
    # a strong random password and prints it ONCE to the logs. Operators can
    # instead set ADMIN_DEFAULT_PASSWORD in their environment to choose one.
    default_admin_password: str = ""

    # ── Monitoring & Alerting ────────────────────────────────────────
    sentry_dsn: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Resend & SMTP Email Dispatch (OTP & Notifications) ───────────
    resend_api_key: str = ""
    resend_from_email: str = "onboarding@resend.dev"
    emails_from_email: str = "otp@ecosystemofsamartinvesting.in"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@tradethrone.io"
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
    app_name: str = "TradeThrone"
    port: int = 8080
    frontend_url: str = "http://localhost:5173"
    allowed_origins: str = "*"  # Comma-separated list or "*"
    database_url: str = f"sqlite+aiosqlite:///{BASE_DIR / 'trading.db'}"
    log_level: str = "INFO"

    # ── Redis ──────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    
    # ── Webhook provider secrets ───────────────────────────────────────
    upstox_webhook_secret: str = ""
    angelone_webhook_secret: str = ""
    binance_webhook_secret: str = ""
    tradethrone_webhook_secret: str = ""
    
    # ── OTLP tracing ───────────────────────────────────────────────────
    otlp_endpoint: str = ""
    otlp_insecure: bool = True

    # ── Webhook local testing ──────────────────────────────────────────
    webhook_local_mode: bool = False  # skip signature verification & Redis
    webhook_http_port: int = 8001     # HTTP port when running locally

    # ── Production Infrastructure (managed services) ────────────────────
    upstash_redis_url: str = ""       # Managed serverless Redis w/ TLS — overrides redis_url when set
    skip_signature_verification: bool = False  # NEVER true in production (HMAC enforcement)

    # ── Additional Indian Brokers ───────────────────────────────────────
    dhan_client_id: str = ""
    dhan_access_token: str = ""
    fyers_app_id: str = ""
    fyers_access_token: str = ""

    # ── Global Feed Providers ───────────────────────────────────────────
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    oanda_api_token: str = ""

    # ── Live Payment Gateways ───────────────────────────────────────────
    stripe_secret_key: str = ""

    @property
    def effective_redis_url(self) -> str:
        """Upstash TLS Redis takes precedence over the plain redis_url."""
        return self.upstash_redis_url.strip() or self.redis_url

    # ── Risk management ──────────────────────────────────────────────
    max_position_size: int = 100
    max_daily_loss: float = 10_000.0
    max_orders_per_minute: int = 30

    # ── Market data simulator ────────────────────────────────────────
    sim_symbols: str = "AAPL,MSFT,NVDA,GOOGL,AMZN"
    sim_tick_interval: float = 0.5  # seconds between simulated ticks (500ms live cadence)

    # ── Market data freshness ──────────────────────────────────────────
    # Max age (seconds) before a *real* tick is considered STALE and must no
    # longer be presented as live. Keyed by asset-class region; the generic
    # default applies to symbols with no explicit region.
    data_freshness_crypto: float = 30.0   # Binance real ticker cadence
    data_freshness_equity: float = 15.0
    data_freshness_forex: float = 60.0
    data_freshness_commodity: float = 60.0
    data_freshness_default: float = 30.0

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

# ── Production fail-fast guards ──────────────────────────────────────────────
# Refuse to boot a production deployment with insecure/missing secrets rather
# than silently running with predictable credentials.
if settings.environment == "production":
    if not settings.jwt_secret or len(settings.jwt_secret) < 32:
        raise RuntimeError(
            "ENVIRONMENT=production requires a strong JWT_SECRET (>= 32 random "
            "characters). Set JWT_SECRET in the environment before booting."
        )
    if settings.skip_signature_verification:
        raise RuntimeError(
            "SKIP_SIGNATURE_VERIFICATION must never be true in production."
        )
