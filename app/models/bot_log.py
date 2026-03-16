from datetime import datetime, timezone
from app.extensions import db


class BotLog(db.Model):
    __tablename__ = "bot_logs"

    id = db.Column(db.Integer, primary_key=True)
    bot_id = db.Column(
        db.Integer,
        db.ForeignKey("bots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # INFO  — normal tick (indicators, hold)
    # BUY   — buy signal
    # SELL  — sell signal
    # WARN  — warning (too few candles, etc.)
    # ERROR — execution error
    level = db.Column(db.String(10), nullable=False, default="INFO")
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    bot = db.relationship("Bot", back_populates="logs")

    def __repr__(self) -> str:
        return f"<BotLog {self.id} [{self.level}] {self.message[:40]}>"
