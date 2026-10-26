"""Compensation rules engine (pure; WP-B).

``assess()`` decides whether a trip qualifies for NS "geld terug bij vertraging" and how much it
is worth, given a measured delay (from the matcher / NS flag). All numbers come from
``rules.yaml`` (see ``config.example/rules.yaml``), never from code.

``reason`` always starts with a machine-readable code followed by a colon, so pipeline code
can distinguish cases without parsing prose:

``eligible``, ``excluded_operator``, ``no_checkout``, ``below_threshold``, ``no_delay_data``,
``deadline_passed``, ``below_min_payout``.

Rules file lookup order (``load_rules``): explicit path, ``CONFIG_DIR/rules.yaml``, the copy
bundled with the package (``treinvertraging/defaults/rules.yaml``), the repo's
``config.example/rules.yaml``.
"""

from __future__ import annotations

import calendar
import os
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from treinvertraging.domain import DelayAssessment, TripDTO
from treinvertraging.errors import TreinvertragingError

_TZ = ZoneInfo("Europe/Amsterdam")
_CENT = Decimal("0.01")


class RulesError(TreinvertragingError):
    """rules.yaml is missing, unreadable or invalid."""


class Tier(BaseModel):
    min: int = Field(ge=0)  # minutes late at the final destination
    pct: int = Field(ge=0, le=100)


def _check_tiers(tiers: list[Tier]) -> list[Tier]:
    out = sorted(tiers, key=lambda t: t.min)
    if len({t.min for t in out}) != len(out):
        raise ValueError("duplicate tier minimum")
    return out


class SubscriptionProduct(BaseModel):
    match: str  # case-insensitive substring of TripDTO.product
    monthly_amount_eur: Decimal | None = None
    thresholds: list[Tier]  # pct = percent of monthly_amount_eur

    _sorted = field_validator("thresholds")(_check_tiers)


class SubscriptionConfig(BaseModel):
    enabled: bool = False
    products: list[SubscriptionProduct] = Field(default_factory=list)


class Rules(BaseModel):
    deadline_months: int = Field(default=3, ge=0)
    min_payout_eur: Decimal = Decimal("2.20")
    require_checkout: bool = True
    cancelled_counts_as_min: int = Field(default=60, ge=0)
    trust_ns_flag_without_data: bool = True
    thresholds: dict[str, list[Tier]]
    default_profile: str = "ns_domestic"
    operator_profiles: dict[str, str] = Field(default_factory=dict)
    product_profiles: dict[str, str] = Field(default_factory=dict)
    operators_excluded: list[str] = Field(default_factory=list)
    subscription_products: SubscriptionConfig = Field(default_factory=SubscriptionConfig)

    @field_validator("thresholds")
    @classmethod
    def _sort_profiles(cls, v: dict[str, list[Tier]]) -> dict[str, list[Tier]]:
        return {name: _check_tiers(tiers) for name, tiers in v.items()}

    @model_validator(mode="after")
    def _check_profiles(self) -> Rules:
        known = set(self.thresholds)
        used = {self.default_profile, *self.operator_profiles.values(), *self.product_profiles.values()}
        missing = used - known
        if missing:
            raise ValueError(f"unknown threshold profile(s): {sorted(missing)}")
        if not self.thresholds[self.default_profile]:
            raise ValueError("default profile has no tiers")
        return self


# --------------------------------------------------------------------------- loading


def _candidate_paths(path: Path | None) -> list[Path]:
    if path is not None:
        return [Path(path)]
    out: list[Path] = []
    config_dir = os.environ.get("CONFIG_DIR")
    if config_dir is None:
        try:
            from treinvertraging.config import get_settings

            config_dir = str(get_settings().CONFIG_DIR)
        except Exception:  # noqa: BLE001 - settings problems must not break rules loading
            config_dir = "/config"
    out.append(Path(config_dir) / "rules.yaml")
    out.append(Path(str(resources.files("treinvertraging") / "defaults" / "rules.yaml")))
    out.append(Path(__file__).resolve().parents[2] / "config.example" / "rules.yaml")
    return out


def find_rules_file(path: Path | None = None) -> Path:
    """Return the first existing rules file (see module docstring for the lookup order)."""
    for candidate in _candidate_paths(path):
        if candidate.is_file():
            return candidate
    raise RulesError(f"rules.yaml not found (looked in: {[str(p) for p in _candidate_paths(path)]})")


