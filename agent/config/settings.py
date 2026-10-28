"""Runtime settings, read from environment / .env only.

TRADING_MODE is read-only for the program: there is no code path that writes it.
Secrets are SecretStr so they never show up in repr() or logs.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# Exchange-native symbol -> ccxt unified linear-perp symbol.
def to_ccxt_symbol(symbol: str) -> str:
    symbol = symbol.upper()
    if "/" in symbol:
        return symbol
    if not symbol.endswith("USDT"):
        raise ValueError(f"only USDT perps supported, got {symbol}")
    return f"{symbol[:-4]}/USDT:USDT"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore", frozen=True
    )

    trading_mode: Literal["paper", "live"] = "paper"
    exchange: Literal["bybit", "binanceusdm"] = "bybit"
    analyst_mode: Literal["llm", "rules"] = "llm"
    cloud_dashboard_url: str = ""
    cloud_dashboard_token: SecretStr = SecretStr("")
    symbols: str = "BTCUSDT,ETHUSDT"
    universe_enabled: bool = False
    universe_size: int = 20
    universe_min_volume: float = 50_000_000
    history_days: int = 120
    multiframe_enabled: bool = False
    multiframe_batch_size: int = 3
    active_demo_enabled: bool = False
    experimental_demo_enabled: bool = False
    legacy_duration_enabled: bool = False

    @field_validator('multiframe_batch_size')
    @classmethod
    def _batch(cls,value):
        if not 1<=value<=10: raise ValueError('scan batch must be 1..10')
        return value
    entry_margin_pct: float = 0.0  # zero retains risk-based sizing; user profile uses 10
    entry_leverage: float = 1.0
    max_hold_minutes: int = 0
    stalled_exit_minutes: int = 0

    @field_validator('entry_margin_pct')
    @classmethod
    def _margin(cls, value):
        if not 0 <= value <= 100: raise ValueError('margin must be 0..100')
        return value

    @field_validator('entry_leverage')
    @classmethod
    def _leverage(cls, value):
        if not 1 <= value <= 10: raise ValueError('leverage must be 1..10')
        return value

    @field_validator('max_hold_minutes','stalled_exit_minutes')
    @classmethod
    def _holding(cls, value):
        if not 0 <= value <= 1440: raise ValueError('holding must be 0..1440 minutes')
        return value
    database_url: str = f"sqlite:///{(ROOT / 'futures_agent.db').as_posix()}"

    bybit_api_key: SecretStr = SecretStr("")
    bybit_api_secret: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    @field_validator('universe_size')
    @classmethod
    def _universe_bound(cls, value):
        if not 2 <= value <= 30:
            raise ValueError('universe_size must be between 2 and 30')
        return value

    @field_validator("trading_mode", mode="before")
    @classmethod
    def _normalize_mode(cls, v):
        return str(v).strip().lower() if v is not None else "paper"

    @property
    def symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols.split(",") if s.strip()]

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"


@lru_cache
def get_settings() -> Settings:
    return Settings()
