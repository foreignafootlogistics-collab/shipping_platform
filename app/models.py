# app/models.py
from datetime import datetime
import hashlib
from flask_login import UserMixin
from app.extensions import db  # ✅ single source of truth for SQLAlchemy


# -------------------------------
# User and Wallet Models
# -------------------------------
class User(db.Model, UserMixin):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String, nullable=False, unique=True, index=True)
    password = db.Column(db.LargeBinary, nullable=False)
    role = db.Column(db.String, nullable=False, default='customer')
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
    referred_by = db.Column(db.Integer)
    referral_code = db.Column(db.String)
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
class Invoice(db.Model):
    __tablename__ = 'invoices'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    invoice_number = db.Column(db.String, unique=True, nullable=False, index=True)
    description = db.Column(db.String)

    # Totals across packages
    total_weight = db.Column(db.Float, default=0)
    invoice_value = db.Column(db.Float, default=0)

    # Breakdown fields (summed across packages)
    duty = db.Column(db.Float, default=0)
    scf = db.Column(db.Float, default=0)
    envl = db.Column(db.Float, default=0)
    caf = db.Column(db.Float, default=0)
    gct = db.Column(db.Float, default=0)
    handling = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)

    # Metadata
    status = db.Column(db.String, default="unpaid")
    date_submitted = db.Column(db.DateTime)
    date_paid = db.Column(db.DateTime)
    due_date = db.Column(db.DateTime)

    user = db.relationship("User", back_populates="invoices")
    packages = db.relationship("Package", back_populates="invoice", lazy=True)

    def __repr__(self):
        return f"<Invoice {self.invoice_number} User {self.user_id}>"


class Package(db.Model):
    __tablename__ = 'packages'

    id = db.Column(db.Integer, primary_key=True)
    house_number = db.Column(db.String)
    manifest_date = db.Column(db.String)
    customer_name = db.Column(db.String)
    customer_id = db.Column(db.String)
    merchant = db.Column(db.String)
    tracking_number = db.Column(db.String, index=True)
    date_received = db.Column(db.String)
    weight = db.Column(db.Numeric(10, 2))  # keep as Numeric if you need exact decimals
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    external_company = db.Column(db.Boolean, default=False)  # True if from the other company

    # Link to invoices
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoices.id'), nullable=True, index=True)

    description = db.Column(db.String)
    status = db.Column(db.String)
    received_date = db.Column(db.String)
    created_at = db.Column(db.String)
    house_awb = db.Column(db.String)
    value = db.Column(db.Float, default=99)
    amount_due = db.Column(db.Float, default=0)
    invoice_file = db.Column(db.String)
    declared_value = db.Column(db.Float)
    destination_country = db.Column(db.String)

    user = db.relationship('User', back_populates='packages')
    invoice = db.relationship('Invoice', back_populates='packages')

    def __repr__(self):
        return f"<Package {self.tracking_number} User {self.user_id}>"


class ShipmentLog(db.Model):
    __tablename__ = 'shipment_log'

    id = db.Column(db.Integer, primary_key=True)
    sl_id = db.Column(db.String, unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # relationship to packages through association table (defined below)
    packages = db.relationship('Package', secondary='shipment_packages', back_populates='shipments')

    def __repr__(self):
        return f"<ShipmentLog {self.sl_id}>"


# Association table for many-to-many
shipment_packages = db.Table(
    'shipment_packages',
    db.Column('shipment_id', db.Integer, db.ForeignKey('shipment_log.id')),
    db.Column('package_id', db.Integer, db.ForeignKey('packages.id'))
)

# Attach reverse relationship to Package
Package.shipments = db.relationship('ShipmentLog', secondary=shipment_packages, back_populates='packages')


class Prealert(db.Model):
    __tablename__ = 'prealerts'

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), index=True)
    vendor_name = db.Column(db.String)
    courier_name = db.Column(db.String)
    tracking_number = db.Column(db.String, index=True)
    purchase_date = db.Column(db.String)
    package_contents = db.Column(db.String)
    item_value_usd = db.Column(db.Float)
    invoice_filename = db.Column(db.String)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    prealert_number = db.Column(db.Integer)

    user = db.relationship('User', back_populates='prealerts')

    def __repr__(self):
        return f"<Prealert {self.prealert_number} Customer {self.customer_id}>"


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

    user = db.relationship('User', back_populates='scheduled_deliveries')


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
    rate = db.Column(db.Float, nullable=False)          # JMD cost for that bracket

    def __repr__(self):
        return f"<AdminRate {self.max_weight}lbs → {self.rate} JMD>"
