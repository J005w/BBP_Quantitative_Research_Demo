"""Durable, atomically enforced local simulated-paper positions only.

No terminal, broker account, manual terminal trade, or external order is managed.
Fills use observed executable quote sides. Financial slippage/commission/financing
are explicit estimates charged once, separate from quote-side spread. Existing
positions retain stop/target/12-hour exits when research pauses activate.
Daily research limits use UTC days and last observed equity at the boundary.
"""

from contextlib import contextmanager
import copy
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time

from .sizing import (RiskPolicy, decimal, json_values, normalize_request,
                     request_from_dict, request_metrics, size_position)


class LedgerError(RuntimeError):
    pass


class LedgerBusyError(LedgerError):
    pass


@dataclass(frozen=True)
class Quote:
    symbol: str
    time: float
    bid: Decimal
    ask: Decimal
    tradable: bool = True

    def normalized(self):
        if self.symbol != "BTCUSD" or type(self.tradable) is not bool:
            raise ValueError("quote must identify BTCUSD and explicit tradability")
        if isinstance(self.time, bool) or not isinstance(self.time, (int, float)) or not math.isfinite(self.time) or self.time < 0:
            raise ValueError("quote time must be finite epoch seconds")
        bid, ask = decimal(self.bid, "bid"), decimal(self.ask, "ask")
        if not 0 < bid <= ask:
            raise ValueError("quote requires positive ordered Bid/Ask")
        return Quote(self.symbol, float(self.time), bid, ask, self.tradable)

    def as_dict(self):
        value = self.normalized()
        return {"symbol": value.symbol, "time": value.time, "bid": str(value.bid),
                "ask": str(value.ask), "tradable": value.tradable}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate ledger JSON field")
        result[key] = value
    return result


def _finite_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("ledger time must be finite epoch seconds")
    return float(value)


def _quote(value):
    return (Quote(**value) if isinstance(value, dict) else value).normalized()


def _entry_invalidated(request, quote):
    """The stop is tested on the closing side, including at a proposed entry."""
    return quote.bid <= request.stop if request.side == "LONG" else quote.ask >= request.stop


