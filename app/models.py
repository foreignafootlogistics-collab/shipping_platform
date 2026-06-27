# at the top of app/models.py (or wherever User lives)
import hashlib
import random
import string
import secrets
from datetime import datetime, timezone
from flask_login import UserMixin
from app.extensions import db
import re
from decimal import Decimal
from sqlalchemy import select

def normalize_tracking(s: str) -> str:
    """
    Make tracking comparisons consistent:
    - strip
    - remove ALL whitespace (spaces/tabs/newlines)
    - uppercase
    """
    if not s:
        return ""
    s = str(s).strip()
    s = re.sub(r"\s+", "", s)
    return s.upper()


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
    employee_code = db.Column(db.String(20), unique=True, index=True)

    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at_dt = db.Column(db.DateTime, default=datetime.utcnow)

    referred_by = db.Column(db.Integer)
    referral_code = db.Column(db.String, unique=True)  # <--- unique
    referrer_id = db.Column(db.Integer)
    is_admin = db.Column(db.Boolean, default=False)
    last_login = db.Column(db.DateTime, nullable=True)
    is_enabled = db.Column(db.Boolean, default=True)
    api_token = db.Column(db.String(128), unique=True, index=True, nullable=True)
    

    # Relationships
    wallet = db.relationship('Wallet', back_populates='user', uselist=False)
    wallet_transactions = db.relationship(
        'WalletTransaction',
        back_populates='user',
        lazy='dynamic',
        foreign_keys='WalletTransaction.user_id'
    )
    invoices = db.relationship('Invoice', back_populates='user', lazy='dynamic')
    packages = db.relationship('Package', back_populates='user', lazy='dynamic', foreign_keys='Package.user_id')
    prealerts = db.relationship(
        "Prealert",
        foreign_keys="Prealert.customer_id",
        back_populates="user",
        lazy="dynamic"
    )
    scheduled_deliveries = db.relationship('ScheduledDelivery', back_populates='user', lazy='dynamic')
    authorized_pickups = db.relationship('AuthorizedPickup', back_populates='user', lazy='dynamic')
    notifications = db.relationship('Notification', back_populates='user', lazy='dynamic')
    purchase_requests = db.relationship(
        "PurchaseRequest",
        foreign_keys="PurchaseRequest.user_id",
        back_populates="user",
        lazy="dynamic"
    )

    @property
    def is_active(self):
        # Flask-Login checks this to allow the user to be authenticated
        return bool(self.is_enabled)

    @is_active.setter
    def is_active(self, value):
        self.is_enabled = bool(value)


    @property
    def initials_color(self):
        base = (self.full_name or "").strip() + (str(self.id) if self.id is not None else "")
        hex_digest = hashlib.md5(base.encode('utf-8')).hexdigest()
        return "#" + hex_digest[:6]

    def __repr__(self):
        return f"<User {self.email}>"

    @staticmethod
    def generate_referral_code(length: int = 8) -> str:
        """
        Generate a random referral code like:
        9H253R75
        """
        import secrets

        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(secrets.choice(chars) for _ in range(length))


# =========================
# SUBSCRIPTION MODELS
# =========================

class SubscriptionPlan(db.Model):
    __tablename__ = "subscription_plans"

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(50), nullable=False, unique=True)
    description = db.Column(db.Text)

    price_usd = db.Column(db.Float, nullable=False)

    # Limits
    package_limit = db.Column(db.Integer, nullable=False)
    weight_limit = db.Column(db.Float, nullable=False)  # total monthly weight (billable)
    max_weight_per_package = db.Column(db.Float, nullable=False)

    # Plan type
    is_family_plan = db.Column(db.Boolean, default=False)
    max_users = db.Column(db.Integer, default=1)

    # Benefits
    priority_processing = db.Column(db.Boolean, default=False)

    # Overage rules
    overage_discount_percent = db.Column(db.Float, default=5.0)
    overage_discount_max_weight = db.Column(db.Float, default=10.0)

    is_active = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    subscriptions = db.relationship("Subscription", back_populates="plan")

    def __repr__(self):
        return f"<SubscriptionPlan {self.name}>"



