import enum
from datetime import datetime, timezone
from app.extensions import db


class BotStatus(str, enum.Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    ERROR = "error"


class Bot(db.Model):
    __tablename__ = "bots"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    name = db.Column(db.String(100), nullable=False)
    symbol = db.Column(db.String(20), nullable=False)              # e.g. BTCUSDT
    algorithm = db.Column(db.String(50), nullable=False)           # e.g. ma_crossover, rsi

    # Algorithm parameters stored as JSON
    # MA: {timeframe, fast_ma, slow_ma, stop_loss_pct, take_profit_pct, trailing_tp_pct}
    # RSI: {timeframe, rsi_period, oversold, overbought, stop_loss_pct, take_profit_pct, trailing_tp_pct}
    params = db.Column(db.JSON, nullable=False, default=dict)

    # Runtime state — also JSON (e.g. has_position, entry_price, max_price)
    state = db.Column(db.JSON, nullable=False, default=dict)

    status = db.Column(db.Enum(BotStatus), default=BotStatus.STOPPED, nullable=False)
    error_message = db.Column(db.Text, nullable=True)

    # Position sizing in quote currency (USDT)
    position_size_usdt = db.Column(db.Numeric(18, 8), nullable=False, default=50)

    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="bots")
    orders = db.relationship("Order", back_populates="bot", cascade="all, delete-orphan")
    logs = db.relationship("BotLog", back_populates="bot", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Bot {self.id} {self.symbol} [{self.algorithm}] {self.status}>"
