# app/__init__.py
import os
from datetime import datetime
from flask import Flask, render_template, redirect, url_for, jsonify, current_app
from flask_mail import Mail
from itsdangerous import URLSafeTimedSerializer
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_migrate import Migrate
from flask_login import LoginManager
from sqlalchemy import text

from . import config as cfg
from .config import PROFILE_UPLOAD_FOLDER
from .forms import CalculatorForm, AdminCalculatorForm
from .extensions import db  # SQLAlchemy shared instance
from werkzeug.middleware.proxy_fix import ProxyFix


migrate = Migrate()
mail = Mail()
csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"

ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png'}

def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _safe_ctx(fn):
    def wrapper():
        try:
            rv = fn()
            return rv if isinstance(rv, dict) else {}
        except Exception as e:
            app.logger.exception(f"[CTX_PROCESSOR_FAIL] {fn.__name__}: {e}")
            return {}
    wrapper.__name__ = fn.__name__
    return wrapper


def _ensure_first_admin():
    """
    Create ONE admin from ADMIN_EMAIL / ADMIN_PASSWORD **only if none exists**.
    Does NOT reset the password on later boots.
    """
    from .models import User
    email = os.getenv("ADMIN_EMAIL")
    password = os.getenv("ADMIN_PASSWORD")
    if not email or not password:
        current_app.logger.info("[ADMIN SEED] ADMIN_EMAIL/ADMIN_PASSWORD not set; skipping seed.")
        return

    existing = User.query.filter_by(email=email, role="admin").first()
    if existing:
        current_app.logger.info("[ADMIN SEED] Admin already exists; not modifying.")
        return

    import bcrypt
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))

    u = User(email=email, role="admin", full_name="Administrator")
    if hasattr(u, "is_admin"):
        u.is_admin = True
    u.password = hashed  # bytes for bcrypt
    db.session.add(u)
    db.session.commit()
    current_app.logger.info(f"[ADMIN SEED] Created admin user {email}.")