class Subscription(db.Model):
    __tablename__ = "subscriptions"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    plan_id = db.Column(db.Integer, db.ForeignKey("subscription_plans.id"), nullable=False)

    start_date = db.Column(db.DateTime, nullable=False)
    end_date = db.Column(db.DateTime, nullable=False)

    status = db.Column(db.String(20), default="active")
    # active, exhausted, expired, cancelled

    auto_renew = db.Column(db.Boolean, default=False)
    renewal_reminder_5d_sent = db.Column(db.Boolean, default=False)
    renewal_reminder_2d_sent = db.Column(db.Boolean, default=False)
    expiry_notice_sent = db.Column(db.Boolean, default=False)

    # Family grouping
    parent_subscription_id = db.Column(
        db.Integer,
        db.ForeignKey("subscriptions.id"),
        nullable=True
    )

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    is_admin_waived = db.Column(db.Boolean, default=False)
    waiver_reason = db.Column(db.Text)
    waived_at = db.Column(db.DateTime)
    waived_by_admin_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True
    )

    # Relationships
    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        backref=db.backref("subscriptions", lazy=True)
    )

    waived_by_admin = db.relationship(
        "User",
        foreign_keys=[waived_by_admin_id]
    )

    plan = db.relationship("SubscriptionPlan", back_populates="subscriptions")

    usage = db.relationship(
        "SubscriptionUsage",
        back_populates="subscription",
        uselist=False,
        cascade="all, delete-orphan"
    )

    family_members = db.relationship(
        "SubscriptionMember",
        back_populates="subscription",
        lazy=True,
        cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Subscription user_id={self.user_id} plan={self.plan_id} status={self.status}>"



class SubscriptionUsage(db.Model):
    __tablename__ = "subscription_usage"

    id = db.Column(db.Integer, primary_key=True)

    subscription_id = db.Column(
        db.Integer,
        db.ForeignKey("subscriptions.id"),
        nullable=False,
        unique=True
    )

    packages_used = db.Column(db.Integer, default=0)
    weight_used = db.Column(db.Float, default=0.0)  # MUST be billable weight

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subscription = db.relationship("Subscription", back_populates="usage")

    def __repr__(self):
        return f"<SubscriptionUsage packages={self.packages_used} weight={self.weight_used}>"

class SubscriptionMember(db.Model):
    __tablename__ = "subscription_members"

    id = db.Column(db.Integer, primary_key=True)

    subscription_id = db.Column(
        db.Integer,
        db.ForeignKey("subscriptions.id"),
        nullable=False,
        index=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True
    )

    role = db.Column(db.String(20), nullable=False, default="member")
    status = db.Column(db.String(20), nullable=False, default="active")

    added_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )

    removed_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    subscription = db.relationship(
        "Subscription",
        back_populates="family_members"
    )

    user = db.relationship(
        "User",
        backref=db.backref("subscription_memberships", lazy=True)
    )

    __table_args__ = (
        db.UniqueConstraint(
            "subscription_id",
            "user_id",
            name="uq_subscription_member_user"
        ),
    )

    def __repr__(self):
        return f"<SubscriptionMember sub={self.subscription_id} user={self.user_id} role={self.role}>"

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


class SubscriptionInvite(db.Model):
    __tablename__ = "subscription_invites"

    id = db.Column(db.Integer, primary_key=True)

    subscription_id = db.Column(
        db.Integer,
        db.ForeignKey("subscriptions.id"),
        nullable=False,
        index=True
    )

    email = db.Column(db.String(255), nullable=False, index=True)

    token = db.Column(
        db.String(100),
        nullable=False,
        unique=True,
        index=True
    )

    status = db.Column(db.String(20), nullable=False, default="pending")
    # pending, accepted, expired, cancelled

    invited_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False
    )

    accepted_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True
    )

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )

    expires_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False
    )

    accepted_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    subscription = db.relationship(
        "Subscription",
        backref=db.backref("invites", lazy=True)
    )

    invited_by_user = db.relationship(
        "User",
        foreign_keys=[invited_by_user_id],
        backref=db.backref("sent_subscription_invites", lazy=True)
    )

    accepted_user = db.relationship(
        "User",
        foreign_keys=[accepted_user_id],
        backref=db.backref("accepted_subscription_invites", lazy=True)
    )

    def __repr__(self):
        return f"<SubscriptionInvite email={self.email} status={self.status}>"


