"""Small input-only facts for focused Stage B-II tests; no canonical vendoring."""

from copy import deepcopy
from datetime import UTC, datetime

from app.services.company_os_synthetic_adapters import SyntheticWorld

AT = "2026-09-25T00:00:00+00:00"
SENDER = {
    "sender_id": "sender", "from_address": "hello@example.test",
    "reply_to": "stop@example.test", "sending_domain": "example.test",
    "postal_address": "100 Synthetic Way", "seller_name": "Synthetic Seller",
    "dispatch_method": "automated", "captured_at": AT,
}
TEMPLATE = {
    "template_id": "approved", "subject": "Service follow-up",
    "body_template": "Hello {name}. Advertisement. Reply to stop@example.test to opt out of all marketing. 100 Synthetic Way",
    "placeholders": ["name"], "ad_disclosure": "Advertisement.",
    "subject_accuracy_verified": {"by": "reviewer", "at": AT},
    "opt_out_route": {"type": "reply", "target": "stop@example.test", "valid_days": 30,
                      "covers_all_marketing": True, "fee_required": False,
                      "extra_data_required": False, "verified_at": AT},
    "approved_at": AT, "revoked_at": None,
}
MESSAGE = {"template_id": "approved", "fills": {"name": "Pat"}}


def eligible_world(run_id="s2-test", recipient="p-1"):
    world = SyntheticWorld(run_id, datetime(2026, 9, 25, tzinfo=UTC))
    world.set_contact(recipient, {
        "recipient_id": recipient, "address": f"{recipient}@example.test",
        "consent_verified": True, "suppressed": False, "captured_at": AT,
        "recipient_location": {"country": "US", "state": "NY"},
        "recipient_time_zone": "America/New_York",
    })
    world.consents[recipient] = {
        "recipient_id": recipient, "contact_email": f"{recipient}@example.test", "method": "automated",
        "seller": "Synthetic Seller", "channel": "email", "purpose": "commercial",
        "captured_at": AT, "revoked_at": None,
    }
    world.suppression_snapshots[recipient] = {"checked_at": AT, "version": "1"}
    world.templates["approved"] = deepcopy(TEMPLATE)
    world.compliance_policy = {"version": "0.1.0", "sha256": "a" * 64}
    return world
