# at the top of app/models.py (or wherever User lives)
import hashlib
import random
import string
from datetime import datetime
from flask_login import UserMixin
from app.extensions import db

# -------------------------------
# User and Wallet Models
# -------------------------------
class User(db.Model, UserMixin):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String, nullable=False, unique=True, index=True)
    password = db.Column(db.LargeBinary, nullable=False)

    role = db.Column(db.String(50), nullable=False, default='customer')
    # customer | admin | accounts_manager | finance | operations | etc.
    is_superadmin = db.Column(db.Boolean, default=False)

    full_name = db.Column(db.String)
    trn = db.Column(db.String)
    mobile = db.Column(db.String)
    registration_number = db.Column(db.String, index=True)
    created_at = db.Column(db.String)
    profile_pic = db.Column(db.String)
    profile_picture = db.Column(db.String)
    date_registered = db.Column(db.String)
    address = db.Column(db.String)
    wallet_balance = db.Column(db.Float, default=0.0)
   
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at_dt = db.Column(db.DateTime, default=datetime.utcnow)

    referred_by = db.Column(db.Integer)
    referral_code = db.Column(db.String, unique=True)  # <--- unique
    referrer_id = db.Column(db.Integer)
    is_admin = db.Column(db.Boolean, default=False)

    # Relationships
    wallet = db.relationship('Wallet', back_populates='user', uselist=False)
    wallet_transactions = db.relationship('WalletTransaction', back_populates='user', lazy='dynamic')
    invoices = db.relationship('Invoice', back_populates='user', lazy='dynamic')
    packages = db.relationship('Package', back_populates='user', lazy='dynamic')
    prealerts = db.relationship('Prealert', back_populates='user', lazy='dynamic')
    scheduled_deliveries = db.relationship('ScheduledDelivery', back_populates='user', lazy='dynamic')
    authorized_pickups = db.relationship('AuthorizedPickup', back_populates='user', lazy='dynamic')
    notifications = db.relationship('Notification', back_populates='user', lazy='dynamic')

    @property
    def initials_color(self):
        base = (self.full_name or "").strip() + (str(self.id) if self.id is not None else "")
        hex_digest = hashlib.md5(base.encode('utf-8')).hexdigest()
        return "#" + hex_digest[:6]

    def __repr__(self):
        return f"<User {self.email}>"

    @staticmethod
    def generate_referral_code(full_name: str) -> str:
        """
        Creates a referral code in the format:
        FAFL-FIRSTNAME-1234
        """
        if not full_name:
            base = "USER"
        else:
            base = full_name.split()[0].upper()  # first name only

        rand = str(random.randint(1000, 9999))
        return f"FAFL-{base}-{rand}"

class Wallet(db.Model):
    __tablename__ = 'wallets'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    ewallet_balance = db.Column(db.Float, default=0.0)
    bucks_balance = db.Column(db.Float, default=0.0)
    bucks_expiry = db.Column(db.Date)

    user = db.relationship('User', back_populates='wallet')

    def __repr__(self):
        return f"<Wallet User {self.user_id} Balance {self.ewallet_balance}>"


class WalletTransaction(db.Model):
    __tablename__ = 'wallet_transactions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    type = db.Column(db.String)

    user = db.relationship('User', back_populates='wallet_transactions')

    def __repr__(self):
        return f"<WalletTransaction User {self.user_id} Amount {self.amount}>"


