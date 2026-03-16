import secrets
from datetime import datetime, timezone
from flask_login import UserMixin
from app.extensions import db, login_manager


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    is_verified = db.Column(db.Boolean, default=False, nullable=False)
    verify_token = db.Column(db.String(64), nullable=True)

    # GDPR
    risk_disclaimer_accepted = db.Column(db.Boolean, default=False, nullable=False)
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=True)

    # Roles
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

    # Preferences
    preferred_lang = db.Column(db.String(2), default="en", nullable=False)

    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships
    api_key = db.relationship("ApiKey", back_populates="user", uselist=False, cascade="all, delete-orphan")
    bots = db.relationship("Bot", back_populates="user", cascade="all, delete-orphan")
    subscription = db.relationship("Subscription", back_populates="user", uselist=False, cascade="all, delete-orphan")
    telegram_account = db.relationship("TelegramAccount", back_populates="user", uselist=False, cascade="all, delete-orphan")

    def anonymize(self):
        """GDPR Art. 17 — erase personal data on account deletion."""
        self.email = f"deleted_{self.id}@anonymized.invalid"
        self.password_hash = ""
        self.verify_token = None
        self.is_deleted = True
        self.deleted_at = datetime.now(timezone.utc)

    def generate_verify_token(self) -> str:
        self.verify_token = secrets.token_urlsafe(32)
        return self.verify_token

    def __repr__(self) -> str:
        return f"<User {self.id} {self.email}>"


@login_manager.user_loader
def load_user(user_id: str):
    user = db.session.get(User, int(user_id))
    if user and user.is_deleted:
        return None
    return user
