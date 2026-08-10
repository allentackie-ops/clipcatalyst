"""Billing tests: gateway URLs, REAL webhook signatures, idempotency, plans.

Mirrors the env/import dance of ``test_auth.py`` — all CC_* vars are set BEFORE
any app module is imported, ``get_settings`` is lru_cached so its cache is
cleared, and the settings-snapshotting modules (queue_app / worker / main, plus
auth for its rate-limit windows and billing for the FakeGateway call log) are
purged for a clean re-import. The whole os.environ is snapshotted and restored
around each client.

api.stripe.com is unreachable from this sandbox, and nothing here needs it: the
webhook signature is offline crypto (this module builds the ``Stripe-Signature``
header by hand exactly as Stripe does, and the stripe library verifies it for
real), and every event fixture embeds its subscription object inline. An autouse
fixture turns any live Stripe API call into a loud test failure so a regression
that reaches for the network cannot pass quietly.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
import stripe

FOUNDER_TOKEN = "s3cr3t-founder-token"
WEBHOOK_SECRET = "whsec_test_c0ffee_1234567890abcdef"
WRONG_SECRET = "whsec_test_not_the_configured_one"
PRICE_STARTER = "price_test_starter"
PRICE_PRO = "price_test_pro"
PRICE_ENTERPRISE = "price_test_enterprise"

# A fixed subscription period end and the ISO-8601 UTC string billing.py must
# store for it (asserted as a literal, not recomputed with the same helper).
PERIOD_END_TS = 1_800_000_000
PERIOD_END_ISO = "2027-01-15T08:00:00.000+00:00"

_SNAPSHOT_MODULES = (
    "clipcatalyst_api.main",
    "clipcatalyst_api.worker",
    "clipcatalyst_api.queue_app",
)


def _purge() -> None:
    from clipcatalyst_api.settings import get_settings

    get_settings.cache_clear()
    for name in _SNAPSHOT_MODULES:
        sys.modules.pop(name, None)


def _set_env(**values: str | None) -> None:
    """Flip CC_* env mid-test; every billing route reads get_settings() live."""
    from clipcatalyst_api.settings import get_settings

    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_live_stripe() -> Iterator[None]:
    """Any call to the live Stripe API is a test bug — make it fail loudly."""
    saved = (
        stripe.Subscription.retrieve,
        stripe.Customer.create,
        stripe.checkout.Session.create,
        stripe.billing_portal.Session.create,
    )

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("a billing test tried to call the live Stripe API")

    stripe.Subscription.retrieve = blocked  # type: ignore[method-assign]
    stripe.Customer.create = blocked  # type: ignore[method-assign]
    stripe.checkout.Session.create = blocked  # type: ignore[method-assign]
    stripe.billing_portal.Session.create = blocked  # type: ignore[method-assign]
    try:
        yield
    finally:
        (
            stripe.Subscription.retrieve,  # type: ignore[method-assign]
            stripe.Customer.create,  # type: ignore[method-assign]
            stripe.checkout.Session.create,  # type: ignore[method-assign]
            stripe.billing_portal.Session.create,  # type: ignore[method-assign]
        ) = saved


@pytest.fixture()
def sandbox(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    """A fresh TestClient with its own data dir; CC_BILLING starts at `fake`."""
    saved_env = dict(os.environ)
    data_dir = tmp_path_factory.mktemp("billingdata")

    os.environ.update(
        {
            "CC_QUEUE": "eager",
            "CC_TRANSCRIBER": "fake",
            "CC_STORAGE": "local",
            "CC_DATA_DIR": str(data_dir),
            "CC_DB_PATH": str(data_dir / "jobs.sqlite3"),
            "CC_PUBLIC_BASE_URL": "",
            "CC_BILLING": "fake",
            # Billing being on REQUIRES a founder token (Settings.validate:
            # without one the job routes stay open to anonymous callers, who
            # skip every plan entitlement) — a billing-enabled app cannot even
            # be constructed without it. Nothing in this module calls a
            # founder-gated route; sessions carry all of its auth.
            "CC_API_TOKEN": FOUNDER_TOKEN,
            "CC_STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
            "CC_STRIPE_PRICE_STARTER": PRICE_STARTER,
            "CC_STRIPE_PRICE_PRO": PRICE_PRO,
            "CC_STRIPE_PRICE_ENTERPRISE": PRICE_ENTERPRISE,
            "CC_FRONTEND_ORIGIN": "https://clips.example",
        }
    )
    os.environ.pop("CC_STRIPE_SECRET_KEY", None)
    _purge()

    from fastapi.testclient import TestClient

    from clipcatalyst_api import auth, billing
    from clipcatalyst_api.main import app

    # Two module globals outlive the purge (a submodule stays reachable as an
    # attribute of the package even after sys.modules eviction): auth's
    # in-process rate-limit windows and the FakeGateway's call log. Clear both
    # on the way in AND on the way out, so this module neither inherits nor
    # leaves per-process state for whatever runs next.
    def _reset_process_state() -> None:
        auth.reset_rate_limits()
        billing._fake_gateway.reset()

    _reset_process_state()
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, data_dir=data_dir)
    finally:
        _reset_process_state()
        os.environ.clear()
        os.environ.update(saved_env)
        _purge()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client, email: str, password: str = "correct-horse-battery") -> dict:
    resp = client.post("/v1/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Webhook payload plumbing: signatures built by hand, verified by the real lib.
# --------------------------------------------------------------------------- #


def _sign(raw: bytes, *, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> dict:
    """The `Stripe-Signature` header for a raw body.

    `t={ts},v1=HMAC_SHA256(secret, b"{ts}." + raw)` — byte for byte what Stripe
    sends, and what ``stripe.Webhook.construct_event`` verifies offline.
    """
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(
        secret.encode("utf-8"), f"{ts}.".encode("utf-8") + raw, hashlib.sha256
    ).hexdigest()
    return {"Stripe-Signature": f"t={ts},v1={digest}"}


def _raw(event: dict) -> bytes:
    """The exact bytes a webhook delivery carries (what gets signed)."""
    return json.dumps(event).encode("utf-8")


def _send(client, raw: bytes, headers: dict):  # noqa: ANN202 - httpx Response
    return client.post("/v1/billing/webhook", content=raw, headers=headers)


def _deliver(client, event: dict, **sign_kwargs: object):  # noqa: ANN202
    """Sign an event correctly and POST it, the happy path in one call."""
    raw = _raw(event)
    return _send(client, raw, _sign(raw, **sign_kwargs))  # type: ignore[arg-type]


def _subscription(
    price_id: str,
    *,
    user_id: str = "",
    status: str = "active",
    customer: str = "cus_live_1",
    sub_id: str = "sub_test_1",
    period_end: int | None = PERIOD_END_TS,
) -> dict:
    sub: dict = {
        "id": sub_id,
        "object": "subscription",
        "status": status,
        "customer": customer,
        "metadata": {"user_id": user_id} if user_id else {},
        "items": {"data": [{"id": "si_1", "price": {"id": price_id}}]},
    }
    if period_end is not None:
        sub["current_period_end"] = period_end
    return sub


def _event(event_type: str, obj: dict, *, event_id: str) -> dict:
    return {
        "id": event_id,
        "object": "event",
        "api_version": "2024-06-20",
        "created": int(time.time()),
        "type": event_type,
        "data": {"object": obj},
    }


def _checkout_completed(
    user_id: str, price_id: str, *, event_id: str, customer: str = "cus_live_1", status: str = "active"
) -> dict:
    """A completed checkout carrying its subscription inline.

    Stripe sends the subscription as a bare id and the real gateway retrieves
    it; the offline fixture embeds the object, which billing resolves without
    a network round-trip (``_resolve_subscription``).
    """
    return _event(
        "checkout.session.completed",
        {
            "id": "cs_test_1",
            "object": "checkout.session",
            "client_reference_id": user_id,
            "customer": customer,
            "subscription": _subscription(
                price_id, user_id=user_id, status=status, customer=customer
            ),
        },
        event_id=event_id,
    )


# --------------------------------------------------------------------------- #
# 1. The fake gateway: deterministic URLs and customer-id creation/reuse.
# --------------------------------------------------------------------------- #


def test_fake_gateway_mints_checkout_urls_and_creates_a_customer(
    sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import billing, db

    client = sandbox.client
    body = _register(client, "checkout@example.com")
    token, user_id = body["token"], body["user"]["id"]

    # No billing profile yet, so the portal has nothing to open.
    portal = client.post("/v1/billing/portal", headers=_bearer(token))
    assert portal.status_code == 400
    assert db.get_user_by_id(user_id)["stripe_customer_id"] == ""

    resp = client.post(
        "/v1/billing/checkout", json={"plan": "starter"}, headers=_bearer(token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": f"https://billing.invalid/checkout/starter/{user_id}"}
    # Checkout created the Stripe customer, exactly as the real gateway would.
    customer_id = db.get_user_by_id(user_id)["stripe_customer_id"]
    assert customer_id == f"cus_fake_{user_id}"

    # A second checkout REUSES that customer instead of minting another.
    resp = client.post(
        "/v1/billing/checkout", json={"plan": "Pro"}, headers=_bearer(token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": f"https://billing.invalid/checkout/pro/{user_id}"}
    assert db.get_user_by_id(user_id)["stripe_customer_id"] == customer_id

    # And the portal opens now that a customer exists.
    portal = client.post("/v1/billing/portal", headers=_bearer(token))
    assert portal.status_code == 200, portal.text
    assert portal.json() == {"url": f"https://billing.invalid/portal/{user_id}"}
    assert db.get_user_by_id(user_id)["stripe_customer_id"] == customer_id

    assert billing._fake_gateway.calls == [
        ("checkout", user_id, "starter"),
        ("checkout", user_id, "pro"),
        ("portal", user_id),
    ]


def test_billing_routes_require_a_session(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    assert client.post("/v1/billing/checkout", json={"plan": "starter"}).status_code == 401
    assert client.post("/v1/billing/portal").status_code == 401
    assert (
        client.post(
            "/v1/billing/checkout",
            json={"plan": "starter"},
            headers=_bearer("cc_sess_" + "a" * 43),
        ).status_code
        == 401
    )


def test_checkout_rejects_unknown_and_free_plans(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    token = _register(client, "plans@example.com")["token"]
    for plan in ("free", "FREE", " free ", "gold", "enterprise-plus"):
        resp = client.post(
            "/v1/billing/checkout", json={"plan": plan}, headers=_bearer(token)
        )
        assert resp.status_code == 400, f"{plan!r}: {resp.text}"
        assert "starter" in resp.json()["detail"]
    # An empty plan is a schema violation, not a plan choice.
    assert (
        client.post(
            "/v1/billing/checkout", json={"plan": ""}, headers=_bearer(token)
        ).status_code
        == 422
    )


def test_billing_off_answers_503_everywhere(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "off@example.com")
    token, user_id = body["token"], body["user"]["id"]
    before = db.get_user_by_id(user_id)
    _set_env(CC_BILLING="off")

    checkout = client.post(
        "/v1/billing/checkout", json={"plan": "starter"}, headers=_bearer(token)
    )
    assert checkout.status_code == 503
    assert "Billing isn't enabled" in checkout.json()["detail"]
    assert client.post("/v1/billing/portal", headers=_bearer(token)).status_code == 503

    # The webhook is off too — and a PERFECTLY signed event still changes nothing.
    event = _checkout_completed(user_id, PRICE_STARTER, event_id="evt_while_off")
    resp = _deliver(client, event)
    assert resp.status_code == 503
    assert "Billing isn't enabled" in resp.json()["detail"]
    assert db.get_user_by_id(user_id) == before
    assert not db.stripe_event_seen("evt_while_off")


def test_stripe_mode_reports_missing_configuration(sandbox: SimpleNamespace) -> None:
    """Deployment gaps are honest 503s, never a half-working checkout."""
    client = sandbox.client
    token = _register(client, "misconfigured@example.com")["token"]

    _set_env(CC_BILLING="stripe", CC_STRIPE_SECRET_KEY=None)
    resp = client.post(
        "/v1/billing/checkout", json={"plan": "starter"}, headers=_bearer(token)
    )
    assert resp.status_code == 503
    assert "CC_STRIPE_SECRET_KEY" in resp.json()["detail"]

    _set_env(CC_STRIPE_SECRET_KEY="sk_test_never_used", CC_STRIPE_PRICE_PRO=None)
    resp = client.post("/v1/billing/checkout", json={"plan": "pro"}, headers=_bearer(token))
    assert resp.status_code == 503
    assert "CC_STRIPE_PRICE_PRO" in resp.json()["detail"]

    _set_env(CC_STRIPE_WEBHOOK_SECRET=None)
    event = _checkout_completed("nobody", PRICE_STARTER, event_id="evt_no_secret")
    resp = _send(client, _raw(event), {"Stripe-Signature": "t=1,v1=whatever"})
    assert resp.status_code == 503
    assert "CC_STRIPE_WEBHOOK_SECRET" in resp.json()["detail"]


def test_get_gateway_follows_cc_billing(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import billing
    from clipcatalyst_api.settings import get_settings

    _set_env(CC_BILLING="off")
    assert billing.get_gateway(get_settings()) is None
    _set_env(CC_BILLING="fake")
    assert isinstance(billing.get_gateway(get_settings()), billing.FakeGateway)
    _set_env(CC_BILLING="stripe")
    assert isinstance(billing.get_gateway(get_settings()), billing.StripeGateway)
    _set_env(CC_BILLING="maybe")
    with pytest.raises(ValueError):
        billing.get_gateway(get_settings())


def test_price_to_plan_maps_only_configured_prices(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import billing
    from clipcatalyst_api.settings import get_settings

    assert billing.price_to_plan(get_settings()) == {
        PRICE_STARTER: "starter",
        PRICE_PRO: "pro",
        PRICE_ENTERPRISE: "enterprise",
    }
    assert billing.price_id_for(get_settings(), "free") == ""
    assert billing.price_id_for(get_settings(), "nonsense") == ""

    # Unset prices drop out of the map — they must never become a "" → plan key.
    _set_env(CC_STRIPE_PRICE_PRO=None, CC_STRIPE_PRICE_ENTERPRISE=None)
    assert billing.price_to_plan(get_settings()) == {PRICE_STARTER: "starter"}


# --------------------------------------------------------------------------- #
# 2. Webhook signature verification — the security boundary.
# --------------------------------------------------------------------------- #


def test_webhook_rejects_every_unsigned_or_forged_delivery(
    sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "forged@example.com")
    user_id = body["user"]["id"]
    before = db.get_user_by_id(user_id)

    event = _checkout_completed(user_id, PRICE_PRO, event_id="evt_forged")
    raw = _raw(event)

    forgeries = [
        ({}, "no Stripe-Signature header at all"),
        ({"Stripe-Signature": ""}, "an empty Stripe-Signature header"),
        ({"Stripe-Signature": "t=1,v1=deadbeef"}, "a made-up signature"),
        ({"Stripe-Signature": "not-a-signature-header"}, "a malformed header"),
        (_sign(raw, secret=WRONG_SECRET), "a signature from the wrong secret"),
        (
            _sign(raw, timestamp=int(time.time()) - 3600),
            "a valid signature replayed outside the tolerance window",
        ),
    ]
    for headers, why in forgeries:
        resp = _send(client, raw, headers)
        assert resp.status_code == 400, f"{why}: {resp.status_code} {resp.text}"
        assert db.get_user_by_id(user_id) == before, why
        assert not db.stripe_event_seen("evt_forged"), why

    # Tampered body: signed as pro, delivered as enterprise. The HMAC covers the
    # RAW bytes, so swapping the payload under a good signature is caught.
    tampered = _raw(_checkout_completed(user_id, PRICE_ENTERPRISE, event_id="evt_forged"))
    assert tampered != raw
    resp = _send(client, tampered, _sign(raw))
    assert resp.status_code == 400
    assert db.get_user_by_id(user_id) == before

    # Well-signed but not JSON at all → 400, not a 500.
    garbage = b"{not json"
    assert _send(client, garbage, _sign(garbage)).status_code == 400

    # Positive control: the SAME event, correctly signed, DOES apply — so every
    # 400 above is the signature check and not some unrelated refusal.
    resp = _send(client, raw, _sign(raw))
    assert resp.status_code == 200, resp.text
    assert db.get_user_by_id(user_id)["plan"] == "pro"


def test_stripe_mode_never_applies_an_unverified_event(sandbox: SimpleNamespace) -> None:
    """The non-negotiable, asserted in the mode that actually ships.

    ``fake`` and ``stripe`` share one verification path, but the promise in
    ACCOUNTS.md is about production: with CC_BILLING=stripe an unsigned POST
    must be a 400 that leaves the user row byte-identical.
    """
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "prod@example.com")
    user_id = body["user"]["id"]
    _set_env(CC_BILLING="stripe", CC_STRIPE_SECRET_KEY="sk_test_never_used")
    before = db.get_user_by_id(user_id)

    event = _event(
        "customer.subscription.updated",
        _subscription(PRICE_PRO, user_id=user_id),
        event_id="evt_prod_1",
    )
    raw = _raw(event)

    assert _send(client, raw, {}).status_code == 400
    assert _send(client, raw, _sign(raw, secret=WRONG_SECRET)).status_code == 400
    assert db.get_user_by_id(user_id) == before
    assert not db.stripe_event_seen("evt_prod_1")

    # Correctly signed: the plan moves. Nothing touched the network to do it
    # (no_live_stripe would have raised) — the fixture carries the object.
    assert _send(client, raw, _sign(raw)).status_code == 200
    after = db.get_user_by_id(user_id)
    assert after["plan"] == "pro"
    assert after["plan_status"] == "active"
    assert db.stripe_event_seen("evt_prod_1")


def test_no_code_path_reaches_apply_event_without_verification() -> None:
    """Structural counterpart: apply_event has exactly one caller, downstream
    of ``stripe.Webhook.construct_event`` in the webhook route."""
    import clipcatalyst_api
    from clipcatalyst_api import main

    package_dir = Path(inspect.getfile(clipcatalyst_api)).parent
    callers: list[str] = []
    for path in sorted(package_dir.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "apply_event(" in code and "def apply_event(" not in code:
                callers.append(f"{path.relative_to(package_dir)}:{lineno}")
    assert len(callers) == 1, f"apply_event must have exactly one caller: {callers}"
    assert callers[0].startswith("main.py:")

    route = inspect.getsource(main._build_router)
    assert route.count("billing.apply_event(") == 1
    assert route.index("stripe.Webhook.construct_event(") < route.index(
        "billing.apply_event("
    )
    # The 400 paths are raises, so nothing after them can fall through to apply.
    assert "except stripe.SignatureVerificationError:" in route


# --------------------------------------------------------------------------- #
# 3. Handled events: each one mutates the user row correctly.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("price_id", "plan", "limit", "max_height"),
    [
        (PRICE_STARTER, "starter", 30, 1920),
        (PRICE_PRO, "pro", 100, 3840),
        (PRICE_ENTERPRISE, "enterprise", None, 3840),
    ],
)
def test_checkout_completed_maps_each_price_to_its_plan(
    sandbox: SimpleNamespace, price_id: str, plan: str, limit: int | None, max_height: int
) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, f"{plan}@example.com")
    token, user_id = body["token"], body["user"]["id"]

    event = _checkout_completed(
        user_id, price_id, event_id=f"evt_checkout_{plan}", customer="cus_live_42"
    )
    resp = _deliver(client, event)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"received": True}

    user = db.get_user_by_id(user_id)
    assert user["plan"] == plan
    assert user["plan_status"] == "active"
    assert user["stripe_customer_id"] == "cus_live_42"
    assert user["current_period_end"] == PERIOD_END_ISO

    # /v1/me — the account page's single source — agrees immediately.
    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["plan"] == plan
    assert me["quota"]["limit"] == limit
    assert me["entitlements"] == {
        "max_height": max_height,
        "watermark_required": False,
        "clips_per_month": limit,
    }


def test_subscription_updated_follows_stripe(sandbox: SimpleNamespace) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "updated@example.com")
    token, user_id = body["token"], body["user"]["id"]

    # Trialing on pro: entitled while the trial runs.
    assert (
        _deliver(
            client,
            _event(
                "customer.subscription.updated",
                _subscription(PRICE_PRO, user_id=user_id, status="trialing"),
                event_id="evt_upd_1",
            ),
        ).status_code
        == 200
    )
    user = db.get_user_by_id(user_id)
    assert (user["plan"], user["plan_status"]) == ("pro", "trialing")
    assert client.get("/v1/me", headers=_bearer(token)).json()["quota"]["limit"] == 100

    # A downgrade to starter is just another update.
    assert (
        _deliver(
            client,
            _event(
                "customer.subscription.updated",
                _subscription(PRICE_STARTER, user_id=user_id, status="active"),
                event_id="evt_upd_2",
            ),
        ).status_code
        == 200
    )
    assert db.get_user_by_id(user_id)["plan"] == "starter"

    # past_due is a grace state: the plan and its entitlements survive it.
    assert (
        _deliver(
            client,
            _event(
                "customer.subscription.updated",
                _subscription(PRICE_STARTER, user_id=user_id, status="past_due"),
                event_id="evt_upd_3",
            ),
        ).status_code
        == 200
    )
    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["plan_status"] == "past_due"
    assert me["quota"]["limit"] == 30
    assert me["entitlements"]["watermark_required"] is False

    # unpaid is not: entitlements collapse to free while the name is kept.
    assert (
        _deliver(
            client,
            _event(
                "customer.subscription.updated",
                _subscription(PRICE_STARTER, user_id=user_id, status="unpaid"),
                event_id="evt_upd_4",
            ),
        ).status_code
        == 200
    )
    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["plan"] == "starter"
    assert me["quota"]["limit"] == 3
    assert me["entitlements"]["watermark_required"] is True


def test_subscription_updated_resolves_the_user_by_customer_id(
    sandbox: SimpleNamespace,
) -> None:
    """Stripe's own lifecycle events carry no metadata we control — the stored
    customer id has to be enough to find the account."""
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "bycustomer@example.com")
    token, user_id = body["token"], body["user"]["id"]
    client.post("/v1/billing/checkout", json={"plan": "starter"}, headers=_bearer(token))
    customer_id = db.get_user_by_id(user_id)["stripe_customer_id"]
    assert customer_id

    event = _event(
        "customer.subscription.updated",
        _subscription(PRICE_PRO, customer=customer_id),  # no metadata.user_id
        event_id="evt_by_customer",
    )
    assert _deliver(client, event).status_code == 200
    assert db.get_user_by_id(user_id)["plan"] == "pro"


def test_subscription_deleted_returns_the_account_to_free(
    sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "deleted@example.com")
    token, user_id = body["token"], body["user"]["id"]
    assert (
        _deliver(
            client, _checkout_completed(user_id, PRICE_PRO, event_id="evt_del_up")
        ).status_code
        == 200
    )
    assert db.get_user_by_id(user_id)["plan"] == "pro"

    assert (
        _deliver(
            client,
            _event(
                "customer.subscription.deleted",
                _subscription(PRICE_PRO, user_id=user_id, status="canceled"),
                event_id="evt_del_1",
            ),
        ).status_code
        == 200
    )
    user = db.get_user_by_id(user_id)
    assert user["plan"] == "free"
    assert user["plan_status"] == "canceled"
    assert user["current_period_end"] == ""
    # The customer id survives so "Manage billing" still works after cancelling.
    assert user["stripe_customer_id"] == "cus_live_1"

    me = client.get("/v1/me", headers=_bearer(token)).json()
    assert me["quota"]["limit"] == 3
    assert me["entitlements"] == {
        "max_height": 1280,
        "watermark_required": True,
        "clips_per_month": 3,
    }


def test_payment_failed_marks_past_due_without_dropping_the_plan(
    sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "dunning@example.com")
    user_id = body["user"]["id"]
    assert (
        _deliver(
            client,
            _checkout_completed(
                user_id, PRICE_STARTER, event_id="evt_pay_up", customer="cus_live_7"
            ),
        ).status_code
        == 200
    )

    invoice = {
        "id": "in_test_1",
        "object": "invoice",
        "customer": "cus_live_7",  # invoices carry no user_id metadata
        "attempt_count": 2,
    }
    assert (
        _deliver(
            client, _event("invoice.payment_failed", invoice, event_id="evt_pay_1")
        ).status_code
        == 200
    )
    user = db.get_user_by_id(user_id)
    assert user["plan"] == "starter"  # dunning grace: still entitled
    assert user["plan_status"] == "past_due"
    assert user["current_period_end"] == PERIOD_END_ISO  # untouched


def test_unrecognized_price_never_grants_a_plan(sandbox: SimpleNamespace) -> None:
    """A price this server doesn't know maps to no plan — status/period follow
    Stripe, but entitlements must not be invented from an unknown product."""
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "mystery@example.com")
    user_id = body["user"]["id"]

    event = _event(
        "customer.subscription.updated",
        _subscription("price_from_another_account", user_id=user_id),
        event_id="evt_mystery",
    )
    assert _deliver(client, event).status_code == 200
    user = db.get_user_by_id(user_id)
    assert user["plan"] == "free"
    assert user["plan_status"] == "active"
    assert user["current_period_end"] == PERIOD_END_ISO


def test_unknown_event_types_are_acknowledged_and_ignored(
    sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "noise@example.com")
    user_id = body["user"]["id"]
    before = db.get_user_by_id(user_id)

    for event_type in ("customer.created", "payment_intent.succeeded", "ping"):
        event = _event(
            event_type,
            {"id": "obj_1", "customer": "cus_live_1", "client_reference_id": user_id},
            event_id=f"evt_{event_type}",
        )
        resp = _deliver(client, event)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"received": True}
        assert db.get_user_by_id(user_id) == before
        # Ignored events are not recorded — the table is for replay defence of
        # events we actually applied.
        assert not db.stripe_event_seen(f"evt_{event_type}")


def test_events_matching_no_account_are_swallowed(sandbox: SimpleNamespace) -> None:
    """A webhook for an account that doesn't live on this box is a 200, not a
    500 — returning an error would only make Stripe retry it forever."""
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "bystander@example.com")
    user_id = body["user"]["id"]
    before = db.get_user_by_id(user_id)

    for event in (
        _checkout_completed(
            "nosuchuser", PRICE_PRO, event_id="evt_orphan_1", customer="cus_unknown"
        ),
        _event(
            "customer.subscription.deleted",
            _subscription(PRICE_PRO, user_id="nosuchuser", customer="cus_unknown"),
            event_id="evt_orphan_2",
        ),
        _event(
            "invoice.payment_failed",
            {"id": "in_x", "object": "invoice", "customer": "cus_unknown"},
            event_id="evt_orphan_3",
        ),
    ):
        assert _deliver(client, event).status_code == 200
    assert db.get_user_by_id(user_id) == before


def test_checkout_without_a_resolvable_subscription_only_stores_the_customer(
    sandbox: SimpleNamespace,
) -> None:
    """Checkout can complete before the subscription object is available; the
    customer id is banked and the plan waits for customer.subscription.updated."""
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "pending@example.com")
    user_id = body["user"]["id"]

    event = _event(
        "checkout.session.completed",
        {
            "id": "cs_test_2",
            "object": "checkout.session",
            "client_reference_id": user_id,
            "customer": "cus_live_9",
            "subscription": None,
        },
        event_id="evt_pending",
    )
    assert _deliver(client, event).status_code == 200
    user = db.get_user_by_id(user_id)
    assert user["stripe_customer_id"] == "cus_live_9"
    assert user["plan"] == "free"
    assert user["plan_status"] == ""


# --------------------------------------------------------------------------- #
# 4. Idempotency: a replayed event id is a 200 no-op, never a second apply.
# --------------------------------------------------------------------------- #


def test_replayed_event_id_is_a_noop_and_applies_nothing(
    sandbox: SimpleNamespace,
) -> None:
    from clipcatalyst_api import db

    client = sandbox.client
    body = _register(client, "replay@example.com")
    user_id = body["user"]["id"]

    event = _checkout_completed(user_id, PRICE_STARTER, event_id="evt_replay")
    assert _deliver(client, event).status_code == 200
    applied = db.get_user_by_id(user_id)
    assert applied["plan"] == "starter"
    assert db.stripe_event_seen("evt_replay")

    # 1. Stripe's own at-least-once retry: byte-identical delivery.
    for _ in range(3):
        resp = _deliver(client, event)
        assert resp.status_code == 200, resp.text
        assert db.get_user_by_id(user_id) == applied

    # 2. The teeth of the check: the SAME event id carrying a DIFFERENT payload
    #    must change nothing. Plan writes are absolute, so a re-apply would be
    #    invisible if the replayed body matched — this one would upgrade the
    #    account to enterprise if the id were not honoured.
    hostile = _checkout_completed(
        user_id, PRICE_ENTERPRISE, event_id="evt_replay", customer="cus_attacker"
    )
    resp = _deliver(client, hostile)
    assert resp.status_code == 200, resp.text
    assert db.get_user_by_id(user_id) == applied

    # 3. A genuinely new id with that payload does apply — proving the no-op
    #    above came from the replay guard and not from a rejected payload.
    fresh = _checkout_completed(
        user_id, PRICE_ENTERPRISE, event_id="evt_replay_2", customer="cus_attacker"
    )
    assert _deliver(client, fresh).status_code == 200
    assert db.get_user_by_id(user_id)["plan"] == "enterprise"