# -------------------------------
# Invoice & Package Models
# -------------------------------
# -------------------------------
# Invoice & Package Models
# -------------------------------
class Invoice(db.Model):
    __tablename__ = 'invoices'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    invoice_number = db.Column(db.String, unique=True, nullable=False, index=True)
    description = db.Column(db.String)

    # Totals
    total_weight = db.Column(db.Float, default=0)
    invoice_value = db.Column(db.Float, default=0)

    duty = db.Column(db.Float, default=0)
    scf = db.Column(db.Float, default=0)
    envl = db.Column(db.Float, default=0)
    caf = db.Column(db.Float, default=0)
    gct = db.Column(db.Float, default=0)
    handling = db.Column(db.Float, default=0)

    # Finance
    amount = db.Column(db.Float, default=0)         # legacy
    amount_due = db.Column(db.Float, default=0)     # open balance
    grand_total = db.Column(db.Float, default=0)    # full sum with fees

    # Dates
    date_issued = db.Column(db.DateTime)
    date_submitted = db.Column(db.DateTime)
    date_paid = db.Column(db.DateTime)
    due_date = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    status = db.Column(db.String, default="unpaid")

    user = db.relationship("User", back_populates="invoices")
    packages = db.relationship("Package", back_populates="invoice", lazy=True)

    def __repr__(self):
        return f"<Invoice {self.invoice_number} User {self.user_id}>"


