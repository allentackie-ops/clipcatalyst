"""Stripe subscriptions behind a tiny gateway; verified webhooks change plans.

`CC_BILLING` picks the gateway: `stripe` talks to the real API, `fake` is the
deterministic offline stand-in (the FakeTranscriber pattern), `off` disables
billing entirely — the routes answer an honest 503. Two invariants hold in
every mode (ACCOUNTS.md):

- Plans change ONLY here, from webhook events whose `Stripe-Signature` was
  verified against ``CC_STRIPE_WEBHOOK_SECRET`` — never from client input.
- Secret keys and signing secrets are read from settings and passed straight
  to the stripe library; they are never logged and never echoed in errors.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Mapping, Protocol

import stripe

from . import db
from .plans import PLANS
from .settings import Settings

logger = logging.getLogger(__name__)

#: Plan names a checkout may target — everything in PLANS except free.
PAID_PLANS = tuple(name for name in PLANS if name != "free")

#: Webhook event types that mutate a user row. Anything else is acknowledged
#: (200) and ignored so Stripe never retries events we don't care about.
HANDLED_EVENTS = frozenset(
    {
        "checkout.session.completed",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "invoice.payment_failed",
    }
)


def price_id_for(settings: Settings, plan: str) -> str:
    """The configured Stripe price id for a paid plan name ('' when unset)."""
    return {
        "starter": settings.stripe_price_starter,
        "pro": settings.stripe_price_pro,
        "enterprise": settings.stripe_price_enterprise,
    }.get(plan, "")


def price_to_plan(settings: Settings) -> dict[str, str]:
    """price id → plan name, from the CC_STRIPE_PRICE_* envs (unset skipped)."""
    return {
        price_id_for(settings, plan): plan
        for plan in PAID_PLANS
        if price_id_for(settings, plan)
    }


class Gateway(Protocol):
    """What billing needs from Stripe: checkout + portal URL minting.

    Both calls create the user's Stripe customer on first use and reuse the
    stored ``stripe_customer_id`` afterwards.
    """

    def create_checkout(self, user: Mapping[str, object], plan: str) -> str: ...

    def create_portal(self, user: Mapping[str, object]) -> str: ...


def get_gateway(settings: Settings) -> Gateway | None:
    """The gateway for CC_BILLING, or None when billing is off."""
    if settings.billing == "off":
        return None
    if settings.billing == "stripe":
        return StripeGateway(settings)
    if settings.billing == "fake":
        return _fake_gateway
    raise ValueError(
        f"Unknown CC_BILLING {settings.billing!r} — use 'stripe', 'fake', or 'off'."
    )


class StripeGateway:
    """Real Stripe. The api key rides on each call — no global stripe state."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create_checkout(self, user: Mapping[str, object], plan: str) -> str:
        origin = self._settings.frontend_origin
        session = stripe.checkout.Session.create(
            api_key=self._settings.stripe_secret_key,
            mode="subscription",
            customer=self._ensure_customer(user),
            line_items=[{"price": price_id_for(self._settings, plan), "quantity": 1}],
            # Both breadcrumbs point the webhook back at the account row: the
            # checkout session carries client_reference_id, the subscription
            # it creates carries metadata.user_id for its lifecycle events.
            client_reference_id=str(user["id"]),
            subscription_data={"metadata": {"user_id": str(user["id"])}},
            success_url=f"{origin}/account?checkout=success",
            cancel_url=f"{origin}/account?checkout=cancelled",
        )
        return str(session.url)

    def create_portal(self, user: Mapping[str, object]) -> str:
        session = stripe.billing_portal.Session.create(
            api_key=self._settings.stripe_secret_key,
            customer=self._ensure_customer(user),
            return_url=f"{self._settings.frontend_origin}/account",
        )
        return str(session.url)

    def _ensure_customer(self, user: Mapping[str, object]) -> str:
        existing = str(user.get("stripe_customer_id") or "")
        if existing:
            return existing
        customer = stripe.Customer.create(
            api_key=self._settings.stripe_secret_key,
            email=str(user["email"]),
            metadata={"user_id": str(user["id"])},
        )
        db.update_user(str(user["id"]), stripe_customer_id=customer.id)
        return str(customer.id)


