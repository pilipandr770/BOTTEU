import enum
from datetime import datetime, timezone
from app.extensions import db


class Plan(str, enum.Enum):
    FREE  = "free"   # no paid plan (default, 0 bots)
    BASIC = "basic"  # €200/mo — 1 bot, no AI/ML
    PRO   = "pro"    # €500/mo — 5 bots, AI recommendations
    ELITE = "elite"  # €1000/mo — unlimited bots, AI + ML


# Plan limits
PLAN_LIMITS = {
    Plan.FREE:  {"max_bots": 0},
    Plan.BASIC: {"max_bots": 1},
    Plan.PRO:   {"max_bots": 5},
    Plan.ELITE: {"max_bots": 9999},
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

    def _is_plan_active(self, plan: Plan) -> bool:
        # Admins always have Elite access — no payment needed
        if self.user and self.user.is_admin:
            return plan == Plan.ELITE
        if self.plan != plan:
            return False
        if self.expires_at is None:
            return True
        return datetime.now(timezone.utc) < self.expires_at

    @property
    def is_active_basic(self) -> bool:
        return self._is_plan_active(Plan.BASIC)

    @property
    def is_active_pro(self) -> bool:
        return self._is_plan_active(Plan.PRO)

    @property
    def is_active_elite(self) -> bool:
        return self._is_plan_active(Plan.ELITE)

    @property
    def is_any_paid(self) -> bool:
        """True if user has any active paid subscription."""
        return self.is_active_basic or self.is_active_pro or self.is_active_elite

    @property
    def has_ai(self) -> bool:
        """AI advisor access: PRO and ELITE."""
        return self.is_active_pro or self.is_active_elite

    @property
    def has_ml(self) -> bool:
        """ML ensemble access: ELITE only."""
        return self.is_active_elite

    @property
    def max_bots(self) -> int:
        if self.is_active_elite:
            return PLAN_LIMITS[Plan.ELITE]["max_bots"]
        if self.is_active_pro:
            return PLAN_LIMITS[Plan.PRO]["max_bots"]
        if self.is_active_basic:
            return PLAN_LIMITS[Plan.BASIC]["max_bots"]
        return PLAN_LIMITS[Plan.FREE]["max_bots"]

    def __repr__(self) -> str:
        return f"<Subscription user_id={self.user_id} plan={self.plan}>"
