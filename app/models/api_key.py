from datetime import datetime, timezone
from app.extensions import db


class ApiKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)

    # Stored encrypted via Fernet (services/encryption.py)
    # Secret is NEVER exposed to frontend after saving
    encrypted_api_key = db.Column(db.Text, nullable=False)
    encrypted_api_secret = db.Column(db.Text, nullable=False)

    # Label (e.g. "Main Account") — plaintext is fine
    label = db.Column(db.String(100), default="Binance API Key")

    # Validation state
    is_valid = db.Column(db.Boolean, default=False)
    last_checked_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Cached list of available spot symbols from Binance exchange info
    # Populated automatically on successful key test
    # Format: [{"symbol": "BTCUSDT", "base": "BTC", "quote": "USDT"}, ...]
    cached_symbols = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="api_key")

    def __repr__(self) -> str:
        return f"<ApiKey user_id={self.user_id} valid={self.is_valid}>"
