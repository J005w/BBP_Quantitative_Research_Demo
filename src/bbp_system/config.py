"""Strict loading of the offline synthetic demonstration configuration."""

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any


class ConfigError(ValueError):
    """The supplied file is not an admissible paper configuration."""


@dataclass(frozen=True)
class Account:
    currency: str
    initial_balance: Decimal


@dataclass(frozen=True)
class RiskLimits:
    per_trade_fraction: Decimal
    total_fraction: Decimal
    group_fraction: Decimal
    max_positions_per_symbol: int


@dataclass(frozen=True)
class HoldingRules:
    max_hours: int
    allow_overnight: bool
    allow_weekend: bool


@dataclass(frozen=True)
class StrategySettings:
    name: str
    bbp_ema_length: int
    di_length: int
    adx_smoothing: int
    atr_length: int
    pivot_left: int
    pivot_right: int


@dataclass(frozen=True)
class BrokerSettings:
    preferred: str
    platform: str
    server: str | None
    qualification: str


@dataclass(frozen=True)
class PaperConfig:
    project: str
    mode: str
    manual_review: bool
    automatic_orders: bool
    account: Account
    instruments: tuple[str, ...]
    timeframes: tuple[tuple[str, str], ...]
    risk: RiskLimits
    holding: HoldingRules
    strategy: StrategySettings
    broker: BrokerSettings


ROOT_FIELDS = {
    "project", "mode", "manual_review", "automatic_orders", "account",
    "instruments", "timeframes", "risk", "holding", "strategy", "broker",
}
INSTRUMENTS = {"XAUUSD", "BTCUSD", "XAGUSD", "ETHUSD"}
TIMEFRAMES = {
    "regime": "H4", "bias": "H1", "setup": "M15", "confirmation": "M5",
}


def _object(value: Any, name: str, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    missing = fields - value.keys()
    extra = value.keys() - fields
    if missing:
        raise ConfigError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ConfigError(f"{name} has unsupported fields: {', '.join(sorted(extra))}")
    return value


def _number(value: Any, name: str) -> Decimal:
    # JSON floats are parsed directly as Decimal, never through binary float.
    if type(value) is int:
        result = Decimal(value)
    elif isinstance(value, Decimal):
        result = value
    else:
        raise ConfigError(f"{name} must be a JSON number")
    if not result.is_finite():
        raise ConfigError(f"{name} must be finite")
    if result <= 0:
        raise ConfigError(f"{name} must be positive")
    return result


def _fraction(value: Any, name: str) -> Decimal:
    result = _number(value, name)
    if result > 1:
        raise ConfigError(f"{name} must not exceed 1")
    return result


def _positive_integer(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{name} must be true or false")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a nonempty string")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ConfigError(f"non-finite JSON number is not allowed: {value}")


def load_config(path: str | Path) -> PaperConfig:
    """Load and validate, without establishing any broker connection."""
    try:
        with Path(path).open("r", encoding="utf-8-sig") as source:
            data = json.load(
                source, parse_float=Decimal,
                parse_constant=_reject_constant, object_pairs_hook=_unique_object,
            )
    except ConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read configuration: {error}") from error
    return parse_config(data)


def parse_config(data: Any) -> PaperConfig:
    """Validate a decoded object. Direct numeric inputs must be int or Decimal."""
    root = _object(data, "configuration", ROOT_FIELDS)
    if root["project"] != "BBP_Quantitative_Research_Demo":
        raise ConfigError("project must be BBP_Quantitative_Research_Demo")
    if root["mode"] != "paper":
        raise ConfigError("only paper mode is supported")
    if root["manual_review"] is not True:
        raise ConfigError("manual_review must be true")
    if root["automatic_orders"] is not False:
        raise ConfigError("automatic_orders must be false")

    account = _object(root["account"], "account", {"currency", "initial_balance"})
    currency = _text(account["currency"], "account.currency")
    if re.fullmatch(r"[A-Z]{3}", currency) is None:
        raise ConfigError("account.currency must be a three-letter uppercase code")
    balance = _number(account["initial_balance"], "account.initial_balance")

    instruments = root["instruments"]
    if (
        not isinstance(instruments, list)
        or not all(isinstance(item, str) for item in instruments)
        or len(instruments) != len(INSTRUMENTS)
        or set(instruments) != INSTRUMENTS
    ):
        raise ConfigError("instruments must contain each of XAUUSD, BTCUSD, XAGUSD and ETHUSD once")
    timeframes = _object(root["timeframes"], "timeframes", set(TIMEFRAMES))
    if timeframes != TIMEFRAMES:
        raise ConfigError("timeframes must be regime H4, bias H1, setup M15 and confirmation M5")

    risk = _object(root["risk"], "risk", {
        "per_trade_fraction", "total_fraction", "group_fraction", "max_positions_per_symbol",
    })
    per_trade = _fraction(risk["per_trade_fraction"], "risk.per_trade_fraction")
    total = _fraction(risk["total_fraction"], "risk.total_fraction")
    group = _fraction(risk["group_fraction"], "risk.group_fraction")
    if not per_trade <= group <= total:
        raise ConfigError("risk caps must satisfy per_trade_fraction <= group_fraction <= total_fraction")
    positions = _positive_integer(risk["max_positions_per_symbol"], "risk.max_positions_per_symbol")
    if positions != 1:
        raise ConfigError("this foundation permits one position per symbol")

    holding = _object(root["holding"], "holding", {"max_hours", "allow_overnight", "allow_weekend"})
    holding_rules = HoldingRules(
        _positive_integer(holding["max_hours"], "holding.max_hours"),
        _boolean(holding["allow_overnight"], "holding.allow_overnight"),
        _boolean(holding["allow_weekend"], "holding.allow_weekend"),
    )

    strategy = _object(root["strategy"], "strategy", {
        "name", "bbp_ema_length", "di_length", "adx_smoothing", "atr_length", "pivot_left", "pivot_right",
    })
    if strategy["name"] != "trend_continuation_v1":
        raise ConfigError("only the proposed trend_continuation_v1 research baseline is configured")
    strategy_settings = StrategySettings(
        name=strategy["name"],
        **{
            name: _positive_integer(strategy[name], f"strategy.{name}")
            for name in (
                "bbp_ema_length", "di_length", "adx_smoothing", "atr_length", "pivot_left", "pivot_right",
            )
        },
    )

    broker = _object(root["broker"], "broker", {"preferred", "platform", "server", "qualification"})
    preferred = _text(broker["preferred"], "broker.preferred")
    if broker["platform"] != "OFFLINE":
        raise ConfigError("the demonstration platform must be OFFLINE")
    if broker["qualification"] != "pending":
        raise ConfigError("broker qualification remains pending in this foundation")
    server = broker["server"]
    if server is not None:
        raise ConfigError("the offline demonstration cannot configure a broker server")

    return PaperConfig(
        project=root["project"], mode="paper", manual_review=True, automatic_orders=False,
        account=Account(currency, balance), instruments=tuple(instruments),
        timeframes=tuple(TIMEFRAMES.items()),
        risk=RiskLimits(per_trade, total, group, positions), holding=holding_rules,
        strategy=strategy_settings,
        broker=BrokerSettings(preferred, "OFFLINE", None, "pending"),
    )
