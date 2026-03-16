import enum
from datetime import datetime, timezone
from app.extensions import db


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExitReason(str, enum.Enum):
    SIGNAL = "SIGNAL"       # Algorithm produced exit signal
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_TP = "TRAILING_TP"
    MANUAL = "MANUAL"       # User pressed Stop


class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    bot_id = db.Column(db.Integer, db.ForeignKey("bots.id", ondelete="CASCADE"), nullable=False)

    binance_order_id = db.Column(db.String(64), nullable=True)
    symbol = db.Column(db.String(20), nullable=False)
    side = db.Column(db.Enum(OrderSide), nullable=False)

    price = db.Column(db.Numeric(24, 8), nullable=True)       # None = MARKET order
    qty = db.Column(db.Numeric(24, 8), nullable=False)
    quote_qty = db.Column(db.Numeric(24, 8), nullable=True)   # executed USDT value

    exit_reason = db.Column(db.Enum(ExitReason), nullable=True)

    # P&L: filled by bot_runner when SELL is executed
    pnl_usdt = db.Column(db.Numeric(24, 8), nullable=True)
    pnl_pct = db.Column(db.Numeric(10, 4), nullable=True)

    is_simulated = db.Column(db.Boolean, default=False)  # backtest orders

    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    bot = db.relationship("Bot", back_populates="orders")

    def __repr__(self) -> str:
        return f"<Order {self.id} {self.side} {self.symbol} qty={self.qty}>"