def parse_rules(text: str, source: str = "<string>") -> Rules:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesError(f"{source}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RulesError(f"{source}: expected a mapping at the top level")
    try:
        return Rules.model_validate(data)
    except ValidationError as exc:
        raise RulesError(f"{source}: invalid rules: {exc}") from exc


@lru_cache(maxsize=8)
def _load_cached(path_str: str, mtime_ns: int) -> Rules:  # noqa: ARG001 - mtime is the cache key
    return parse_rules(Path(path_str).read_text(encoding="utf-8"), path_str)


def load_rules(path: Path | None = None) -> Rules:
    """Load rules (cached per file path + mtime, so edits to rules.yaml are picked up)."""
    file = find_rules_file(path)
    try:
        mtime = file.stat().st_mtime_ns
    except OSError as exc:
        raise RulesError(f"cannot read {file}: {exc}") from exc
    return _load_cached(str(file), mtime)


# --------------------------------------------------------------------------- assessment


def add_months(d: date, months: int) -> date:
    """``d`` plus ``months`` calendar months, clamping the day to the target month's length."""
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _local_date(dt: datetime) -> date:
    return dt.astimezone(_TZ).date() if dt.tzinfo else dt.date()


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _tier_for(tiers: list[Tier], minutes: int) -> Tier | None:
    best: Tier | None = None
    for tier in tiers:  # sorted ascending
        if minutes >= tier.min:
            best = tier
    return best


def _profile_for(trip: TripDTO, rules: Rules) -> str:
    product = (trip.product or "").casefold()
    if product:
        for needle, profile in rules.product_profiles.items():
            if needle.casefold() in product:
                return profile
    operator = (trip.operator or "NS").strip().casefold() or "ns"
    for name, profile in rules.operator_profiles.items():
        if name.casefold() == operator:
            return profile
    return rules.default_profile


def _subscription_for(trip: TripDTO, rules: Rules) -> SubscriptionProduct | None:
    if not rules.subscription_products.enabled or not trip.product:
        return None
    product = trip.product.casefold()
    for sub in rules.subscription_products.products:
        if sub.match.casefold() in product:
            return sub
    return None


def _eur(value: Decimal) -> str:
    return f"EUR {_money(value)}"


def assess(
    trip: TripDTO,
    delay_min: int | None,
    cancelled: bool,
    ns_flag: bool | None,
    *,
    today: date | None = None,
    rules: Rules | None = None,
) -> DelayAssessment:
    """Assess one trip. Pure: all inputs are arguments (``today`` and ``rules`` are injectable).

    ``delay_min`` is the measured arrival delay at the final destination (None = no independent
    data). ``cancelled`` means the final leg was cancelled. ``ns_flag`` is NS's own delay marker.
    """
    rules = rules or load_rules()
    today = today or datetime.now(_TZ).date()
    deadline = add_months(_local_date(trip.checkin_at), rules.deadline_months)

    evidence: list[str] = []
    if delay_min is not None:
        delay_min = max(delay_min, 0)
        evidence.append(f"delay:+{delay_min}min")
    if cancelled:
        evidence.append("cancelled")
    if ns_flag:
        evidence.append("ns_flag")

    def result(eligible: bool, code: str, text: str, pct: int = 0, expected: Decimal = Decimal("0.00")):
        return DelayAssessment(
            trip_key=trip.key(),
            arrival_delay_min=delay_min,
            cancelled=cancelled,
            evidence=evidence,
            eligible=eligible,
            percent=pct if eligible else 0,
            expected_eur=_money(expected) if eligible else Decimal("0.00"),
            reason=f"{code}: {text}",
            deadline=deadline,
        )

    # 1. excluded operator
    operator = (trip.operator or "").strip().casefold()
    if operator and operator in {o.casefold() for o in rules.operators_excluded}:
        return result(False, "excluded_operator", f"operator {trip.operator} is not covered by these rules")

    # 2. check-out required
    if rules.require_checkout and trip.checkout_at is None:
        return result(False, "no_checkout", "trip has no check-out, so the journey cannot be verified")

    # 3. effective delay and tier
    sub = _subscription_for(trip, rules)
    tiers = sub.thresholds if sub else rules.thresholds[_profile_for(trip, rules)]
    effective = delay_min
    note = ""
    if cancelled:
        effective = max(delay_min or 0, rules.cancelled_counts_as_min)
        note = f" (cancelled, counted as at least {rules.cancelled_counts_as_min} min)"
    elif delay_min is None:
        if ns_flag and rules.trust_ns_flag_without_data and tiers:
            effective = tiers[0].min
            note = f" (no independent delay data; trusting NS flag, assuming lowest tier of {effective} min)"
        else:
            return result(False, "no_delay_data", "no delay data and no NS delay flag")
    elif ns_flag and delay_min < tiers[0].min:
        note = f" (NS flags a delay but measured delay is {delay_min} min)"

    tier = _tier_for(tiers, effective or 0)
    if tier is None:
        return result(
            False, "below_threshold", f"delay {effective or 0} min is below {tiers[0].min} min{note}"
        )

    # 4. deadline
    if today > deadline:
        return result(
            False,
            "deadline_passed",
            f"claim deadline was {deadline.isoformat()} (delay {effective} min would have given {tier.pct}%)",
        )

    # 5. amount
    if sub:
        base, base_label = sub.monthly_amount_eur, "monthly amount"
    else:
        base, base_label = trip.fare_eur, "fare"
    if base is None:
        return result(
            True,
            "eligible",
            f"delay {effective} min >= {tier.min} min: {tier.pct}% of unknown {base_label}; "
            f"expected amount unknown, set to EUR 0.00{note}",
            pct=tier.pct,
        )
    base = max(base, Decimal("0"))
    expected = _money(base * tier.pct / 100)
    if expected < rules.min_payout_eur:
        return result(
            False,
            "below_min_payout",
            f"{tier.pct}% of {base_label} {_eur(base)} = {_eur(expected)} is below the minimum payout "
            f"of {_eur(rules.min_payout_eur)}{note}",
        )
    return result(
        True,
        "eligible",
        f"delay {effective} min >= {tier.min} min: {tier.pct}% of {base_label} {_eur(base)} = "
        f"{_eur(expected)}{note}",
        pct=tier.pct,
        expected=expected,
    )
