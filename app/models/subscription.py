import enum
from datetime import datetime, timezone
from app.extensions import db


class Plan(str, enum.Enum):
    FREE = "free"
    PRO = "pro"


# Plan limits
PLAN_LIMITS = {
    Plan.FREE: {"max_bots": 1, "max_pairs_per_bot": 1},
    Plan.PRO: {"max_bots": 999, "max_pairs_per_bot": 999},
}


class Subscription(db.Model):
    __tablename__ = "subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)

    plan = db.Column(db.Enum(Plan), default=Plan.FREE, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    stripe_customer_id = db.Column(db.String(100), nullable=True)
    stripe_subscription_id = db.Column(db.String(100), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="subscription")

    @property
    def is_active_pro(self) -> bool:
        if self.plan != Plan.PRO:
            return False
        if self.expires_at is None:
            return True
        return datetime.now(timezone.utc) < self.expires_at

    def get_limit(self, key: str):
        plan = self.plan if self.is_active_pro else Plan.FREE
        return PLAN_LIMITS[plan][key]

    def __repr__(self) -> str:
        return f"<Subscription user_id={self.user_id} plan={self.plan}>"
