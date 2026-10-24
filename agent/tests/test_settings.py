import pytest
from pydantic import ValidationError

from config.settings import Settings, to_ccxt_symbol


def test_default_mode_is_paper(monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    s = Settings(_env_file=None)
    assert s.trading_mode == "paper" and not s.is_live


def test_live_only_from_env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", " LIVE ")
    assert Settings(_env_file=None).is_live


def test_invalid_mode_rejected(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "yolo")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_are_frozen(monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    s = Settings(_env_file=None)
    with pytest.raises(ValidationError):
        s.trading_mode = "live"


def test_secrets_not_leaked_in_repr(monkeypatch):
    monkeypatch.setenv("BYBIT_API_KEY", "super-secret-key-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz")
    s = Settings(_env_file=None)
    text = repr(s) + str(s) + str(s.model_dump())
    assert "super-secret-key-123" not in text and "sk-ant-xyz" not in text
    assert s.bybit_api_key.get_secret_value() == "super-secret-key-123"


def test_symbols_and_mapping(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "btcusdt, ETHUSDT,")
    assert Settings(_env_file=None).symbol_list == ["BTCUSDT", "ETHUSDT"]
    assert to_ccxt_symbol("BTCUSDT") == "BTC/USDT:USDT"
    assert to_ccxt_symbol("ETH/USDT:USDT") == "ETH/USDT:USDT"
    with pytest.raises(ValueError):
        to_ccxt_symbol("BTCUSD")