class PaperLedger:
    """Every mutation reloads, validates and replaces state under an OS file lock.

    Locks are released by the OS on crash. JSON replacement is atomic; financial
    state is revalidated on restart. Duplicate IDs never duplicate positions or
    realized cash flows. A fresh quote is at most 60 seconds old at ``as_of``.
    """

    def __init__(self, path, initial_equity=None, *, policy=None, clock=None):
        self.path = Path(path)
        self.policy = policy or RiskPolicy()
        self.clock = clock or time.time
        self._thread_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked():
            if self.path.exists():
                state = self._read()
                if initial_equity is not None and decimal(initial_equity) != decimal(state["initial_equity"]):
                    raise LedgerError("Existing ledger initial equity differs; explicit new ledger/migration required.")
            else:
                equity = decimal(initial_equity, "initial equity") if initial_equity is not None else Decimal(0)
                if equity <= 0:
                    raise LedgerError("A new paper ledger requires positive initial equity.")
                state = {"schema_version": 1, "mode": "SIMULATED_PAPER", "currency": "USD",
                         "initial_equity": str(equity), "cash": str(equity), "peak_equity": str(equity),
                         "day_start_equity": str(equity), "day_key": None, "as_of": None,
                         "last_quote": None, "records": {}, "revision": 0, "policy": self.policy.as_dict()}
                self._write(state)

    @classmethod
    def create(cls, path, initial_equity, **kwargs):
        return cls(path, initial_equity, **kwargs)

    @classmethod
    def load(cls, path, **kwargs):
        return cls(path, **kwargs)

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as lock:
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise LedgerBusyError("Paper ledger is in use; retry the same idempotent action.") from error
                try:
                    yield
                finally:
                    lock.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                               parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite ledger JSON")))
            self._validate(state)
            return state
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise LedgerError("Paper ledger is invalid or unreadable; refusing to reset financial state.") from error

    def _write(self, state):
        self._validate(state)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(state, output, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _validate(self, state):
        if state["schema_version"] != 1 or state["mode"] != "SIMULATED_PAPER" or state["currency"] != "USD":
            raise ValueError("unsupported ledger schema/mode/currency")
        if state["policy"] != self.policy.as_dict():
            raise ValueError("ledger policy differs; explicit migration required")
        initial, cash, peak = (decimal(state[key], key) for key in ("initial_equity", "cash", "peak_equity"))
        decimal(state["day_start_equity"], "day start")
        if initial <= 0 or peak < initial or type(state["revision"]) is not int or state["revision"] < 0:
            raise ValueError("invalid ledger balances or revision")
        if state["as_of"] is not None:
            _finite_time(state["as_of"])
        quote = Quote(**state["last_quote"]).normalized() if state["last_quote"] is not None else None
        if quote is not None and (state["as_of"] is None or quote.time > state["as_of"] + 1):
            raise ValueError("stored quote is beyond its observed ledger clock")
        active, realized = [], Decimal(0)
        if not isinstance(state["records"], dict):
            raise ValueError("records must be keyed by unique setup IDs")
        for identifier, record in state["records"].items():
            if not isinstance(identifier, str) or not identifier or record["setup_id"] != identifier:
                raise ValueError("invalid stable setup ID")
            status = record["status"]
            if status not in {"RESERVED", "OPEN", "REJECTED", "EXPIRED", "CANCELLED", "CLOSED"}:
                raise ValueError("invalid paper lifecycle")
            _finite_time(record["created_at"])
            _finite_time(record["expires_at"])
            request = request_from_dict(record["request"])
            volume = decimal(record["volume"])
            metrics = request_metrics(request)
            if volume < 0 or (volume and (volume < request.grid.minimum or volume > request.grid.maximum or (volume - request.grid.minimum) % request.grid.step)):
                raise ValueError("paper quantity is outside the broker grid")
            if decimal(record["estimated_stop_loss"]) != volume * metrics["loss_per_lot"]:
                raise ValueError("stored paper risk disagrees with broker/cost basis")
            if volume:
                context = record["risk_context"]
                sized = size_position(request, policy=self.policy, **context)
                if not sized.accepted or sized.as_dict() != record["sizing"] or sized.volume != volume:
                    raise ValueError("stored position disagrees with its enforced sizing context")
                context_time = record.get("entered_at", record["created_at"])
                cash_at_sizing = initial + sum((decimal(other["realized_pnl"]) for other in state["records"].values()
                                               if other is not record and other["status"] == "CLOSED" and other["closed_at"] <= context_time), Decimal(0))
                if decimal(context["equity"]) != cash_at_sizing:
                    raise ValueError("sizing equity disagrees with the single-symbol paper cash history")
            if status in ("RESERVED", "OPEN"):
                if volume <= 0:
                    raise ValueError("active record requires positive volume")
                active.append(request.symbol)
            if status == "OPEN":
                if quote is None or record["deadline_at"] != record["entered_at"] + self.policy.effective_hold_hours * 3600:
                    raise ValueError("open paper position has invalid quote/holding deadline")
                _finite_time(record["entered_at"])
                if not record["created_at"] <= record["entered_at"] < record["expires_at"] or record["entered_at"] > state["as_of"]:
                    raise ValueError("entry falls outside the causal reservation window")
                if decimal(record["unrealized_pnl"]) != self._pnl(record, quote):
                    raise ValueError("paper mark disagrees with the stored executable quote")
            if status == "CLOSED":
                pnl = decimal(record["realized_pnl"])
                exit_price = decimal(record["exit_price"])
                if exit_price <= 0 or not record["entered_at"] <= record["closed_at"] <= state["as_of"]:
                    raise ValueError("invalid chronological/positive closed fill")
                sign = 1 if request.side == "LONG" else -1
                expected = volume * sign * (exit_price - request.entry) * metrics["price_value_per_lot"] - self._cash_cost(record, record["closed_at"])
                if pnl != expected:
                    raise ValueError("closed paper cash flow disagrees with fill/cost basis")
                realized += pnl
        if len(active) != len(set(active)) or cash != initial + realized:
            raise ValueError("duplicate symbol exposure or inconsistent realized cash")
        if peak < self._equity(state):
            raise ValueError("peak equity is below marked current equity")

    def _equity(self, state):
        return decimal(state["cash"]) + sum(
            (decimal(record["unrealized_pnl"]) for record in state["records"].values() if record["status"] == "OPEN"), Decimal(0))

    def _pnl(self, record, quote):
        request, volume = request_from_dict(record["request"]), decimal(record["volume"])
        metrics = request_metrics(request)
        price = quote.bid if request.side == "LONG" else quote.ask
        sign = 1 if request.side == "LONG" else -1
        return volume * sign * (price - request.entry) * metrics["price_value_per_lot"] - self._cash_cost(record, quote.time)

    def _cash_cost(self, record, at):
        """Accepted fixed scenario; offline adapters may accrue evidenced events.

        The durable paper ledger retains its original cost arithmetic. A research
        adapter must use the same hook for marks, exits and cash validation.
        """
        return decimal(record["volume"]) * request_metrics(request_from_dict(record["request"]))["cost_per_lot"]

    def _exposure(self, state, exclude=None):
        risk = gross = margin = Decimal(0)
        for identifier, record in state["records"].items():
            if identifier == exclude or record["status"] not in ("RESERVED", "OPEN"):
                continue
            volume, metrics = decimal(record["volume"]), request_metrics(request_from_dict(record["request"]))
            risk += decimal(record["estimated_stop_loss"])
            if record["status"] == "OPEN" and state["last_quote"] is not None:
                plan = request_from_dict(record["request"])
                # Conservative current Ask marks gross exposure, including shorts.
                scale = decimal(state["last_quote"]["ask"]) / plan.entry
                gross += volume * metrics["notional_per_lot"] * scale
                margin += volume * metrics["margin_per_lot"] * scale
            else:
                gross += volume * metrics["notional_per_lot"]
                margin += volume * metrics["margin_per_lot"]
        return risk, gross, margin

    def _sizing_context(self, state, exclude=None):
        risk, gross, margin = self._exposure(state, exclude)
        return {"equity": self._equity(state), "open_and_reserved_risk": risk, "crypto_risk": risk,
                "current_gross_notional": gross, "used_margin": margin,
                "day_start_equity": decimal(state["day_start_equity"]), "peak_equity": decimal(state["peak_equity"])}

    def _size(self, state, request, exclude=None):
        return size_position(request, policy=self.policy, **self._sizing_context(state, exclude))

    def _snapshot(self, state):
        equity = self._equity(state)
        risk, gross, margin = self._exposure(state)
        day, peak = decimal(state["day_start_equity"]), decimal(state["peak_equity"])
        pauses = []
        if equity <= 0:
            pauses.append("NONPOSITIVE_EQUITY")
        if self.policy.daily_loss_fraction is not None and day > 0 and day - equity >= day * self.policy.daily_loss_fraction:
            pauses.append("DAILY_LOSS_PAUSE")
        if self.policy.drawdown_fraction is not None and peak - equity >= peak * self.policy.drawdown_fraction:
            pauses.append("DRAWDOWN_PAUSE")
        return json_values({
            "mode": "SIMULATED_PAPER_ONLY", "currency": "USD", "current_equity": equity,
            "cash": decimal(state["cash"]), "initial_equity": decimal(state["initial_equity"]),
            "peak_equity": peak, "day_start_equity": day, "day_key": state["day_key"],
            "daily_window": "UTC_RESEARCH_DEFAULT_LAST_OBSERVED_BOUNDARY_EQUITY",
            "open_and_reserved_risk": risk, "crypto_risk": risk, "gross_notional": gross,
            "used_margin": margin, "per_trade_budget": max(equity, 0) * self.policy.per_trade_fraction,
            "aggregate_budget": max(equity, 0) * self.policy.total_fraction,
            "crypto_budget": max(equity, 0) * self.policy.crypto_fraction,
            "new_entries_paused": pauses, "last_quote": state["last_quote"], "as_of": state["as_of"],
            "records": copy.deepcopy(state["records"]), "revision": state["revision"],
            "reserved_setup_ids": [identifier for identifier, record in state["records"].items() if record["status"] == "RESERVED"],
            "open_setup_ids": [identifier for identifier, record in state["records"].items() if record["status"] == "OPEN"],
            "finished_setup_ids": [identifier for identifier, record in state["records"].items() if record["status"] not in ("RESERVED", "OPEN")],
            "research_controls": self.policy.as_dict(),
            "margin_model": "PAPER_BROKER_BASIS_SCALED_TO_CURRENT_ASK_NOT_TERMINAL_MARGIN",
            "external_trades_managed": False,
        })

    def _apply_quote(self, state, quote, as_of):
        quote, as_of = _quote(quote), _finite_time(as_of)
        if state["as_of"] is not None and as_of < state["as_of"]:
            raise LedgerError("Paper events cannot move backwards in time.")
        if quote.time > as_of + 1:
            raise LedgerError("Paper quote is in the future.")
        day_key = datetime.fromtimestamp(as_of, timezone.utc).date().isoformat()
        if state["day_key"] != day_key:
            state["day_key"], state["day_start_equity"] = day_key, str(self._equity(state))
        state["as_of"] = as_of
        for record in state["records"].values():
            if record["status"] == "RESERVED" and as_of >= record["expires_at"]:
                record["status"], record["reason"] = "EXPIRED", "REVIEW_EXPIRED"
        previous = state["last_quote"]
        if previous is not None and quote.time < previous["time"]:
            raise LedgerError("Out-of-order paper quote rejected.")
        if previous is not None and quote.time == previous["time"] and (quote.bid != decimal(previous["bid"]) or quote.ask != decimal(previous["ask"])):
            raise LedgerError("Conflicting prices for the same quote timestamp.")
        state["last_quote"] = quote.as_dict()
        executable = quote.tradable and -1 <= as_of - quote.time <= 60
        events = []
        for record in state["records"].values():
            if record["status"] != "OPEN":
                continue
            request = request_from_dict(record["request"])
            price = quote.bid if request.side == "LONG" else quote.ask
            record["unrealized_pnl"] = str(self._pnl(record, quote))
            stop = price <= request.stop if request.side == "LONG" else price >= request.stop
            target = price >= request.target if request.side == "LONG" else price <= request.target
            reason = "STOP" if stop else "TARGET" if target else "TIME" if as_of >= record["deadline_at"] else None
            if reason and executable:
                pnl = decimal(record["unrealized_pnl"])
                record.update(status="CLOSED", reason=reason, exit_price=str(price),
                              closed_at=as_of, fill_quote_time=quote.time, realized_pnl=str(pnl),
                              late_exit_seconds=max(0, as_of - record["deadline_at"]), unrealized_pnl="0")
                state["cash"] = str(decimal(state["cash"]) + pnl)
                events.append({"setup_id": record["setup_id"], "reason": reason, "realized_pnl": str(pnl)})
        state["peak_equity"] = str(max(decimal(state["peak_equity"]), self._equity(state)))
        return executable, events

    def _transaction(self, operation):
        with self._locked():
            state = self._read()
            result = operation(state)
            state["revision"] += 1
            self._write(state)
            result["snapshot"] = self._snapshot(state)
            return result

    def snapshot(self):
        with self._locked():
            return self._snapshot(self._read())

    def preview(self, request, quote, *, setup_id=None, as_of=None):
        """Pure current-ledger sizing view; no reservation or financial fill."""
        request, quote = normalize_request(request), _quote(quote)
        with self._locked():
            state = self._read()
            instant = _finite_time(self.clock() if as_of is None else as_of)
            reason = None
            previous = state["last_quote"]
            if ((state["as_of"] is not None and instant < state["as_of"])
                    or (previous is not None and quote.time < previous["time"])):
                reason = "OUT_OF_ORDER_QUOTE"
            elif previous is not None and quote.time == previous["time"] and (quote.bid != decimal(previous["bid"]) or quote.ask != decimal(previous["ask"])):
                reason = "CONFLICTING_QUOTE_TIMESTAMP"
            elif not quote.tradable or not -1 <= instant - quote.time <= 60:
                reason = "STALE_OR_UNTRADABLE_QUOTE"
            elif request.entry != (quote.ask if request.side == "LONG" else quote.bid):
                reason = "ENTRY_BASIS_DOES_NOT_MATCH_EXECUTABLE_QUOTE"
            elif _entry_invalidated(request, quote):
                reason = "STOP_ALREADY_INVALIDATED"
            elif any(identifier != setup_id and record["status"] in ("RESERVED", "OPEN")
                     for identifier, record in state["records"].items()):
                reason = "ONE_POSITION_OR_RESERVATION_PER_SYMBOL"
            elif setup_id in state["records"] and state["records"][setup_id]["status"] == "OPEN":
                reason = "ALREADY_OPEN"
            for record in state["records"].values():
                if record["status"] == "OPEN":
                    record["unrealized_pnl"] = str(self._pnl(record, quote))
            state["last_quote"] = quote.as_dict()
            result = self._size(state, request, exclude=setup_id)
            if reason is not None:
                result = replace(result, accepted=False, reason=reason, volume=Decimal(0),
                                 estimated_stop_loss=Decimal(0), net_target_profit=Decimal(0), margin=Decimal(0), notional=Decimal(0))
            return result

    def on_quote(self, quote, *, as_of=None):
        def operation(state):
            executable, events = self._apply_quote(state, quote, self.clock() if as_of is None else as_of)
            return {"accepted": True, "quote_executable": executable, "events": events}
        return self._transaction(operation)

    def reserve(self, setup_id, request, *, expires_at, quote, confirmed=False, as_of=None):
        request, quote = normalize_request(request), _quote(quote)
        if not isinstance(setup_id, str) or not setup_id.strip() or len(setup_id) > 200:
            raise ValueError("a stable nonempty setup ID is required")
        expiry = _finite_time(expires_at)
        def operation(state):
            executable, events = self._apply_quote(state, quote, self.clock() if as_of is None else as_of)
            if setup_id in state["records"]:
                record = state["records"][setup_id]
                existing = request_from_dict(record["request"])
                if (existing.symbol, existing.side, existing.stop, existing.target) != (request.symbol, request.side, request.stop, request.target):
                    return {"accepted": False, "status": record["status"], "reason": "SETUP_ID_CONFLICT", "events": events}
                return {"accepted": record["status"] in ("RESERVED", "OPEN"), "status": record["status"],
                        "reason": "IDEMPOTENT_EXISTING_ID", "events": events}
            result = self._size(state, request)
            reason = result.reason
            if confirmed is not True:
                reason = "NOT_CONFIRMED"
            elif not executable:
                reason = "STALE_OR_UNTRADABLE_QUOTE"
            elif request.entry != (quote.ask if request.side == "LONG" else quote.bid):
                reason = "ENTRY_BASIS_DOES_NOT_MATCH_EXECUTABLE_QUOTE"
            elif _entry_invalidated(request, quote):
                reason = "STOP_ALREADY_INVALIDATED"
            elif not state["as_of"] < expiry <= state["as_of"] + 300:
                reason = "INVALID_REVIEW_EXPIRY"
            elif any(record["status"] in ("RESERVED", "OPEN") for record in state["records"].values()):
                reason = "ONE_POSITION_OR_RESERVATION_PER_SYMBOL"
            accepted = result.accepted and reason == result.reason
            state["records"][setup_id] = {
                "setup_id": setup_id, "status": "RESERVED" if accepted else "REJECTED", "reason": reason,
                "created_at": state["as_of"], "expires_at": expiry, "request": request.as_dict(),
                "volume": str(result.volume if accepted else Decimal(0)),
                "estimated_stop_loss": str(result.estimated_stop_loss if accepted else Decimal(0)),
                "unrealized_pnl": "0", "sizing": result.as_dict(),
                "risk_context": json_values(self._sizing_context(state)),
            }
            return {"accepted": accepted, "status": state["records"][setup_id]["status"], "reason": reason, "events": events}
        return self._transaction(operation)

    def enter(self, setup_id, request, quote, *, as_of=None):
        request, quote = normalize_request(request), _quote(quote)
        def operation(state):
            executable, events = self._apply_quote(state, quote, self.clock() if as_of is None else as_of)
            record = state["records"].get(setup_id)
            if record is None or record["status"] != "RESERVED":
                return {"accepted": bool(record and record["status"] == "OPEN"),
                        "status": record["status"] if record else "NOT_RESERVED", "reason": "IDEMPOTENT_OR_NOT_RESERVED", "events": events}
            old = request_from_dict(record["request"])
            reason = None
            if not executable:
                reason = "STALE_OR_UNTRADABLE_QUOTE"
            elif (request.symbol, request.side, request.stop) != (old.symbol, old.side, old.stop):
                reason = "STRUCTURAL_STOP_OR_DIRECTION_CHANGED"
            elif request.entry != (quote.ask if request.side == "LONG" else quote.bid):
                reason = "ENTRY_BASIS_DOES_NOT_MATCH_EXECUTABLE_QUOTE"
            elif _entry_invalidated(request, quote):
                reason = "STOP_ALREADY_INVALIDATED"
            result = self._size(state, request, exclude=setup_id)
            if reason is not None or not result.accepted:
                record.update(status="CANCELLED", reason=reason or result.reason)
                return {"accepted": False, "status": "CANCELLED", "reason": record["reason"], "events": events}
            record.update(status="OPEN", reason="ENTERED_SIMULATED_PAPER", request=request.as_dict(),
                          volume=str(result.volume), estimated_stop_loss=str(result.estimated_stop_loss), sizing=result.as_dict(),
                          entered_at=state["as_of"], entry_quote_time=quote.time,
                          deadline_at=state["as_of"] + self.policy.effective_hold_hours * 3600)
            record["risk_context"] = json_values(self._sizing_context(state, exclude=setup_id))
            record["unrealized_pnl"] = str(self._pnl(record, quote))
            return {"accepted": True, "status": "OPEN", "reason": record["reason"], "events": events}
        return self._transaction(operation)

    def reject(self, setup_id, *, reason="MANUAL_REJECTION"):
        def operation(state):
            record = state["records"].get(setup_id)
            if record is None:
                return {"accepted": False, "status": "NOT_FOUND", "reason": "NOT_FOUND"}
            if record["status"] == "RESERVED":
                record.update(status="CANCELLED", reason=str(reason))
            return {"accepted": record["status"] == "CANCELLED", "status": record["status"], "reason": record["reason"]}
        return self._transaction(operation)

    cancel = reject