class WalletTransaction(db.Model):
    __tablename__ = 'wallet_transactions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    amount = db.Column(db.Float, nullable=False)

    # required going forward
    description = db.Column(db.String)
    type = db.Column(db.String)

    # new audit fields
    action = db.Column(db.String(30), nullable=True, index=True)
    reason = db.Column(db.String(80), nullable=True, index=True)
    invoice_number = db.Column(db.String(40), nullable=True, index=True)
    package_id = db.Column(db.Integer, db.ForeignKey("packages.id"), nullable=True, index=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship(
        "User",
        back_populates="wallet_transactions",
        foreign_keys=[user_id],
        primaryjoin="WalletTransaction.user_id == User.id"
    )

    admin = db.relationship(
        "User",
        foreign_keys=[admin_id],
        primaryjoin="WalletTransaction.admin_id == User.id",
        lazy="joined"
    )

    package = db.relationship(
        "Package",
        foreign_keys=[package_id],
        lazy="joined"
    )

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

    image_url = db.Column(db.Text, nullable=True)

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
    # POS Discount Tracking
    subtotal_before_discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    discount_type = db.Column(db.String(20), nullable=False, default="none")
    discount_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    discount_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    # Dates
    date_issued = db.Column(db.DateTime)
    date_submitted = db.Column(db.DateTime)
    date_paid = db.Column(db.DateTime)
    due_date = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    status = db.Column(db.String, default="unpaid")

    user = db.relationship("User", back_populates="invoices")
    packages = db.relationship("Package", back_populates="invoice", lazy=True)

    invoice_emailed_at = db.Column(db.DateTime, nullable=True)
    invoice_email_failed = db.Column(db.Boolean, default=False, nullable=False)

    invoice_email_failed_at = db.Column(db.DateTime, nullable=True)
    invoice_email_failure_reason = db.Column(db.Text, nullable=True)

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

    # 🔑 NEW FIELD — CATEGORY
    category = db.Column(db.String(120), default="Other")   # ✅ ADD THIS LINE

    pricing_locked = db.Column(db.Boolean, default=False, nullable=False)

    # optional tracking
    pricing_locked_at = db.Column(db.DateTime, nullable=True)
    pricing_locked_by = db.Column(db.Integer, nullable=True)
    customer_notified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    customer_notified_by = db.Column(db.Integer, nullable=True)

    is_locked = db.Column(db.Boolean, default=False, nullable=False)
    locked_reason = db.Column(db.String(120), nullable=True)
    locked_at = db.Column(db.DateTime(timezone=True), nullable=True)


    # Weight / Value
    weight = db.Column(db.Float, default=0.00)
    declared_value = db.Column(db.Float)
    value = db.Column(db.Float, default=99)

    # Extra fees / adjustments
    other_charges = db.Column(db.Float, default=0.0, nullable=False)
    discount_due  = db.Column(db.Float, default=0.0, nullable=False)
    bad_address = db.Column(db.Boolean, nullable=False, default=False)
    bad_address_fee = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    subscription_applied = db.Column(db.Boolean, default=False, server_default="false", nullable=False)
    subscription_applied_at = db.Column(db.DateTime, nullable=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id"), nullable=True, index=True)
    subscription_result = db.Column(db.String(40), nullable=True)
    
    # Invoice linkage
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoices.id'), nullable=True, index=True)
    invoice_file = db.Column(db.String)

    ...


    # Scheduled delivery link
    scheduled_delivery_id = db.Column(
        db.Integer,
        db.ForeignKey("scheduled_deliveries.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    # Finance
    amount_due = db.Column(db.Float, default=0)

    # --- Pricing breakdown (JMD) ---
    duty  = db.Column(db.Float, default=0.0, nullable=False)
    gct   = db.Column(db.Float, default=0.0, nullable=False)
    scf   = db.Column(db.Float, default=0.0, nullable=False)
    envl  = db.Column(db.Float, default=0.0, nullable=False)
    caf   = db.Column(db.Float, default=0.0, nullable=False)
    stamp = db.Column(db.Float, default=0.0, nullable=False)

    customs_total = db.Column(db.Float, default=0.0, nullable=False)

    freight_fee   = db.Column(db.Float, default=0.0, nullable=False)
    handling_fee  = db.Column(db.Float, default=0.0, nullable=False)

    freight_total = db.Column(db.Float, default=0.0, nullable=False)
    grand_total   = db.Column(db.Float, default=0.0, nullable=False)


    # Status and destination
    status = db.Column(db.String, default="Overseas")
    destination_country = db.Column(db.String)

    current_location = db.Column(
        db.String(50),
        nullable=False,
        default="Warehouse",
        index=True
    )

    purchase_request_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_requests.id"),
        nullable=True,
        index=True
    )
  
    # --------------------------------------------------
    # Shipment Receiving Scan Tracking
    # --------------------------------------------------
    received_scan_status = db.Column(
        db.String(30),
        nullable=False,
        default="not_scanned",
        index=True
    )
    # not_scanned | scanned

    received_scanned_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    received_scanned_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True
    )

    received_scanned_by = db.relationship(
        "User",
        foreign_keys=[received_scanned_by_id],
        lazy="select"
    )

    delivery_scan_status = db.Column(
        db.String(30),
        nullable=False,
        default="not_scanned",
        index=True
    )

    delivery_scanned_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    delivery_scanned_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True
    )
    delivery_scanned_by = db.relationship(
        "User",
        foreign_keys=[delivery_scanned_by_id],
        lazy="select"
    )

    # Relations
    user = db.relationship('User', back_populates='packages', foreign_keys=[user_id])
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

    def __init__(self, *args, **kwargs):
        tn = kwargs.get("tracking_number")
        if tn is not None:
            kwargs["tracking_number"] = normalize_tracking(tn)
        super().__init__(*args, **kwargs)



class PackageAttachment(db.Model):
    __tablename__ = "package_attachments"

    id = db.Column(db.Integer, primary_key=True)

    package_id = db.Column(
        db.Integer,
        db.ForeignKey("packages.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # ✅ LEGACY column still exists in DB (keep it during transition)
    file_name = db.Column(db.String(255), nullable=False)

    # ✅ NEW Cloudinary URL (secure_url)
    file_url = db.Column(db.Text, nullable=False)

    original_name = db.Column(db.String(255))

    cloud_public_id = db.Column(db.String(255), nullable=True)
    cloud_resource_type = db.Column(db.String(20), nullable=True)  # "image" or "raw"

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    package = db.relationship("Package", back_populates="attachments")

    @property
    def url(self):
        # templates can use a.url and it works for old + new rows
        return self.file_url or self.file_name

from datetime import datetime, timezone

class ShipmentLog(db.Model):
    __tablename__ = 'shipment_log'

    id = db.Column(db.Integer, primary_key=True)
    sl_id = db.Column(db.String, unique=True, nullable=False, index=True)
    sl_name = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

   
    is_archived = db.Column(db.Boolean, nullable=False, default=False, index=True)    
    archived_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    archived_by_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    archive_reason = db.Column(db.String(64), nullable=True)
    archived_by_admin = db.relationship("User", foreign_keys=[archived_by_admin_id], lazy="joined")

    # ✅ prevents immediate auto-rearchive after manual unarchive
    unarchive_override_until = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    packages = db.relationship(
        'Package',
        secondary='shipment_packages',
        back_populates='shipments'
    )    

    def archive_locked(self) -> bool:
        if not self.unarchive_override_until:
            return False
        return self.unarchive_override_until > datetime.now(timezone.utc)

    def __repr__(self):
        return f"<ShipmentLog {self.sl_id}>"


shipment_packages = db.Table(
    'shipment_packages',
    db.Column('shipment_id', db.Integer, db.ForeignKey('shipment_log.id')),
    db.Column('package_id', db.Integer, db.ForeignKey('packages.id'))
)

class ShipmentArchiveLog(db.Model):
    __tablename__ = "shipment_archive_logs"

    id = db.Column(db.Integer, primary_key=True)
    shipment_id = db.Column(db.Integer, db.ForeignKey("shipment_log.id"), nullable=False, index=True)

    action = db.Column(db.String(24), nullable=False)  # ARCHIVE / UNARCHIVE / AUTO_ARCHIVE
    reason = db.Column(db.String(128), nullable=True)

    actor_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    actor_admin = db.relationship("User", foreign_keys=[actor_admin_id], lazy="joined")

    # ✅ timezone aware
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    shipment = db.relationship("ShipmentLog", backref=db.backref("archive_logs", lazy="dynamic"))

class ShipmentScanLog(db.Model):
    __tablename__ = "shipment_scan_logs"

    id = db.Column(db.Integer, primary_key=True)

    shipment_id = db.Column(
        db.Integer,
        db.ForeignKey("shipment_log.id"),
        nullable=False,
        index=True
    )

    package_id = db.Column(
        db.Integer,
        db.ForeignKey("packages.id"),
        nullable=True,
        index=True
    )

    scanned_value = db.Column(db.String(255), nullable=False, index=True)

    scan_result = db.Column(db.String(30), nullable=False, default="matched")
    # matched | already_scanned | not_found

    scanned_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True
    )

    scanned_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
    )

    notes = db.Column(db.String(255), nullable=True)

    shipment = db.relationship("ShipmentLog")
    package = db.relationship("Package")
    scanned_by = db.relationship("User")


class Claim(db.Model):
    __tablename__ = "claims"

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.String(30), unique=True, index=True)

    # who submitted
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    package_id = db.Column(db.Integer, db.ForeignKey("packages.id"), nullable=True, index=True)

    # identifiers (customer enters)
    house_awb = db.Column(db.String(64), nullable=False, index=True)
    tracking_number = db.Column(db.String(128), nullable=True, index=True)

    # money
    item_value_jmd = db.Column(db.Numeric(12, 2), nullable=False)

    # claim details
    description = db.Column(db.Text, nullable=True)

    # evidence uploads (Cloudinary URL + public id optional)
    invoice_url = db.Column(db.Text, nullable=False)
    invoice_public_id = db.Column(db.String(255), nullable=True)

    bank_statement_url = db.Column(db.Text, nullable=False)
    bank_statement_public_id = db.Column(db.String(255), nullable=True)

    # refund preference
    refund_method = db.Column(db.String(20), nullable=False, default="cash")  # cash|bank_transfer
    # --- Refund issued tracking ---
    refund_issued = db.Column(db.Boolean, nullable=False, default=False)
    refund_issued_method = db.Column(db.String(30), nullable=True)  # cash|bank_transfer|wallet_credit
    refund_reference = db.Column(db.String(120), nullable=True)     # txn ref / receipt # (optional)
    refund_issued_at = db.Column(db.DateTime(timezone=True), nullable=True)
    refund_issued_by_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    refunded_amount_jmd = db.Column(db.Numeric(12, 2), nullable=True)  # what was actually issued

    refund_issued_by = db.relationship("User", foreign_keys=[refund_issued_by_admin_id], lazy="joined")

    # bank transfer details (only if refund_method == bank_transfer)
    bank_account_name = db.Column(db.String(120), nullable=True)
    bank_branch = db.Column(db.String(120), nullable=True)
    bank_account_number = db.Column(db.String(60), nullable=True)
    bank_account_type = db.Column(db.String(20), nullable=True)  # savings|chequing

    # workflow
    status = db.Column(db.String(30), nullable=False, default="submitted", index=True)
    # submitted | under_review | need_more_info | approved | rejected | paid

    admin_notes = db.Column(db.Text, nullable=True)
    decision_reason = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                       onupdate=lambda: datetime.now(timezone.utc))

    reviewed_by_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    approved_amount_jmd = db.Column(db.Numeric(12, 2), nullable=True)
    paid_at = db.Column(db.DateTime(timezone=True), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id], backref=db.backref("claims", lazy="dynamic"))
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_admin_id], lazy="joined")
    package = db.relationship("Package", foreign_keys=[package_id], lazy="joined")


