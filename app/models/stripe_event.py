"""Track processed Stripe webhook events to ensure idempotency."""
from datetime import datetime, timezone
from app.extensions import db


class StripeProcessedEvent(db.Model):
    """One row per Stripe event ID that has been successfully handled.

    Before processing any webhook event we INSERT this row. If the INSERT
    fails with a unique-constraint violation the event was already processed
    and can be safely ignored (Stripe's at-least-once delivery guarantee).
    """
    __tablename__ = "stripe_processed_events"

    id = db.Column(db.Integer, primary_key=True)
    stripe_event_id = db.Column(db.String(100), nullable=False, unique=True, index=True)
    event_type = db.Column(db.String(80), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<StripeProcessedEvent {self.stripe_event_id} [{self.event_type}]>"