class Package(db.Model):
    __tablename__ = 'packages'

    id = db.Column(db.Integer, primary_key=True)

    # Admin input fields
    house_awb = db.Column(db.String)
    merchant = db.Column(db.String)
    description = db.Column(db.String)
    tracking_number = db.Column(db.String, index=True)
    shipper = db.Column(db.String(255))

    epc = db.Column(db.Integer, default=0, nullable=False)

    # Customer/user
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    external_company = db.Column(db.Boolean, default=False)

    # Dates
    received_date = db.Column(db.DateTime)   # when package reached JA
    date_received = db.Column(db.DateTime)   # legacy compatibility
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Weight / Value
    weight = db.Column(db.Float, default=0.00)
    declared_value = db.Column(db.Float)
    value = db.Column(db.Float, default=99)

    # Invoice linkage
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoices.id'), nullable=True, index=True)
    invoice_file = db.Column(db.String)

    # Scheduled delivery link
    scheduled_delivery_id = db.Column(
        db.Integer,
        db.ForeignKey("scheduled_deliveries.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    # Finance
    amount_due = db.Column(db.Float, default=0)

    # Status and destination
    status = db.Column(db.String, default="Overseas")
    destination_country = db.Column(db.String)

    # Relations
    user = db.relationship('User', back_populates='packages')
    invoice = db.relationship('Invoice', back_populates='packages')

    scheduled_delivery = db.relationship(
        "ScheduledDelivery",
        back_populates="packages"
    )

    # Shipment many-to-many
    shipments = db.relationship(
        'ShipmentLog',
        secondary='shipment_packages',
        back_populates='packages'
    )

    # ✅ FIX: This MUST be inside Package
    attachments = db.relationship(
        "PackageAttachment",
        back_populates="package",
        cascade="all, delete-orphan",
        lazy="select"
    )

    def __repr__(self):
        return f"<Package {self.tracking_number} User {self.user_id}>"


class PackageAttachment(db.Model):
    __tablename__ = "package_attachments"

    id = db.Column(db.Integer, primary_key=True)

    package_id = db.Column(
        db.Integer,
        db.ForeignKey("packages.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    file_name = db.Column(db.String(255), nullable=False)   # stored filename
    original_name = db.Column(db.String(255))               # what user uploaded
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # ✅ Matches Package.attachments
    package = db.relationship("Package", back_populates="attachments")


class ShipmentLog(db.Model):
    __tablename__ = 'shipment_log'

    id = db.Column(db.Integer, primary_key=True)
    sl_id = db.Column(db.String, unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    packages = db.relationship(
        'Package',
        secondary='shipment_packages',
        back_populates='shipments'
    )

    def __repr__(self):
        return f"<ShipmentLog {self.sl_id}>"


shipment_packages = db.Table(
    'shipment_packages',
    db.Column('shipment_id', db.Integer, db.ForeignKey('shipment_log.id')),
    db.Column('package_id', db.Integer, db.ForeignKey('packages.id'))
)



class Prealert(db.Model):
    __tablename__ = 'prealerts'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)

    vendor_name = db.Column(db.String)
    courier_name = db.Column(db.String)
    tracking_number = db.Column(db.String, index=True)
    package_contents = db.Column(db.String)

    purchase_date = db.Column(db.Date)  # FIXED from string → date object
    item_value_usd = db.Column(db.Float)

    invoice_filename = db.Column(db.String)
    prealert_number = db.Column(db.Integer)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', back_populates='prealerts')

    def __repr__(self):
        return f"<Prealert PA-{self.prealert_number}>"


# -------------------------------
# Scheduled Delivery & Authorized Pickup
# -------------------------------
class ScheduledDelivery(db.Model):
    __tablename__ = 'scheduled_deliveries'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    scheduled_date = db.Column(db.Date, nullable=False)
    scheduled_time = db.Column(db.String(20), nullable=False)
    location = db.Column(db.String(255), nullable=False)
    direction = db.Column(db.String(255))
    mobile_number = db.Column(db.String(50))
    person_receiving = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    status = db.Column(db.String(30), nullable=False, default="Scheduled", index=True)

    user = db.relationship('User', back_populates='scheduled_deliveries')

    packages = db.relationship(
        "Package",
        back_populates="scheduled_delivery",
        lazy="dynamic",
        passive_deletes=True
    )

class AuthorizedPickup(db.Model):
    __tablename__ = 'authorized_pickups'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    full_name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255))
    phone_number = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', back_populates='authorized_pickups')


# -------------------------------
# Notification & Calculator
# -------------------------------
class Notification(db.Model):
    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    message = db.Column(db.String(255), nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', back_populates='notifications')

    def __repr__(self):
        return f"<Notification {self.id} for User {self.user_id}>"


class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    sender = db.relationship("User", foreign_keys=[sender_id], backref="sent_messages")
    recipient = db.relationship("User", foreign_keys=[recipient_id], backref="received_messages")



class Discount(db.Model):
    __tablename__ = "discounts"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoices.id'), nullable=False)
    label = db.Column(db.String(120), default='Discount')
    amount_jmd = db.Column(db.Float, nullable=False, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    invoice = db.relationship('Invoice', backref=db.backref('discounts', lazy=True, cascade="all, delete-orphan"))

class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoices.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    method = db.Column(db.String(30), default="Cash")          # Cash | Card | Bank | Wallet
    amount_jmd = db.Column(db.Float, nullable=False, default=0.0)
    reference = db.Column(db.String(100))                      # POS ref #, bank ref, etc.
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    invoice = db.relationship('Invoice', backref=db.backref('payments', lazy=True, cascade="all, delete-orphan"))
    user = db.relationship('User', backref=db.backref('payments', lazy=True))


class CalculatorLog(db.Model):
    __tablename__ = 'calculator_logs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    category = db.Column(db.String(100))
    weight = db.Column(db.Float)
    value_usd = db.Column(db.Float, nullable=False)
    duty_amount = db.Column(db.Float)
    scf_amount = db.Column(db.Float)
    envl_amount = db.Column(db.Float)
    caf_amount = db.Column(db.Float)
    gct_amount = db.Column(db.Float)
    total_amount = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PendingReferral(db.Model):
    __tablename__ = 'pending_referrals'

    id = db.Column(db.Integer, primary_key=True)
    referrer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    referred_email = db.Column(db.String(120), nullable=False)
    accepted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AdminRate(db.Model):
    __tablename__ = "rate_brackets"

    id = db.Column(db.Integer, primary_key=True)
    max_weight = db.Column(db.Integer, nullable=False, unique=True, index=True)
    rate = db.Column(db.Float, nullable=False)  # JMD cost for that bracket

    def __repr__(self):
        return f"<AdminRate {self.max_weight}lbs → {self.rate} JMD>"

# Back-compat
RateBracket = AdminRate

class Expense(db.Model):
    __tablename__ = "expenses"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)              # stored as a calendar date
    category = db.Column(db.String(100), nullable=False)   # e.g. Rent, Fuel, Salaries
    amount = db.Column(db.Float, nullable=False, default=0.0)
    description = db.Column(db.String(255))

    def __repr__(self):
        return f"<Expense {self.id} {self.category} {self.amount}>"

# -------------------------------
# Settings (company / display / rates)
# -------------------------------
class Settings(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)

    # Company info
    company_name    = db.Column(db.String(255))
    company_address = db.Column(db.String(255))
    company_email   = db.Column(db.String(255))

    # Logo path relative to /static
    logo_path = db.Column(db.String(255))

    # Display & currency
    currency_code   = db.Column(db.String(10), default="JMD")
    currency_symbol = db.Column(db.String(10), default="$")
    usd_to_jmd      = db.Column(db.Float, default=0.0)
    date_format     = db.Column(db.String(32), default="%Y-%m-%d")

    # Legacy simple rates (you can still use these elsewhere if you want)
    base_rate    = db.Column(db.Float, default=0.0)
    handling_fee = db.Column(db.Float, default=0.0)

    # --- FREIGHT TAB (top of 2nd screenshot) ---
    # “Special Bill freight Below 1lb (JMD)”
    special_below_1lb_jmd   = db.Column(db.Numeric(10, 2), default=0)

    # “JMD Charge Per 0.1 lb Below 1lb”
    per_0_1lb_below_1lb_jmd = db.Column(db.Numeric(10, 2), default=0)

    # “Minimum Billable Weight”
    min_billable_weight     = db.Column(db.Integer, default=1)

    # “JMD Per Lb rate above 100Lbs”
    per_lb_above_100_jmd    = db.Column(db.Numeric(10, 2), default=0)

    # “JMD Handling Fee above 100Lbs”
    handling_above_100_jmd  = db.Column(db.Numeric(10, 2), default=0)

    # “Weight Rounding Method” (Always Round Up / Nearest, etc.)
    weight_round_method     = db.Column(db.String(50), default="round_up")

    # --- CUSTOMS/DUTY TAB (1st screenshot) ---
    # “Customs Duty Enabled” checkbox
    customs_enabled       = db.Column(db.Boolean, default=True)

    # “Customs Exchange Rate”
    customs_exchange_rate = db.Column(db.Numeric(10, 4), default=155)

    # “GCT 25 (%)”
    gct_25_rate           = db.Column(db.Numeric(5, 2), default=25)

    # “GCT 15 (%)”
    gct_15_rate           = db.Column(db.Numeric(5, 2), default=15)

    # “Customs CAF Residential (JMD)”
    caf_residential_jmd   = db.Column(db.Numeric(10, 2), default=2500)

    # “Customs CAF Commercial (JMD)”
    caf_commercial_jmd    = db.Column(db.Numeric(10, 2), default=5000)

    # “Customs Diminimis Point (USD)”
    diminis_point_usd     = db.Column(db.Numeric(10, 2), default=100)

    # “Default Duty Rate (%)”
    default_duty_rate     = db.Column(db.Numeric(5, 2), default=20)

    # “Customs Insurance Rate (%)”
    insurance_rate        = db.Column(db.Numeric(5, 2), default=1)

    # “Customs SCF Rate (%)”
    scf_rate              = db.Column(db.Numeric(5, 2), default=0.3)

    # “Customs ENVL Rate (%)”
    envl_rate             = db.Column(db.Numeric(5, 2), default=0.5)

    # “Stamp Duty (JMD)”
    stamp_duty_jmd        = db.Column(db.Numeric(10, 2), default=100)

    # Other text blobs
    branches       = db.Column(db.Text)       # JSON/text of branches/locations
    terms          = db.Column(db.Text)
    privacy_policy = db.Column(db.Text)

    # US warehouse address fields
    us_street       = db.Column(db.String(255))
    us_suite_prefix = db.Column(db.String(50))
    us_city         = db.Column(db.String(100))
    us_state        = db.Column(db.String(100))
    us_zip          = db.Column(db.String(20))

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

    def __repr__(self):
        return f"<Settings id={self.id}>"
class Counter(db.Model):
    __tablename__ = "counters"

    name = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Integer, default=0, nullable=False)

    def __repr__(self):
        return f"<Counter {self.name}={self.value}>"

