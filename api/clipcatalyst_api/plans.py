"""Plan entitlements — the single source of truth, mirrored nowhere else.

Kept in its own tiny module so main.py (quota/entitlement reads) and
billing.py (webhook plan changes) can both import it without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Plan:
    clips_per_month: int | None  # None = unlimited (enterprise)
    max_height: int
    watermark_required: bool


PLANS: dict[str, Plan] = {
    "free": Plan(clips_per_month=3, max_height=1280, watermark_required=True),
    "starter": Plan(clips_per_month=30, max_height=1920, watermark_required=False),
    "pro": Plan(clips_per_month=100, max_height=3840, watermark_required=False),
    "enterprise": Plan(clips_per_month=None, max_height=3840, watermark_required=False),
}

# A subscription keeps its entitlements through the grace states Stripe
# reports while payment is still being retried.
_ENTITLED_STATUSES = frozenset({"active", "trialing", "past_due"})


def effective_plan(user: Mapping[str, object]) -> str:
    """The plan whose entitlements a user actually gets right now.

    ``plan`` counts only while ``plan_status`` says the subscription is live
    (active/trialing/past_due); anything else — canceled, unpaid, "" — is
    free. Plan names change ONLY via verified Stripe webhooks or the founder
    editing the DB, never from client input.
    """
    plan = str(user.get("plan") or "free")
    if (
        plan != "free"
        and user.get("plan_status") in _ENTITLED_STATUSES
        and plan in PLANS
    ):
        return plan
    return "free"
