import sqlite3
import os
from datetime import datetime
from flask import Flask, render_template
from flask_mail import Mail
from itsdangerous import URLSafeTimedSerializer
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_migrate import Migrate
from flask_login import LoginManager

# Project config
from . import config  # app/config.py
from .config import PROFILE_UPLOAD_FOLDER, DB_PATH
from .forms import CalculatorForm, AdminCalculatorForm
from .utils.counters import ensure_counters_table  # NEW
from app.utils.unassigned import ensure_unassigned_user


# Centralized extensions (single source of truth)
# Make sure you have: app/extensions.py -> db = SQLAlchemy()
from .extensions import db  # ✅ use the shared instance
migrate = Migrate()
mail = Mail()
csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'  # default login for customers
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"

# Allowed file types for invoice upload
ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}

def allowed_file(filename: str) -> bool:
    """Check if the uploaded file has an allowed extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def create_app():
    app = Flask(__name__)

    # Ensure instance folder exists (used for tmp previews, etc.)
    os.makedirs(app.instance_path, exist_ok=True)

    # ==============================
    # Load Configuration
    # ==============================
    app.config.from_object(config)
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', config.SECRET_KEY)

    db_url = os.getenv("DATABASE_URL")
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    app.config['SQLALCHEMY_DATABASE_URI'] = db_url or f"sqlite:///{DB_PATH}"
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # Upload folders
    app.config['UPLOAD_FOLDER'] = os.path.join('static', 'invoices')
    app.config['PROFILE_UPLOAD_FOLDER'] = PROFILE_UPLOAD_FOLDER
    app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB max

    # Flask-Mail settings (consider env vars in production)
    app.config.update(
        MAIL_SERVER=os.getenv('MAIL_SERVER', 'smtp.example.com'),
        MAIL_PORT=int(os.getenv('MAIL_PORT', 587)),
        MAIL_USE_TLS=os.getenv('MAIL_USE_TLS', 'true').lower() == 'true',
        MAIL_USERNAME=os.getenv('MAIL_USERNAME', 'your-email@example.com'),
        MAIL_PASSWORD=os.getenv('MAIL_PASSWORD', 'your-email-password'),
        MAIL_DEFAULT_SENDER=os.getenv('MAIL_DEFAULT_SENDER', 'no-reply@yourdomain.com'),
        BASE_URL=os.getenv('BASE_URL', 'http://localhost:5000'),
    )

    # -------------------------------
    # Initialize Extensions
    # -------------------------------
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    # Mail can be optional in some envs
    try:
        mail.init_app(app)
    except Exception:
        app.logger.warning("MAIL init skipped or failed; continuing without mail.")

    # ---------------------------------------
    # One-time bootstraps that need context
    # ---------------------------------------
    with app.app_context():
        try:
            ensure_unassigned_user()
        except Exception as e:
            app.logger.warning(f"[UNASSIGNED] Failed on startup: {e}")

        try:
            ensure_counters_table()
        except Exception as e:
            app.logger.exception("[COUNTERS] Failed on startup: %s", e)

    # -------------------------------
    # User loader for Flask-Login
    # -------------------------------
    @login_manager.user_loader
    def load_user(user_id):
        from .models import User
        try:
            return User.query.get(int(user_id))
        except Exception:
            return None

    # -------------------------------
    # Local filesystem setup
    # -------------------------------
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    os.makedirs(app.config['PROFILE_UPLOAD_FOLDER'], exist_ok=True)
  
    # Token serializer (used by reset links, etc.)
    app.serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

    # -------------------------------
    # Template Context & Filters
    # -------------------------------
    @app.context_processor
    def inject_current_year():
        return {'current_year': datetime.now().year}

    @app.context_processor
    def inject_unread_notifications_count():
        from flask_login import current_user
        try:
            if current_user.is_authenticated:
                from .models import Notification
                count = Notification.query.filter_by(
                    user_id=current_user.id, is_read=False
                ).count()
            else:
                count = 0
        except Exception:
            count = 0
        return dict(unread_notifications_count=count)

    @app.context_processor
    def inject_categories():
        try:
            from .models import Category
            categories = [c.name for c in Category.query.all()]
        except Exception:
            categories = []
        return dict(categories=categories)

    @app.context_processor
    def inject_calculator_form():
        return {
            "calculator_form": CalculatorForm(),
            "admin_calculator_form": AdminCalculatorForm()
        }
    
    @app.context_processor
    def inject_settings():
        """Expose settings row to all templates as `settings`."""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM settings WHERE id=1")
            row = c.fetchone()
            conn.close()
            return {'settings': row}
        except Exception:
            return {'settings': None}

    @app.context_processor
    def inject_now():
        # Return the function (callable) so Jinja can call it if needed
        return {"now": datetime.utcnow}
    
    @app.template_filter('datetimeformat')
    def datetimeformat(value, format='%Y-%m-%d'):
        if not value:
            return ''
        try:
            dt = datetime.fromisoformat(value)
        except Exception:
            try:
                dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
            except Exception:
                dt = datetime.strptime(value, '%Y-%m-%d')
        return dt.strftime(format)

    @app.template_filter('currency')
    def currency(value):
        try:
            return "${:,.2f}".format(float(value))
        except (ValueError, TypeError):
            return "$0.00"

    # -------------------------------
    # Error Handlers
    # -------------------------------
    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        return render_template('csrf_error.html', reason=e.description), 400

    # -------------------------------
    # Register Blueprints
    # -------------------------------
    from .routes.customer_routes import customer_bp
    from .routes.admin_routes import admin_bp
    from .routes.auth_routes import auth_bp
    from .routes.accounts_profiles_routes import accounts_bp
    from .routes.admin_auth_routes import admin_auth_bp
    from .calculator import calculator_bp              # lazy import to avoid cycles
    from .routes.admin.calculator import admin_calculator_bp
    from .routes.logistics import logistics_bp
    from .routes.finance import finance_bp
    from .routes.settings import settings_bp
    from .routes.api_routes import api_bp

    app.register_blueprint(customer_bp, url_prefix='/customer')
    app.register_blueprint(admin_bp, url_prefix='/admin')
    app.register_blueprint(auth_bp)
    app.register_blueprint(accounts_bp, url_prefix='/accounts')
    app.register_blueprint(admin_auth_bp, url_prefix='/admin_auth')
    app.register_blueprint(calculator_bp, url_prefix='/calculator')
    app.register_blueprint(admin_calculator_bp)
    app.register_blueprint(logistics_bp)
    app.register_blueprint(finance_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(api_bp)

    @app.route('/')
    def index():
        return "Welcome to Foreign A Foot Logistics!"

    # Import models AFTER db.init_app so tables are registered
    from . import models  # noqa: F401
    from app.sqlite_utils import close_db
    app.teardown_appcontext(close_db)


    return app
