import secrets
from datetime import datetime, timezone
from app.extensions import db


class TelegramAccount(db.Model):
    __tablename__ = "telegram_accounts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)

    # Set after user sends /start <code> in Telegram
    chat_id = db.Column(db.BigInteger, nullable=True, unique=True)

    # 6-digit code shown in cabinet, expires after 10 min
    link_code = db.Column(db.String(6), nullable=True)
    link_code_expires_at = db.Column(db.DateTime(timezone=True), nullable=True)

    is_verified = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", back_populates="telegram_account")

    def generate_link_code(self) -> str:
        """Generate a fresh 6-digit numeric code valid for 10 minutes."""
        from datetime import timedelta
        self.link_code = str(secrets.randbelow(900000) + 100000)  # 100000–999999
        self.link_code_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.is_verified = False
        self.chat_id = None
        return self.link_code

    def __repr__(self) -> str:
        return f"<TelegramAccount user_id={self.user_id} verified={self.is_verified}>"
