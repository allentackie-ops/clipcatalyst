"""Plan entitlements — the single source of truth, mirrored nowhere else.

Kept in its own tiny module so main.py (quota/entitlement reads) and
billing.py (webhook plan changes) can both import it without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping


@dataclass(frozen=True)
class Plan:
    clips_per_month: int | None  # None = unlimited (enterprise)
    max_height: int
    watermark_required: bool
    # The creator's own logo and caption colour on cloud renders (BRANDKIT.md).
    # Deliberately its own field rather than `not watermark_required`: the
    # pricing page sells them as two promises, and reading one off the other
    # would silently re-price whichever moves first.
    brand_kit: bool
    # How long a rendered clip's FILE is kept in the library; None = forever
    # (LIBRARY.md Part 2). Its own field for the same reason as brand_kit, and
    # a sharper one: retention is the promise that decides when we delete
    # somebody's video, so it is never derived from clips_per_month, from the
    # watermark, or from "is this a paid plan" — those move for pricing
    # reasons, and a pricing change must never quietly shorten how long we
    # keep what people already made. The clip's METADATA is permanent
    # regardless; this is the file's lifetime only.
    retention_days: int | None


PLANS: dict[str, Plan] = {
    "free": Plan(
        clips_per_month=3,
        max_height=1280,
        watermark_required=True,
        brand_kit=False,
        retention_days=7,
    ),
    "starter": Plan(
        clips_per_month=30,
        max_height=1920,
        watermark_required=False,
        brand_kit=True,
        retention_days=30,
    ),
    "pro": Plan(
        clips_per_month=100,
        max_height=3840,
        watermark_required=False,
        brand_kit=True,
        retention_days=90,
    ),
    "enterprise": Plan(
        clips_per_month=None,
        max_height=3840,
        watermark_required=False,
        brand_kit=True,
        retention_days=None,
    ),
}

# A subscription keeps its entitlements through the grace states Stripe
# reports while payment is still being retried.
_ENTITLED_STATUSES = frozenset({"active", "trialing", "past_due"})

#: How long past_due keeps its entitlements after the paid period ended.
#: Stripe's default dunning schedule retries a failed renewal for about two
#: weeks before giving up; matching it means a customer whose card expires is
#: never cut off mid-retry, while an account that simply stopped paying stops
#: being entitled instead of staying "past_due" on a paid plan forever.
PAST_DUE_GRACE_DAYS = 14


def _within_past_due_grace(user: Mapping[str, object]) -> bool:
    """Is a past_due subscription still inside its dunning window?

    Bounded by ``current_period_end`` (the last period the customer actually
    paid for) plus PAST_DUE_GRACE_DAYS. An empty or unparseable period end
    means Stripe never told us when the paid period ended — the only rows
    like that are hand-edited ones, and inventing a cutoff for them would
    revoke a founder's own grant, so they stay entitled.
    """
    raw = str(user.get("current_period_end") or "")
    if not raw:
        return True
    try:
        period_end = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) <= period_end + timedelta(days=PAST_DUE_GRACE_DAYS)


def effective_plan(user: Mapping[str, object]) -> str:
    """The plan whose entitlements a user actually gets right now.

    ``plan`` counts only while ``plan_status`` says the subscription is live
    (active/trialing/past_due); anything else — canceled, unpaid, "" — is
    free. ``past_due`` is additionally bounded: it is a dunning grace state,
    not an indefinite one, so it expires PAST_DUE_GRACE_DAYS after the paid
    period ended (an unpaid account cannot keep rendering forever just because
    Stripe's last word on it was "retrying"). Plan names change ONLY via
    verified Stripe webhooks or the founder editing the DB, never from client
    input.
    """
    plan = str(user.get("plan") or "free")
    status = user.get("plan_status")
    if (
        plan != "free"
        and status in _ENTITLED_STATUSES
        and plan in PLANS
        and (status != "past_due" or _within_past_due_grace(user))
    ):
        return plan
    return "free"