class ClaimAuditLog(db.Model):
    __tablename__ = "claim_audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    claim_id = db.Column(db.Integer, db.ForeignKey("claims.id"), nullable=False, index=True)

    action = db.Column(db.String(60), nullable=False)  # created, status_changed, note_added, approved, rejected, marked_paid, etc.
    from_status = db.Column(db.String(30), nullable=True)
    to_status = db.Column(db.String(30), nullable=True)

    actor_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    claim = db.relationship("Claim", backref=db.backref("audit_logs", lazy="dynamic", cascade="all, delete-orphan"))

class PackageSearchCase(db.Model):
    __tablename__ = "package_search_cases"

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.String(30), unique=True, index=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # customer will have this from merchant/seller
    tracking_number = db.Column(db.String(128), nullable=False, index=True)

    # if you require the customer to enter it, keep required
    delivered_date = db.Column(db.Date, nullable=False)

    # proof required
    proof_url = db.Column(db.Text, nullable=False)
    proof_public_id = db.Column(db.String(255), nullable=True)

    notes = db.Column(db.Text, nullable=True)

    status = db.Column(db.String(30), nullable=False, default="submitted", index=True)
    # submitted | searching | found | not_found | resolved

    admin_notes = db.Column(db.Text, nullable=True)

    # ✅ badge / "new" tracking for admin
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)

    # optional audit (recommended)
    updated_by_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", foreign_keys=[user_id], lazy="joined")
    updated_by_admin = db.relationship("User", foreign_keys=[updated_by_admin_id], lazy="joined")

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

    invoice_original_name = db.Column(db.String(255), nullable=True)
    invoice_public_id = db.Column(db.String(255), nullable=True)
    invoice_resource_type = db.Column(db.String(20), nullable=True)  # "raw" or "image"
    
    # ✅ Link to Package (prevents duplicates + shows which package got the invoice)
    linked_package_id = db.Column(
        db.Integer,
        db.ForeignKey("packages.id"),
        nullable=True,
        index=True
    )
    linked_at = db.Column(db.DateTime(timezone=True), nullable=True)  # ✅ timezone-aware

    # ✅ Manual admin lock for cases where package arrived but tracking did not match
    is_locked = db.Column(db.Boolean, default=False, nullable=False, index=True)
    locked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    locked_by_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    lock_reason = db.Column(db.String(255), nullable=True)

    prealert_number = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship(
        "User",
        foreign_keys=[customer_id],
        back_populates="prealerts"
    )

    # ✅ optional but helpful relationship
    linked_package = db.relationship("Package", foreign_keys=[linked_package_id])

    locked_by_admin = db.relationship(
        "User",
        foreign_keys=[locked_by_admin_id]
    )

    
    def __repr__(self):
        return f"<Prealert PA-{self.prealert_number}>"

    def __init__(self, *args, **kwargs):
        tn = kwargs.get("tracking_number")
        if tn is not None:
            kwargs["tracking_number"] = normalize_tracking(tn)
        super().__init__(*args, **kwargs)