def create_app():
    app = Flask(__name__)

    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    # Folders
    os.makedirs(app.instance_path, exist_ok=True)
    os.makedirs("static/invoices", exist_ok=True)
    os.makedirs(str(PROFILE_UPLOAD_FOLDER), exist_ok=True)

    # Config
    app.config['SECRET_KEY'] = cfg.SECRET_KEY
    app.config['SQLALCHEMY_DATABASE_URI'] = cfg.SQLALCHEMY_DATABASE_URI
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = cfg.SQLALCHEMY_TRACK_MODIFICATIONS
    app.config['UPLOAD_FOLDER'] = os.path.join('static', 'invoices')
    app.config['PROFILE_UPLOAD_FOLDER'] = str(PROFILE_UPLOAD_FOLDER)
    app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024
    app.config.update(
        MAIL_SERVER=os.getenv('MAIL_SERVER', 'smtp.example.com'),
        MAIL_PORT=int(os.getenv('MAIL_PORT', 587)),
        MAIL_USE_TLS=os.getenv('MAIL_USE_TLS', 'true').lower() == 'true',
        MAIL_USERNAME=os.getenv('MAIL_USERNAME', 'your-email@example.com'),
        MAIL_PASSWORD=os.getenv('MAIL_PASSWORD', 'your-email-password'),
        MAIL_DEFAULT_SENDER=os.getenv('MAIL_DEFAULT_SENDER', 'no-reply@yourdomain.com'),
        BASE_URL=os.getenv('BASE_URL', 'http://localhost:5000'),
    )

    # Extensions
    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    try:
        mail.init_app(app)
    except Exception:
        app.logger.warning("MAIL init skipped or failed; continuing without mail.")
    
    @app.teardown_request
    def teardown_request(exc):
        if exc:
            db.session.rollback()
        db.session.remove()

    @app.template_filter("jmd")
    def jmd(value):
        try:
            return f"JMD {float(value):,.2f}"
        except (ValueError, TypeError):
            return "JMD 0.00"

    # Login user loader
    @login_manager.user_loader
    def load_user(user_id):
        from .models import User
        try:
            return User.query.get(int(user_id))
        except Exception:
            return None

    # Template context
    @app.context_processor
    @_safe_ctx
    def inject_current_year():
        return {'current_year': datetime.now().year}

    @app.context_processor
    @_safe_ctx
    def inject_unread_notifications_count():
        from flask_login import current_user
        try:
            if current_user.is_authenticated:
                from .models import Notification
                count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
            else:
                count = 0
        except Exception:
            count = 0
        return dict(unread_notifications_count=count)

    @app.context_processor
    @_safe_ctx
    def inject_categories():
        try:
            from .models import Category
            categories = [c.name for c in Category.query.all()]
        except Exception:
            categories = []
        return dict(categories=categories)

    @app.context_processor
    @_safe_ctx
    def inject_calculator_form():
        try:
            return {
                "calculator_form": CalculatorForm(),
                "admin_calculator_form": AdminCalculatorForm(),
            }
        except Exception:
            # NEVER return None from a context_processor
            return {
                "calculator_form": None,
                "admin_calculator_form": None,
            }


    @app.context_processor
    @_safe_ctx
    def inject_settings():
        try:
            from .models import Settings
            settings = Settings.query.get(1)
        except Exception:
            return {'settings': None}

    @app.context_processor
    @_safe_ctx
    def inject_now():
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

    # Errors
    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        return render_template('csrf_error.html', reason=e.description), 400

    # Blueprints
    from .routes.customer_routes import customer_bp
    from .routes.admin_routes import admin_bp
    from .routes.auth_routes import auth_bp
    from .routes.accounts_profiles_routes import accounts_bp
    from .routes.admin_auth_routes import admin_auth_bp
    from .calculator import calculator_bp
    from .routes.admin.calculator import admin_calculator_bp
    from .routes.logistics import logistics_bp
    from .routes.finance import finance_bp
    from .routes.settings import settings_bp
    from .routes.api_routes import api_bp
    from .routes.analytics_routes import analytics_bp


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
    app.register_blueprint(analytics_bp, url_prefix='/analytics')


    # Basic routes
    @app.route("/", methods=["GET"])
    def index():
        # If already logged in, send them where they belong
        from flask_login import current_user

        if current_user.is_authenticated:
            # admin users
            if getattr(current_user, "is_admin", False) or getattr(current_user, "role", "") == "admin":
                return redirect(url_for("admin.dashboard"))
            # customers
            return redirect(url_for("customer.customer_dashboard"))

        # not logged in → customer login page
        return redirect(url_for("auth.login"))


    @app.route("/api/health")
    def api_health_inline():
        return jsonify({"ok": True, "status": "up"})

    @app.route("/__routes")
    def __routes():
        try:
            rules = sorted([{
                "rule": r.rule,
                "endpoint": r.endpoint,
                "methods": sorted(list(r.methods - {"HEAD", "OPTIONS"}))
            } for r in app.url_map.iter_rules()], key=lambda x: x["rule"])
            return jsonify({"ok": True, "routes": rules})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/register")
    def public_register():
        for ep in ("auth.register", "register", "auth.signup"):
            try:
                return redirect(url_for(ep))
            except Exception:
                continue
        return "Register route not found. Check /__routes for the correct path.", 404

    @app.route("/login-customer")
    def public_login():
        for ep in ("auth.login_customer", "auth.login", "login_customer", "login"):
            try:
                return redirect(url_for(ep))
            except Exception:
                continue
        return "Customer login route not found. Check /__routes for the correct path.", 404

    # Optional debug route
    if os.getenv("ENABLE_DEBUG_ROUTES") == "1":
        @app.route("/__debug/db")
        def __debug_db():
            try:
                url = str(db.engine.url)
                name = db.engine.name
                n_users = db.session.execute(text("select count(*) from users")).scalar()
                emails = db.session.execute(text("select email from users limit 3")).scalars().all()
                return {
                    "engine_url": url,
                    "engine_name": name,
                    "user_count": int(n_users or 0),
                    "sample_emails": emails,
                }
            except Exception as e:
                return {"error": str(e)}

    # Import models and do one-time boot work
    from . import models  # noqa: F401
    with app.app_context():
        app.logger.info("[BOOT] App ready.")

    return app

