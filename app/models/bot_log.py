import json
from datetime import datetime, timezone
from sqlalchemy import event
from sqlalchemy.orm import Session
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


# ── Redis Pub/Sub: publish new BotLog rows after each DB commit ────────────
# Snapshot new BotLog instances before flush (IDs assigned during flush).
@event.listens_for(Session, "before_flush")
def _snapshot_new_bot_logs(session: Session, flush_context, instances) -> None:
    new_logs = [obj for obj in session.new if isinstance(obj, BotLog)]
    if new_logs:
        session.info.setdefault("_pending_log_publishes", []).extend(new_logs)


# After commit, publish accumulated entries to Redis (non-fatal on failure).
@event.listens_for(Session, "after_commit")
def _publish_bot_logs(session: Session) -> None:
    logs = session.info.pop("_pending_log_publishes", [])
    if not logs:
        return
    try:
        from app.extensions import get_redis
        r = get_redis()
        if r is None:
            return
        for log in logs:
            payload = json.dumps({
                "id":      log.id,
                "bot_id":  log.bot_id,
                "level":   log.level,
                "message": log.message,
                "ts":      (log.created_at or datetime.now(timezone.utc)).isoformat(),
            })
            r.publish(f"bot:{log.bot_id}:logs", payload)
    except Exception:
        pass  # Redis failure must never break the main flow


# Clear pending list on rollback to avoid stale data.
@event.listens_for(Session, "after_rollback")
def _clear_pending_bot_logs(session: Session) -> None:
    session.info.pop("_pending_log_publishes", None)
