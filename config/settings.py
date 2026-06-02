from enum import Enum
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Enums ────────────────────────────────────────────────────────

class LogLevel(str, Enum):
    DEBUG    = "DEBUG"
    INFO     = "INFO"
    WARNING  = "WARNING"
    ERROR    = "ERROR"
    CRITICAL = "CRITICAL"


class TelegramMode(str, Enum):
    ALL       = "ALL"
    IMPORTANT = "IMPORTANT"
    OFF       = "OFF"


class BrokerType(str, Enum):
    PAPER          = "paper"
    BYBIT          = "bybit"
    BYBIT_TESTNET  = "bybit_testnet"


# ── Секции ───────────────────────────────────────────────────────

class KeysSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KEYS_")

    keys_file: str = "keys.enc"


class LoggingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LOG_")

    level:         LogLevel     = LogLevel.INFO
    folder:        str          = "logs/"
    max_bytes:     int          = 10 * 1024 * 1024  # 10 MB
    backup_count:  int          = 10
    telegram_mode: TelegramMode = TelegramMode.IMPORTANT

    @field_validator("max_bytes")
    @classmethod
    def max_bytes_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_bytes должен быть больше 0")
        return v

    @field_validator("backup_count")
    @classmethod
    def backup_count_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("backup_count должен быть больше 0")
        return v


class TelegramSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TELEGRAM_")

    mode:             TelegramMode = TelegramMode.IMPORTANT
    timeout_sec:      float        = 10.0
    max_per_minute:   int          = 20
    dedup_window_sec: int          = 300

    @field_validator("max_per_minute")
    @classmethod
    def max_per_minute_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_per_minute должен быть больше 0")
        return v

    @field_validator("timeout_sec")
    @classmethod
    def timeout_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("timeout_sec должен быть больше 0")
        return v


class BrokerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BROKER_",
        populate_by_name=True,   # разрешает обращение и по alias, и по имени поля
    )

    # Поле названо broker_type, но читается из env BROKER_TYPE через alias.
    # alias="TYPE" + env_prefix="BROKER_" → env var BROKER_TYPE.
    # bot.py использует settings.broker.broker_type — имя поля совпадает.
    broker_type: BrokerType = Field(
        default=BrokerType.PAPER,
        alias="TYPE",
    )

    request_timeout_sec:   float = 5.0
    retry_delay_sec:       float = 1.0
    max_retries:           int   = 3
    paper_initial_balance: float = 1000.0
    paper_commission_pct:  float = 0.1
    paper_slippage_pct:    float = 0.05

    # ── Stop-Loss: жёсткий риск-лимит оператора ──────────────────
    # Максимально допустимое проскальзывание при рыночном закрытии по SL.
    # Static config (не JSONB) — WFO не должен иметь возможность поднять
    # этот лимит без осознанного действия оператора.
    # Проверяется в Close Protocol шаг 9 перед отправкой MARKET ордера.
    # Превышение → STOP_CRANE (emit SL_CLOSE_BLOCKED).
    # Env var: BROKER_SL_MAX_MARKET_SLIPPAGE_PCT
    sl_max_market_slippage_pct: float = 1.0

    @field_validator("request_timeout_sec", "retry_delay_sec")
    @classmethod
    def must_be_positive_float(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Значение должно быть больше 0")
        return v

    @field_validator("max_retries")
    @classmethod
    def must_be_positive_int(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Значение должно быть больше 0")
        return v

    @field_validator("sl_max_market_slippage_pct")
    @classmethod
    def sl_slippage_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("sl_max_market_slippage_pct должен быть больше 0")
        return v


class MarketSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MARKET_")

    provider:            str   = "bybit"
    ticker:              str   = "BTCUSDT"
    timeframe:           str   = "1h"
    stale_threshold_sec: int   = 30
    poll_interval_sec:   int   = 10
    reconnect_delay_sec: int   = 1
    max_reconnect_sec:   int   = 30
    spike_threshold_pct: float = 10.0
    max_spread_pct:      float = 1.0


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DB_")

    host:     str = "localhost"
    port:     int = 5432
    name:     str = "thebot"
    user:     str = "postgres"
    password: str = ""

    @property
    def url(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )


# ── Корневой AppSettings ─────────────────────────────────────────

class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    keys:     KeysSettings     = KeysSettings()
    logging:  LoggingSettings  = LoggingSettings()
    telegram: TelegramSettings = TelegramSettings()
    broker:   BrokerSettings   = BrokerSettings()
    market:   MarketSettings   = MarketSettings()
    database: DatabaseSettings = DatabaseSettings()
