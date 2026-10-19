"""Immutable, explicit research choices shared by scanning and replay.

These choices do not amend the accepted paper configuration. Shorter holding
limits are research exit variants; the accepted maximum remains the ceiling.
"""
from dataclasses import dataclass, fields
from decimal import Decimal
import hashlib
import json

from .sizing import decimal


@dataclass(frozen=True)
class ResearchParameters:
    h4_adx_minimum: Decimal = Decimal("25")
    m15_ema_proximity_atr: Decimal = Decimal("0")
    m15_pullback_lookback: int = 4
    m5_confirmation_window: int = 6
    max_hold_hours: Decimal = Decimal("12")
    minimum_net_rr: Decimal = Decimal("2")

    def __post_init__(self):
        for name in ("h4_adx_minimum", "m15_ema_proximity_atr", "max_hold_hours", "minimum_net_rr"):
            value = decimal(getattr(self, name), name)
            if value < 0 or value == 0 and name != "m15_ema_proximity_atr":
                raise ValueError(f"{name} must be {'nonnegative' if name == 'm15_ema_proximity_atr' else 'positive'}")
            object.__setattr__(self, name, value)
        for name, minimum in (("m15_pullback_lookback", 2), ("m5_confirmation_window", 1)):
            if type(getattr(self, name)) is not int or getattr(self, name) < minimum:
                raise ValueError(f"{name} must be an integer at least {minimum}")
        if self.h4_adx_minimum > 100:
            raise ValueError("h4_adx_minimum cannot exceed 100")
        if self.max_hold_hours > 12:
            raise ValueError("research holding limit cannot exceed accepted 12 hours")
        if self.max_hold_hours != self.max_hold_hours.to_integral_value():
            raise ValueError("research holding limit requires whole hours")

    def as_dict(self):
        return {field.name: (format(getattr(self, field.name).normalize(), "f")
                            if isinstance(getattr(self, field.name), Decimal) else getattr(self, field.name))
                for field in fields(self)}

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    @property
    def is_baseline(self):
        return self == BASELINE_PARAMETERS

    def validate_holding_ceiling(self, config):
        if self.max_hold_hours > decimal(config.holding.max_hours):
            raise ValueError("research holding limit exceeds accepted paper maximum")


BASELINE_PARAMETERS = ResearchParameters()


def effective_parameters(parameters=None):
    if parameters is None:
        return BASELINE_PARAMETERS
    if not isinstance(parameters, ResearchParameters):
        raise TypeError("parameters must be an immutable ResearchParameters instance")
    return parameters