pickup_packages = db.Table(
    "pickup_packages",
    db.Column(
        "pickup_id",
        db.Integer,
        db.ForeignKey("scheduled_pickups.id", ondelete="CASCADE"),
        primary_key=True
    ),
    db.Column(
        "package_id",
        db.Integer,
        db.ForeignKey("packages.id", ondelete="CASCADE"),
        primary_key=True
    ),
)

class PrealertAttachment(db.Model):
    __tablename__ = "prealert_attachments"

    id = db.Column(db.Integer, primary_key=True)

    prealert_id = db.Column(
        db.Integer,
        db.ForeignKey("prealerts.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    file_url = db.Column(db.Text, nullable=False)
    original_name = db.Column(db.String(255))

    cloud_public_id = db.Column(db.String(255), nullable=True)
    cloud_resource_type = db.Column(db.String(20), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    prealert = db.relationship(
        "Prealert",
        backref=db.backref(
            "attachments",
            lazy="select",
            cascade="all, delete-orphan"
        )
    )

class PurchaseRequest(db.Model):
    __tablename__ = "purchase_requests"

    id = db.Column(db.Integer, primary_key=True)

    request_number = db.Column(
        db.String(30),
        unique=True,
        nullable=False,
        index=True
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True
    )

    product_url = db.Column(db.Text, nullable=False)

    store_name = db.Column(db.String(120))
    item_name = db.Column(db.String(255))

    quantity = db.Column(db.Integer, default=1)

    color = db.Column(db.String(100))
    size = db.Column(db.String(100))

    item_price_usd = db.Column(db.Numeric(12,2), default=0)
    service_fee_jmd = db.Column(db.Numeric(12,2), default=0)

    quoted_item_price_usd = db.Column(db.Numeric(12,2))
    quoted_service_fee_jmd = db.Column(db.Numeric(12,2))
    quoted_at = db.Column(db.DateTime)

    customer_fafl_number = db.Column(db.String(20))

    order_number = db.Column(db.String(120))
    merchant_tracking_number = db.Column(db.String(255))

    notes = db.Column(db.Text)

    status = db.Column(
        db.String(30),
        nullable=False,
        default="requested",
        index=True
    )

    invoice_id = db.Column(
        db.Integer,
        db.ForeignKey("invoices.id"),
        nullable=True,
        index=True
    )

    package_id = db.Column(
        db.Integer,
        db.ForeignKey("packages.id"),
        nullable=True,
        index=True
    )

    purchased_by_admin_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True
    )

    purchased_at = db.Column(db.DateTime)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        back_populates="purchase_requests"
    )

    invoice = db.relationship(
        "Invoice",
        foreign_keys=[invoice_id]
    )

    package = db.relationship(
        "Package",
        foreign_keys=[package_id]
    )

    purchased_by = db.relationship(
        "User",
        foreign_keys=[purchased_by_admin_id]
    )

class PurchaseRequestItem(db.Model):
    __tablename__ = "purchase_request_items"

    id = db.Column(db.Integer, primary_key=True)

    purchase_request_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    product_url = db.Column(db.Text, nullable=False)
    store_name = db.Column(db.String(120))
    item_name = db.Column(db.String(255))

    quantity = db.Column(db.Integer, default=1)

    color = db.Column(db.String(100))
    size = db.Column(db.String(100))

    notes = db.Column(db.Text)

    item_price_usd = db.Column(db.Numeric(12, 2), default=0)
    quoted_item_price_usd = db.Column(db.Numeric(12, 2))

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    purchase_request = db.relationship(
        "PurchaseRequest",
        backref=db.backref(
            "items",
            lazy="select",
            cascade="all, delete-orphan"
        )
    )

class PurchaseRequestAttachment(db.Model):
    __tablename__ = "purchase_request_attachments"

    id = db.Column(db.Integer, primary_key=True)

    purchase_request_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    file_url = db.Column(db.Text, nullable=False)

    original_name = db.Column(db.String(255))

    cloud_public_id = db.Column(db.String(255))
    cloud_resource_type = db.Column(db.String(20))

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow
    )

    purchase_request = db.relationship(
        "PurchaseRequest",
        backref=db.backref(
            "attachments",
            lazy="select",
            cascade="all, delete-orphan"
        )
    )


class ScheduledPickup(db.Model):
    __tablename__ = "scheduled_pickups"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True
    )

    pickup_date = db.Column(
        db.Date,
        nullable=False,
        index=True
    )

    branch = db.Column(
        db.String(100),
        nullable=False,
        default="Gregory Park"
    )

    status = db.Column(
        db.String(30),
        nullable=False,
        default="Scheduled",
        index=True
    )
    # Scheduled | Ready | Collected | Cancelled

    authorized_person = db.Column(db.String(255))
    notes = db.Column(db.Text)

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )

    completed_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    user = db.relationship(
        "User",
        backref=db.backref(
            "scheduled_pickups",
            lazy="dynamic"
        )
    )

    packages = db.relationship(
        "Package",
        secondary=pickup_packages,
        lazy="dynamic",
        backref=db.backref(
            "scheduled_pickups",
            lazy="dynamic"
        )
    )

