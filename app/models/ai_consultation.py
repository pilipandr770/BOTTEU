"""AI Consultation model — stores history of AI analyses and recommendations."""
from datetime import datetime, timezone
from app.extensions import db


class AIConsultation(db.Model):
    """Stores each AI advisor analysis/recommendation for audit and display."""
    __tablename__ = "ai_consultations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bot_id = db.Column(
        db.Integer,
        db.ForeignKey("bots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    symbol = db.Column(db.String(20), nullable=False)

    # AI analysis results
    market_regime = db.Column(db.String(30), nullable=True)   # trending_up, ranging, etc.
    recommended_algorithm = db.Column(db.String(50), nullable=True)
    recommended_params = db.Column(db.JSON, nullable=True)
    recommended_timeframe = db.Column(db.String(10), nullable=True)
    confidence_score = db.Column(db.Integer, nullable=True)    # 0-100
    reasoning = db.Column(db.Text, nullable=True)

    # Raw data for debugging / re-analysis
    signal_matrix = db.Column(db.JSON, nullable=True)
    backtest_results = db.Column(db.JSON, nullable=True)

    # Whether the user (or autopilot) applied this recommendation
    applied = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    user = db.relationship("User", backref="ai_consultations")
    bot = db.relationship("Bot", backref="ai_consultations")

    def __repr__(self) -> str:
        return (
            f"<AIConsultation {self.id} {self.symbol} "
            f"regime={self.market_regime} conf={self.confidence_score}>"
        )
