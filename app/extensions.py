"""
Shared Flask extensions — initialised here to avoid circular imports.
Imported and registered in app/__init__.py (app factory).
"""
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from flask_wtf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_babel import Babel

db = SQLAlchemy()
login_manager = LoginManager()
mail = Mail()
migrate = Migrate()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)
babel = Babel()

login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"