# -------------------------------
# Scheduled Delivery & Authorized Pickup
# -------------------------------
class ScheduledDelivery(db.Model):
    __tablename__ = 'scheduled_deliveries'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    scheduled_date = db.Column(db.Date, nullable=False)
    scheduled_time = db.Column(db.String(20), nullable=False)
    scheduled_time_from = db.Column(db.String(20), nullable=True)
    scheduled_time_to   = db.Column(db.String(20), nullable=True)

    location = db.Column(db.String(255), nullable=False)

    # ✅ ADD THIS
    area_zone = db.Column(db.String(30), nullable=False, default="kgn_core", index=True)
    # -----------------------------
    # Distance / Map Tracking
    # -----------------------------
    delivery_parish = db.Column(db.String(100), nullable=True, index=True)

    delivery_branch = db.Column(
        db.String(100),
        nullable=True,
        index=True
    )
    # Kingston Dispatch | Gregory Park Branch

    distance_km = db.Column(db.Float, nullable=True)

    estimated_drive_minutes = db.Column(db.Integer, nullable=True)

    delivery_type = db.Column(
        db.String(30),
        nullable=False,
        default="free_route",
        index=True
    )
    # free_route | express | special_request

    is_free_delivery = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )

    delivery_latitude = db.Column(db.Float, nullable=True)
    delivery_longitude = db.Column(db.Float, nullable=True)

    google_place_id = db.Column(db.String(255), nullable=True)

    delivery_risk_status = db.Column(
        db.String(20),
        nullable=False,
        default="safe",
        index=True
    )
    # safe | caution | restricted | blocked

    delivery_notes = db.Column(db.Text, nullable=True)

    direction = db.Column(db.String(255))
    mobile_number = db.Column(db.String(50))
    person_receiving = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    status = db.Column(db.String(30), nullable=False, default="Scheduled", index=True)

    invoice_number = db.Column(db.String(40), unique=True, index=True)
    delivery_fee = db.Column(db.Numeric(10, 2), nullable=False, default=Decimal("1000.00"))
    fee_currency = db.Column(db.String(10), nullable=False, default="JMD")
    fee_status = db.Column(db.String(20), nullable=False, default="Unpaid")
    paid_at = db.Column(db.DateTime)
    delivered_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    reschedule_requested = db.Column(
        db.Boolean,
        default=False
    )

    reschedule_requested_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True
    )

    requested_new_date = db.Column(
        db.Date,
        nullable=True
    )

    reschedule_reason = db.Column(
        db.Text,
        nullable=True
    )

    reschedule_status = db.Column(
        db.String(30),
        default="none"
    )

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
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True
    )

    # ✅ NEW: short title for the notification
    subject = db.Column(db.String(120), nullable=False, default="Notification")

    message = db.Column(db.String(255), nullable=False)

    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    is_broadcast = db.Column(db.Boolean, nullable=False, default=False, index=True)

    # ✅ keep timezone-aware UTC timestamps
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
    )

    user = db.relationship("User", back_populates="notifications")

    def __repr__(self):
        return f"<Notification {self.id} {self.subject} for User {self.user_id}>"



