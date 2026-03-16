from datetime import datetime, timezone
from app.extensions import db


class RiskConfig(db.Model):
    """Per-user global risk limits. All bots owned by a user share this config."""
    __tablename__ = "risk_configs"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"),
                           nullable=False, unique=True)
    enabled    = db.Column(db.Boolean, default=True, nullable=False)

    # Stop opening new positions if today's realised P&L drops below –N% of invested capital
    max_daily_loss_pct  = db.Column(db.Numeric(6, 2), nullable=True)

    # Emergency-stop all bots if equity drawdown (from all-time peak) exceeds N%
    max_drawdown_pct    = db.Column(db.Numeric(6, 2), nullable=True)

    # Never allow more than N bots to hold an open position simultaneously
    max_open_positions  = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True),
                           onupdate=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return (
            f"<RiskConfig user={self.user_id} "
            f"daily={self.max_daily_loss_pct}% "
            f"dd={self.max_drawdown_pct}% "
            f"max_pos={self.max_open_positions}>"
        )