class FakeGateway:
    """Deterministic offline gateway for tests/dev (CC_BILLING=fake).

    Mints ``billing.invalid`` URLs, persists a deterministic customer id the
    way the real gateway would, and records every call for assertions. No
    network — the webhook route still signature-verifies in this mode, so the
    full plan-change pipeline is exercised for real.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def create_checkout(self, user: Mapping[str, object], plan: str) -> str:
        self._ensure_customer(user)
        self.calls.append(("checkout", str(user["id"]), plan))
        return f"https://billing.invalid/checkout/{plan}/{user['id']}"

    def create_portal(self, user: Mapping[str, object]) -> str:
        self._ensure_customer(user)
        self.calls.append(("portal", str(user["id"])))
        return f"https://billing.invalid/portal/{user['id']}"

    def _ensure_customer(self, user: Mapping[str, object]) -> str:
        existing = str(user.get("stripe_customer_id") or "")
        if existing:
            return existing
        customer_id = f"cus_fake_{user['id']}"
        db.update_user(str(user["id"]), stripe_customer_id=customer_id)
        return customer_id

    def reset(self) -> None:
        """Forget recorded calls (test isolation hook)."""
        self.calls.clear()


# One instance per process so recorded calls survive across requests; tests
# purge this module (or call reset()) for isolation, like auth's rate windows.
_fake_gateway = FakeGateway()


# --------------------------------------------------------------------------- #
# Webhook event application. The caller (main.py) has ALREADY verified the
# signature via stripe.Webhook.construct_event — nothing below runs on an
# unverified payload.
# --------------------------------------------------------------------------- #


def apply_event(event: Mapping[str, object], settings: Settings) -> bool:
    """Apply one verified Stripe event; returns True when the type is handled.

    Idempotent via the ``stripe_events`` table: a replayed event id is a
    no-op. The id is recorded AFTER the handler succeeds, so a crash mid-apply
    leaves the event unrecorded and Stripe's retry gets a clean second run
    (every handler is an absolute row update, safe to repeat).
    """
    event_id = str(event["id"])
    event_type = str(event["type"])
    if event_type not in HANDLED_EVENTS:
        return False
    if db.stripe_event_seen(event_id):
        logger.info("webhook %s (%s) already processed — no-op", event_id, event_type)
        return True
    obj = event["data"]["object"]  # type: ignore[index, call-overload]
    if event_type == "checkout.session.completed":
        _handle_checkout_completed(obj, settings)
    elif event_type == "customer.subscription.updated":
        _handle_subscription_updated(obj, settings)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(obj)
    elif event_type == "invoice.payment_failed":
        _handle_payment_failed(obj)
    db.record_stripe_event(event_id)
    return True


def _handle_checkout_completed(session: Mapping[str, object], settings: Settings) -> None:
    """A checkout finished: attach the customer, resolve subscription → plan."""
    user = _find_user(str(session.get("client_reference_id") or ""), session)
    if user is None:
        return
    customer_id = str(session.get("customer") or "")
    if customer_id:
        db.update_user(str(user["id"]), stripe_customer_id=customer_id)
    subscription = _resolve_subscription(session.get("subscription"), settings)
    if subscription is None:
        logger.warning(
            "checkout.session.completed for user %s carried no resolvable "
            "subscription — waiting for customer.subscription.updated",
            user["id"],
        )
        return
    _apply_subscription(user, subscription, settings)


def _handle_subscription_updated(
    subscription: Mapping[str, object], settings: Settings
) -> None:
    """Plan, status, and period end follow whatever Stripe now says."""
    user = _subscription_user(subscription)
    if user is not None:
        _apply_subscription(user, subscription, settings)


def _handle_subscription_deleted(subscription: Mapping[str, object]) -> None:
    """The subscription is gone — the account is free again."""
    user = _subscription_user(subscription)
    if user is not None:
        db.update_user(
            str(user["id"]), plan="free", plan_status="canceled", current_period_end=""
        )


def _handle_payment_failed(invoice: Mapping[str, object]) -> None:
    """A renewal charge failed: mark past_due (still entitled — grace period)."""
    user = _find_user("", invoice)
    if user is not None:
        db.update_user(str(user["id"]), plan_status="past_due")


def _apply_subscription(
    user: Mapping[str, object], subscription: Mapping[str, object], settings: Settings
) -> None:
    """Store a subscription's plan/status/period on the user row.

    The plan comes ONLY from the price→plan map (CC_STRIPE_PRICE_*); a price
    we don't recognize changes status/period but never grants a plan.
    """
    fields: dict[str, object] = {
        "plan_status": str(subscription.get("status") or ""),
        "current_period_end": _period_end_iso(subscription),
    }
    price_id = _subscription_price_id(subscription)
    plan = price_to_plan(settings).get(price_id, "")
    if plan:
        fields["plan"] = plan
    else:
        logger.warning(
            "subscription %s has price %r which maps to no CC_STRIPE_PRICE_* "
            "plan — leaving user %s on plan %r",
            subscription.get("id"),
            price_id,
            user["id"],
            user.get("plan"),
        )
    db.update_user(str(user["id"]), **fields)


def _resolve_subscription(
    ref: object, settings: Settings
) -> Mapping[str, object] | None:
    """A subscription object from an event's reference.

    Stripe sends checkout sessions with the subscription as a bare id string;
    in stripe mode we retrieve it. Fake-mode fixtures embed the object inline
    (there is deliberately no network to fetch from).
    """
    if isinstance(ref, Mapping):
        return ref
    if isinstance(ref, str) and ref and settings.billing == "stripe":
        subscription = stripe.Subscription.retrieve(
            ref, api_key=settings.stripe_secret_key
        )
        return subscription.to_dict()  # plain nested dicts, like the fixtures
    return None


def _subscription_price_id(subscription: Mapping[str, object]) -> str:
    """The first item's price id — one price per subscription in this product."""
    try:
        items = subscription["items"]["data"]  # type: ignore[index, call-overload]
        return str(items[0]["price"]["id"])
    except (KeyError, IndexError, TypeError):
        return ""