class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    subject = db.Column(db.String(255), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # ✅ NEW (threads + archive/delete per user)
    thread_key = db.Column(db.String(64), index=True)
    archived_by_sender = db.Column(db.Boolean, default=False, nullable=False)
    archived_by_recipient = db.Column(db.Boolean, default=False, nullable=False)
    deleted_by_sender = db.Column(db.Boolean, default=False, nullable=False)
    deleted_by_recipient = db.Column(db.Boolean, default=False, nullable=False)

    sender = db.relationship("User", foreign_keys=[sender_id], backref="sent_messages")
    recipient = db.relationship("User", foreign_keys=[recipient_id], backref="received_messages")


class MessageAttachment(db.Model):
    __tablename__ = "message_attachments"

    id = db.Column(db.Integer, primary_key=True)

    message_id = db.Column(
        db.Integer,
        db.ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    file_url = db.Column(db.Text, nullable=False)
    original_name = db.Column(db.String(255))

    cloud_public_id = db.Column(db.String(255), nullable=True)
    cloud_resource_type = db.Column(db.String(20), nullable=True)

    created_at = db.Column(
        db.DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )

    message = db.relationship(
        "Message",
        backref=db.backref("attachments", lazy="select", cascade="all, delete-orphan")
    )

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

    # ---------------------------------------------------------
    # Core ownership
    # ---------------------------------------------------------
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True
    )

    # ---------------------------------------------------------
    # Admin / audit
    # ---------------------------------------------------------
    authorized_by_admin_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True
    )

    source = db.Column(
        db.String(30),
        nullable=False,
        default="admin",
        index=True
    )
    # admin | customer_portal | system_auto | wallet

    # ---------------------------------------------------------
    # Optional linked records
    # ---------------------------------------------------------
    invoice_id = db.Column(
        db.Integer,
        db.ForeignKey("invoices.id"),
        nullable=True,
        index=True
    )

    scheduled_delivery_id = db.Column(
        db.Integer,
        db.ForeignKey("scheduled_deliveries.id"),
        nullable=True,
        index=True
    )

    package_id = db.Column(
        db.Integer,
        db.ForeignKey("packages.id"),
        nullable=True,
        index=True
    )

    claim_id = db.Column(
        db.Integer,
        db.ForeignKey("claims.id"),
        nullable=True,
        index=True
    )

    # ---------------------------------------------------------
    # Transaction details
    # ---------------------------------------------------------
    method = db.Column(db.String(30), default="Cash")
    amount_jmd = db.Column(db.Float, nullable=False, default=0.0)
    reference = db.Column(db.String(100), nullable=True)
    notes = db.Column(db.String(255), nullable=True)

    # ---------------------------------------------------------
    # Transaction classification
    # ---------------------------------------------------------
    transaction_type = db.Column(
        db.String(30),
        nullable=False,
        default="invoice_payment",
        index=True
    )
    # invoice_payment | delivery_payment | package_refund | delivery_refund | wallet_credit | adjustment

    status = db.Column(
        db.String(20),
        nullable=False,
        default="completed",
        index=True
    )
    # pending | completed | failed | reversed

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        index=True
    )

    # ---------------------------------------------------------
    # Relationships
    # ---------------------------------------------------------
    invoice = db.relationship(
        "Invoice",
        backref=db.backref("payments", lazy=True, cascade="all, delete-orphan"),
        foreign_keys=[invoice_id]
    )

    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        backref=db.backref("payments", lazy=True, foreign_keys="Payment.user_id")
    )

    authorized_by = db.relationship(
        "User",
        foreign_keys=[authorized_by_admin_id],
        lazy="joined"
    )

    scheduled_delivery = db.relationship(
        "ScheduledDelivery",
        backref=db.backref("payments", lazy=True),
        foreign_keys=[scheduled_delivery_id]
    )

    package = db.relationship(
        "Package",
        backref=db.backref("payments", lazy=True),
        foreign_keys=[package_id]
    )

    claim = db.relationship(
        "Claim",
        backref=db.backref("payments", lazy=True),
        foreign_keys=[claim_id]
    )

    @property
    def display_type(self):
        labels = {
            "invoice_payment": "Invoice Payment",
            "delivery_payment": "Delivery Payment",
            "package_refund": "Package Refund",
            "delivery_refund": "Delivery Refund",
            "wallet_credit": "Wallet Credit",
            "adjustment": "Adjustment",
        }
        return labels.get(self.transaction_type, "Transaction")

    @property
    def is_refund(self):
        return self.transaction_type in {"package_refund", "delivery_refund", "wallet_credit"}

    @property
    def is_payment(self):
        return self.transaction_type in {"invoice_payment", "delivery_payment"}

    def __repr__(self):
        return f"<Payment id={self.id} type={self.transaction_type} user_id={self.user_id} amount_jmd={self.amount_jmd} status={self.status}>"


class POSCloseout(db.Model):
    __tablename__ = "pos_closeouts"

    id = db.Column(db.Integer, primary_key=True)

    business_date = db.Column(db.Date, nullable=False, unique=True, index=True)

    expected_cash = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    expected_card = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    expected_transfer = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    expected_total = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    expected_discount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    actual_cash = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    cash_difference = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    notes = db.Column(db.Text, nullable=True)

    closed_by_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    closed_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    closed_by = db.relationship("User", foreign_keys=[closed_by_admin_id], lazy="joined")


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
    date = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(120), nullable=False)
    amount = db.Column(db.Float, nullable=False, default=0.0)
    description = db.Column(db.Text)

    # ✅ Attachment (Cloudinary-only)
    attachment_name = db.Column(db.String(255))       # original filename
    attachment_url = db.Column(db.String(500))        # Cloudinary secure_url
    attachment_public_id = db.Column(db.String(255))  # Cloudinary public_id (for delete)
    attachment_mime = db.Column(db.String(120))
    attachment_uploaded_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ExpenseAuditLog(db.Model):
    __tablename__ = "expense_audit_logs"

    id = db.Column(db.Integer, primary_key=True)

    # The expense being acted on (nullable if needed later)
    expense_id = db.Column(db.Integer, nullable=True, index=True)

    # action: CREATED / UPDATED / DELETED
    action = db.Column(db.String(20), nullable=False)

    # actor info
    actor_id = db.Column(db.Integer, nullable=True)
    actor_email = db.Column(db.String(255))
    actor_role = db.Column(db.String(50))

    # snapshot of expense at time of action (especially important for deletes)
    expense_date = db.Column(db.Date)
    expense_category = db.Column(db.String(120))
    expense_amount = db.Column(db.Float)
    expense_description = db.Column(db.Text)
    expense_attachment_name = db.Column(db.String(255))
    expense_attachment_stored = db.Column(db.String(255))

    # request metadata
    ip_address = db.Column(db.String(64))
    user_agent = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


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
    bad_address_fee_jmd = db.Column(db.Numeric(10, 2), default=500)

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
    customs_exchange_rate = db.Column(db.Numeric(10, 4), default=165)

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

    # -----------------------------
    # Delivery Settings
    # -----------------------------

    kingston_dispatch_address = db.Column(
        db.String(255),
        default="4 Park Boulevard, Kingston 5, Jamaica"
    )

    stc_dispatch_address = db.Column(
        db.String(255),
        default="Unit 7, Lot C22, Cedar Manor, Gregory Park, St. Catherine, Jamaica"
    )

    kingston_delivery_branch_name = db.Column(
        db.String(100),
        default="Kingston Dispatch"
    )

    stc_delivery_branch_name = db.Column(
        db.String(100),
        default="Gregory Park Branch"
    )

    delivery_base_km = db.Column(
        db.Float,
        default=10.0
    )

    delivery_base_fee_jmd = db.Column(
        db.Numeric(10,2),
        default=1000
    )

    delivery_per_km_jmd = db.Column(
        db.Numeric(10,2),
        default=100
    )

    google_maps_api_key = db.Column(
        db.String(255),
        nullable=True
    )

    # Free delivery days
    kingston_free_delivery_days = db.Column(
        db.String(50),
        default="Tuesday,Thursday"
    )

    stc_free_delivery_days = db.Column(
        db.String(50),
        default="Friday"
    )

    # Delivery operational limits
    max_delivery_distance_km = db.Column(
        db.Float,
        default=35.0
    )

    allow_saturday_delivery = db.Column(
        db.Boolean,
        default=True
    )

    saturday_delivery_fee_jmd = db.Column(
        db.Numeric(10,2),
        default=1000
    )
    

    # Registration number settings
    registration_prefix = db.Column(db.String(20), default="FAFL")
    registration_number_width = db.Column(db.Integer, default=5)

    reuse_deleted_registration_numbers = db.Column(db.Boolean, default=False)
    lock_registration_number = db.Column(db.Boolean, default=True)

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

