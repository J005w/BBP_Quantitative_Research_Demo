"""Exact paper budget arithmetic, not position sizing or risk enforcement."""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from .config import PaperConfig


@dataclass(frozen=True)
class RiskBudgets:
    currency: str
    per_trade: Decimal
    total: Decimal
    group: Decimal


def initial_risk_budgets(config: PaperConfig) -> RiskBudgets:
    """Compute budgets from configured initial paper equity, not live equity."""
    equity = config.account.initial_balance
    fractions = (
        config.risk.per_trade_fraction, config.risk.total_fraction,
        config.risk.group_fraction,
    )
    # Multiplication needs enough significant digits to preserve exact inputs.
    with localcontext() as context:
        context.prec = max(
            28, len(equity.as_tuple().digits)
            + max(len(value.as_tuple().digits) for value in fractions) + 2,
        )
        per_trade, total, group = (equity * value for value in fractions)
    return RiskBudgets(config.account.currency, per_trade, total, group)