def _period_end_iso(subscription: Mapping[str, object]) -> str:
    """current_period_end as ISO-8601 UTC ('' when absent).

    Newer Stripe API versions report the period on the subscription items
    instead of the subscription — fall back to the first item.
    """
    ts = subscription.get("current_period_end")
    if not ts:
        try:
            items = subscription["items"]["data"]  # type: ignore[index, call-overload]
            ts = items[0].get("current_period_end")
        except (KeyError, IndexError, TypeError, AttributeError):
            ts = None
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(
        timespec="milliseconds"
    )


def _subscription_user(subscription: Mapping[str, object]) -> dict | None:
    """The user row a subscription event belongs to (metadata, then customer)."""
    metadata = subscription.get("metadata") or {}
    user_id = str(metadata.get("user_id") or "") if isinstance(metadata, Mapping) else ""
    return _find_user(user_id, subscription)


def _find_user(user_id: str, obj: Mapping[str, object]) -> dict | None:
    """Resolve an event's user by id, else by its ``customer``; warn on miss.

    A miss is logged and swallowed — returning an error would only make Stripe
    retry an event that can never match an account on this box.
    """
    if user_id:
        user = db.get_user_by_id(user_id)
        if user is not None:
            return user
    customer_id = str(obj.get("customer") or "")
    if customer_id:
        user = db.get_user_by_customer(customer_id)
        if user is not None:
            return user
    logger.warning(
        "webhook event matched no user (user_id=%r, customer=%r) — ignored",
        user_id,
        customer_id,
    )
    return None