def next_counter_value(name: str) -> int:
    """
    Atomically increment and return counter value.
    Works across date changes because it's global.
    """
    row = db.session.execute(
        select(Counter).where(Counter.name == name).with_for_update()
    ).scalar_one_or_none()

    if not row:
        row = Counter(name=name, value=0)
        db.session.add(row)
        db.session.flush()

    row.value = (row.value or 0) + 1
    db.session.flush()
    return row.value


def generate_claim_case_id(prefix: str = "CLM") -> str:
    """
    Case ID format example:
      CLM-20260303-000001
    - date changes daily
    - last 6 digits keep counting forever
    """
    seq = next_counter_value("claim_case_seq")   # <-- global, never resets
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{prefix}-{date_part}-{seq:06d}"

def generate_search_case_id(prefix: str = "SRC") -> str:
    seq = next_counter_value("search_case_seq")
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{prefix}-{date_part}-{seq:06d}"

def generate_purchase_request_number(prefix: str = "PS") -> str:
    seq = next_counter_value("purchase_request_seq")
    return f"{prefix}{seq:06d}"

def calculate_purchase_service_fee(item_value_usd, usd_to_jmd=162):
    item_value_usd = float(item_value_usd or 0)

    if item_value_usd <= 50:
        return 1000
    elif item_value_usd <= 100:
        return 1500
    elif item_value_usd <= 250:
        return 2000
    elif item_value_usd <= 500:
        return 3500
    else:
        return round((item_value_usd * 0.05) * float(usd_to_jmd or 162), 2)


class EmployeePayroll(db.Model):
    __tablename__ = "employee_payroll"

    id = db.Column(db.Integer, primary_key=True)    
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    position_title = db.Column(db.String(100), nullable=True, index=True)

    pay_type = db.Column(db.String(20), nullable=False)  # salary / hourly

    # monthly / fortnightly
    pay_frequency = db.Column(db.String(20), nullable=False, default="monthly", index=True)

    base_salary = db.Column(db.Numeric(12, 2), default=0)
    hourly_rate = db.Column(db.Numeric(12, 2), default=0)

    is_active = db.Column(db.Boolean, default=True)

    user = db.relationship("User", lazy="joined")


class PayrollRun(db.Model):
    __tablename__ = "payroll_runs"

    id = db.Column(db.Integer, primary_key=True)

    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)

    # monthly / fortnightly
    pay_frequency = db.Column(db.String(20), nullable=False, default="monthly", index=True)

    total_gross = db.Column(db.Numeric(12, 2), default=0)
    total_net = db.Column(db.Numeric(12, 2), default=0)

    status = db.Column(db.String(20), default="draft")  # draft / paid

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    paid_at = db.Column(db.DateTime(timezone=True), nullable=True)
    paid_by_admin_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    paid_by = db.relationship("User", foreign_keys=[paid_by_admin_id])


class PayrollItem(db.Model):
    __tablename__ = "payroll_items"

    id = db.Column(db.Integer, primary_key=True)

    payroll_run_id = db.Column(db.Integer, db.ForeignKey("payroll_runs.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    gross_pay = db.Column(db.Numeric(12, 2), default=0)
    deductions = db.Column(db.Numeric(12, 2), default=0)
    net_pay = db.Column(db.Numeric(12, 2), default=0)

    payroll_run = db.relationship("PayrollRun", backref="items")
    user = db.relationship("User")
    allowance = db.Column(db.Numeric(12, 2), default=0)
    overtime = db.Column(db.Numeric(12, 2), default=0)
    bonus = db.Column(db.Numeric(12, 2), default=0)
    nis = db.Column(db.Numeric(12, 2), default=0)
    tax = db.Column(db.Numeric(12, 2), default=0)
    nht = db.Column(db.Numeric(12, 2), default=0)
    education_tax = db.Column(db.Numeric(12, 2), default=0)
    other_deductions = db.Column(db.Numeric(12, 2), default=0)
    pay_advance = db.Column(db.Numeric(12, 2), default=0)

class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)

    module = db.Column(db.String(50), nullable=False, index=True)
    action = db.Column(db.String(100), nullable=False, index=True)
    reason = db.Column(db.String(100), nullable=True, index=True)
    entity_type = db.Column(db.String(50), nullable=True)
    entity_id = db.Column(db.Integer, nullable=True)

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True
    )

    admin_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=True,
        index=True
    )

    description = db.Column(db.Text, nullable=True)

    old_value = db.Column(db.Text, nullable=True)
    new_value = db.Column(db.Text, nullable=True)

    created_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True
    )

    user = db.relationship(
        "User",
        foreign_keys=[user_id]
    )

    admin = db.relationship(
        "User",
        foreign_keys=[admin_id]
    )

    def __repr__(self):
        return f"<AuditLog {self.module}:{self.action}>"