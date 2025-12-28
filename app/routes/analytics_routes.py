# app/routes/analytics_routes.py

from datetime import date, datetime, timedelta
import sqlalchemy as sa
from sqlalchemy import func, cast, String
from collections import defaultdict
from sqlalchemy.orm import aliased

from flask import Blueprint, render_template, request
from flask_login import login_required

from app.extensions import db
from app.models import User, Package, Invoice, Prealert, ScheduledDelivery, ShipmentLog, shipment_packages
from app.routes.admin_auth_routes import admin_required

analytics_bp = Blueprint("analytics", __name__, url_prefix="/analytics")


def _coalesced_user_dt():
    """
    Helper: best-effort datetime for user registration:
    prefer date_registered, then created_at.
    """
    return func.coalesce(
        sa.cast(User.date_registered, sa.DateTime()),
        sa.cast(User.created_at, sa.DateTime())
    )


def _coalesced_package_dt():
    """
    Helper: best-effort datetime for package creation:
    prefer date_received, then created_at.
    """
    return func.coalesce(
        sa.cast(Package.date_received, sa.DateTime()),
        sa.cast(Package.created_at, sa.DateTime())
    )
def _to_date(value):
    """Best-effort convert various stored values to a Python date."""
    if value is None:
        return None

    # Already a date
    if isinstance(value, date) and not isinstance(value, datetime):
        return value

    # Datetime -> date
    if isinstance(value, datetime):
        return value.date()

    # Excel serial numbers (just in case)
    if isinstance(value, (int, float)):
        try:
            base = date(1899, 12, 30)  # Excel serial 1 = 1899-12-31
            return base + timedelta(days=int(value))
        except Exception:
            return None

    # Strings in various formats
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

        for fmt in ("%Y-%m-%d",
                    "%Y-%m-%d %H:%M:%S",
                    "%d/%m/%Y",
                    "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
        # last resort: ISO parser
        try:
            return datetime.fromisoformat(value).date()
        except Exception:
            return None

    return None

@analytics_bp.route("/daily-stats")
@admin_required
def daily_stats():
    today = date.today()
    start_7 = today - timedelta(days=6)        # window for last 7 days
    month_start = today.replace(day=1)         # first day of current month

    # ---------- USERS ----------
    users = User.query.with_entities(
        User.id, User.date_registered, User.created_at
    ).all()

    user_by_day = defaultdict(int)
    today_new_users = 0
    month_new_users = 0

    for _, dr, ca in users:
        d = _to_date(dr) or _to_date(ca)
        if not d:
            continue

        if d == today:
            today_new_users += 1
        if month_start <= d <= today:
            month_new_users += 1
        if start_7 <= d <= today:
            user_by_day[d] += 1

    # ---------- PACKAGES ----------
    packages = Package.query.with_entities(
        Package.id, Package.date_received, Package.created_at
    ).all()

    pkg_by_day = defaultdict(int)
    today_new_packages = 0
    month_new_packages = 0

    for _, dr, ca in packages:
        d = _to_date(dr) or _to_date(ca)
        if not d:
            continue

        if d == today:
            today_new_packages += 1
        if month_start <= d <= today:
            month_new_packages += 1
        if start_7 <= d <= today:
            pkg_by_day[d] += 1

    # ---------- PRE-ALERTS ----------
    prealerts = Prealert.query.with_entities(
        Prealert.id, Prealert.created_at
    ).all()

    pa_by_day = defaultdict(int)
    today_prealerts = 0
    month_prealerts = 0

    for _, ca in prealerts:
        d = _to_date(ca)
        if not d:
            continue

        if d == today:
            today_prealerts += 1
        if month_start <= d <= today:
            month_prealerts += 1
        if start_7 <= d <= today:
            pa_by_day[d] += 1

    # ---------- DELIVERIES ----------
    deliveries_q = ScheduledDelivery.query.with_entities(
        ScheduledDelivery.id,
        ScheduledDelivery.scheduled_date
    ).all()

    deliv_by_day = defaultdict(int)
    today_deliveries = 0
    month_deliveries = 0

    for _, sd in deliveries_q:
        d = _to_date(sd)
        if not d:
            continue

        if d == today:
            today_deliveries += 1
        if month_start <= d <= today:
            month_deliveries += 1
        if start_7 <= d <= today:
            deliv_by_day[d] += 1

    # ---------- LAST 7 DAYS TABLE ----------

    last7_stats = []
    # oldest -> newest (6 days ago up to today)
    for offset in range(6, -1, -1):
        d = today - timedelta(days=offset)
        last7_stats.append({
            "date": d.isoformat(),
            "users":      user_by_day.get(d, 0),
            "packages":   pkg_by_day.get(d, 0),
            "prealerts":  pa_by_day.get(d, 0),
            "deliveries": deliv_by_day.get(d, 0),
        })

    # placeholders for now (you can hook real finance data later)
    net_today = 0.0
    net_month = 0.0

    return render_template(
        "admin/analytics/daily_stats.html",

        today=today,
        today_str=today.isoformat(),

        today_new_users=today_new_users,
        today_new_packages=today_new_packages,
        today_prealerts=today_prealerts,
        today_deliveries=today_deliveries,

        month_new_users=month_new_users,
        month_new_packages=month_new_packages,
        month_prealerts=month_prealerts,
        month_deliveries=month_deliveries,

        # 👇 name matches the template now
        last7_stats=last7_stats,

        net_today=net_today,
        net_month=net_month,
    )

# ---------------------------------------------
# CUSTOMER RETENTION OVERVIEW
# ---------------------------------------------
@analytics_bp.route('/customer/retention')
@admin_required
def customer_retention():
    today = date.today()
    d60 = today - timedelta(days=60)
    d120 = today - timedelta(days=120)
    d30 = today - timedelta(days=30)

    # Common "activity date" expression: package date as DATE
    activity_date = func.date(
        func.coalesce(Package.date_received, Package.created_at)
    )

    # === Subqueries with last activity per user ======================

    # Users active in last 60 days
    active_subq = (
        db.session.query(
            Package.user_id.label("user_id"),
            func.max(activity_date).label("last_activity"),
        )
        .filter(activity_date >= d60)
        .group_by(Package.user_id)
        .subquery()
    )

    # Users active 60–120 days ago (but not caring yet if they are still active)
    prev_subq = (
        db.session.query(
            Package.user_id.label("user_id"),
            func.max(activity_date).label("last_activity"),
        )
        .filter(activity_date >= d120, activity_date < d60)
        .group_by(Package.user_id)
        .subquery()
    )

    # --- Active users (last 60 days packages)
    active_users = db.session.query(active_subq.c.user_id).count()

    # --- Previously active users (60–120 days ago)
    prev_active_users = db.session.query(prev_subq.c.user_id).count()

    # --- Returning users: in BOTH windows (present in both subqueries)
    returning_users = (
        db.session.query(active_subq.c.user_id)
        .join(prev_subq, prev_subq.c.user_id == active_subq.c.user_id)
        .count()
    )

    # --- New users (registered last 30 days)
    new_users = (
        db.session.query(User)
        .filter(
            func.date(
                func.coalesce(User.date_registered, User.created_at)
            ) >= d30
        )
        .count()
    )

    # --- Churned users (active 60–120 days ago, but NOT active in last 60)
    churned_users = max(prev_active_users - returning_users, 0)

    # --- Compute retention rates ---
    retention_rate = (
        round((returning_users / prev_active_users) * 100, 2)
        if prev_active_users
        else 0
    )
    churn_rate = (
        round((churned_users / prev_active_users) * 100, 2)
        if prev_active_users
        else 0
    )

    # ---------- Sample lists for small tables ----------

    # Returning customers (in both windows)
    returning_rows = (
        db.session.query(
            User.full_name,
            User.registration_number,
            active_subq.c.last_activity,
        )
        .join(active_subq, active_subq.c.user_id == User.id)
        .join(prev_subq, prev_subq.c.user_id == User.id)
        .order_by(active_subq.c.last_activity.desc())
        .limit(10)
        .all()
    )

    # Churned customers: in prev_subq but NOT in active_subq
    churned_rows = (
        db.session.query(
            User.full_name,
            User.registration_number,
            prev_subq.c.last_activity,
        )
        .join(prev_subq, prev_subq.c.user_id == User.id)
        .outerjoin(active_subq, active_subq.c.user_id == User.id)
        .filter(active_subq.c.user_id.is_(None))
        .order_by(prev_subq.c.last_activity.desc())
        .limit(10)
        .all()
    )

    def _row_to_dict(name, reg, last_dt):
        if isinstance(last_dt, (datetime, date)):
            last_str = last_dt.strftime("%Y-%m-%d")
        else:
            last_str = str(last_dt) if last_dt else ""
        return {
            "full_name": name or "—",
            "registration_number": reg or "",
            "last_activity": last_str,
        }

    returning_customers = [
        _row_to_dict(name, reg, last_dt)
        for (name, reg, last_dt) in returning_rows
    ]
    churned_customers = [
        _row_to_dict(name, reg, last_dt)
        for (name, reg, last_dt) in churned_rows
    ]

    return render_template(
        "admin/analytics/customer_retention.html",
        today=today,
        active_users=active_users,
        prev_active_users=prev_active_users,
        returning_users=returning_users,
        churned_users=churned_users,
        new_users=new_users,
        retention_rate=retention_rate,
        churn_rate=churn_rate,
        returning_customers=returning_customers,
        churned_customers=churned_customers,
    )

# ---------------------------
# Package Breakdown
# ---------------------------
@analytics_bp.route("/package-breakdown")
@admin_required
def package_breakdown():
    from datetime import date, datetime, timedelta
    from sqlalchemy import func
    import sqlalchemy as sa

    today = date.today()
    default_start = today - timedelta(days=29)

    # --- Read filters from query string, with sensible defaults ---
    start_str = (request.args.get("start") or default_start.isoformat()).strip()
    end_str   = (request.args.get("end")   or today.isoformat()).strip()

    # Parse dates safely
    try:
        start_date = datetime.fromisoformat(start_str).date()
    except Exception:
        start_date = default_start
        start_str = start_date.isoformat()

    try:
        end_date = datetime.fromisoformat(end_str).date()
    except Exception:
        end_date = today
        end_str = end_date.isoformat()

    # Ensure start <= end
    if start_date > end_date:
        start_date, end_date = end_date, start_date
        start_str, end_str = start_date.isoformat(), end_date.isoformat()

    # We'll treat the filter as [start_date, end_date + 1)
    end_plus_one = end_date + timedelta(days=1)

    # -------------------------------------------------------
    # Base query + coalesced date for Package
    # Prefer date_received, fall back to created_at
    # -------------------------------------------------------
    pkg_date = func.coalesce(
        sa.cast(Package.date_received, sa.Date()),
        sa.cast(Package.created_at,    sa.Date()),
    ).label("pkg_date")

    base_q = (
        db.session.query(
            Package,
            User.registration_number,
            User.full_name,
            pkg_date
        )
        .join(User, Package.user_id == User.id)
        .filter(pkg_date >= start_date, pkg_date < end_plus_one)
    )

    # -------------------------------------------------------
    # 1) Overall stats: total packages + total weight
    # -------------------------------------------------------
    total_stats = (
        db.session.query(
            func.count(Package.id),
            func.coalesce(func.sum(Package.weight), 0.0)
        )
        .join(User, Package.user_id == User.id)
        .filter(pkg_date >= start_date, pkg_date < end_plus_one)
        .first()
    )
    total_packages = int(total_stats[0] or 0)
    total_weight   = float(total_stats[1] or 0.0)

    # -------------------------------------------------------
    # 2) Status breakdown (count + weight per status)
    # -------------------------------------------------------
    status_rows = (
        db.session.query(
            Package.status,
            func.count(Package.id).label("cnt"),
            func.coalesce(func.sum(Package.weight), 0.0).label("total_weight")
        )
        .join(User, Package.user_id == User.id)
        .filter(pkg_date >= start_date, pkg_date < end_plus_one)
        .group_by(Package.status)
        .order_by(Package.status)
        .all()
    )

    status_breakdown = []
    for st, cnt, tw in status_rows:
        status_breakdown.append({
            "status": st or "Unknown",
            "count": int(cnt or 0),
            "total_weight": float(tw or 0.0),
        })

    # -------------------------------------------------------
    # 3) 🔹 Top Customers (by package count in this range)
    # -------------------------------------------------------
    top_cust_rows = (
        db.session.query(
            User.registration_number,
            User.full_name,
            func.count(Package.id).label("pkg_count"),
            func.coalesce(func.sum(Package.weight), 0.0).label("total_weight")
        )
        .join(Package, Package.user_id == User.id)
        .filter(pkg_date >= start_date, pkg_date < end_plus_one)
        .group_by(User.id, User.registration_number, User.full_name)
        .order_by(func.count(Package.id).desc())
        .limit(10)
        .all()
    )

    top_customers = []
    for reg, full_name, cnt, tw in top_cust_rows:
        top_customers.append({
            "registration_number": reg,
            "full_name": full_name or "—",
            "pkg_count": int(cnt or 0),
            "total_weight": float(tw or 0.0),
        })

    # -------------------------------------------------------
    # 4) 🔹 Daily Weight (total weight by day in this range)
    # -------------------------------------------------------
    daily_rows = (
        db.session.query(
            pkg_date,
            func.count(Package.id).label("pkg_count"),
            func.coalesce(func.sum(Package.weight), 0.0).label("total_weight")
        )
        .join(User, Package.user_id == User.id)
        .filter(pkg_date >= start_date, pkg_date < end_plus_one)
        .group_by(pkg_date)
        .order_by(pkg_date.asc())
        .all()
    )

    daily_weight = []
    for d, cnt, tw in daily_rows:
        daily_weight.append({
            "day": d.isoformat() if isinstance(d, date) else str(d),
            "pkg_count": int(cnt or 0),
            "total_weight": float(tw or 0.0),
        })

    # Build some labels for the page
    date_range_label = f"{start_date.isoformat()} → {end_date.isoformat()}"

    return render_template(
        "admin/analytics/package_breakdown.html",
        start=start_str,
        end=end_str,
        date_range_label=date_range_label,

        total_packages=total_packages,
        total_weight=total_weight,
        status_breakdown=status_breakdown,

        # 🔹 NEW:
        top_customers=top_customers,
        daily_weight=daily_weight,
    )

@analytics_bp.route("/shipment-performance")
@admin_required
def shipment_performance():
    from datetime import date, datetime, timedelta
    from sqlalchemy import func

    today = date.today()
    default_start = today - timedelta(days=30)

    # ---- Read date filters from query string (or default last 30 days) ----
    start_str = request.args.get("start", default_start.isoformat())
    end_str   = request.args.get("end", today.isoformat())

    try:
        start_date = datetime.fromisoformat(start_str).date()
    except Exception:
        start_date = default_start
        start_str = default_start.isoformat()

    try:
        end_date = datetime.fromisoformat(end_str).date()
    except Exception:
        end_date = today
        end_str = today.isoformat()

    # ---- Base query: shipments in this date range (by created_at) ----
    base_q = (
        db.session.query(ShipmentLog)
        .filter(
            func.date(ShipmentLog.created_at) >= start_date,
            func.date(ShipmentLog.created_at) <= end_date,
        )
    )

    total_shipments = base_q.count()

    # ---- Total packages in those shipments ----
    pkg_count_q = (
        db.session.query(func.count(Package.id))
        .join(shipment_packages, shipment_packages.c.package_id == Package.id)
        .join(ShipmentLog, ShipmentLog.id == shipment_packages.c.shipment_id)
        .filter(
            func.date(ShipmentLog.created_at) >= start_date,
            func.date(ShipmentLog.created_at) <= end_date,
        )
    )
    total_packages = pkg_count_q.scalar() or 0

    avg_packages_per_shipment = (
        total_packages / total_shipments if total_shipments else 0
    )

    # ---- Total & average shipment weight ----
    weight_q = (
        db.session.query(func.coalesce(func.sum(Package.weight), 0.0))
        .join(shipment_packages, shipment_packages.c.package_id == Package.id)
        .join(ShipmentLog, ShipmentLog.id == shipment_packages.c.shipment_id)
        .filter(
            func.date(ShipmentLog.created_at) >= start_date,
            func.date(ShipmentLog.created_at) <= end_date,
        )
    )
    total_weight = float(weight_q.scalar() or 0.0)
    avg_weight_per_shipment = (
        total_weight / total_shipments if total_shipments else 0.0
    )

    # ---- Daily shipment counts for a small chart/table ----
    day_col = func.date(ShipmentLog.created_at).label("day")
    per_day_rows = (
        db.session.query(day_col, func.count(ShipmentLog.id))
        .filter(
            func.date(ShipmentLog.created_at) >= start_date,
            func.date(ShipmentLog.created_at) <= end_date,
        )
        .group_by(day_col)
        .order_by(day_col)
    )

    daily_shipments = [
        {
            "day": d.isoformat() if hasattr(d, "isoformat") else str(d),
            "count": int(c or 0),
        }
        for d, c in per_day_rows
    ]

    # ---- Shipments by status (if ShipmentLog has a status column) ----
    shipments_by_status = []
    if hasattr(ShipmentLog, "status"):
        status_rows = (
            db.session.query(ShipmentLog.status, func.count(ShipmentLog.id))
            .filter(
                func.date(ShipmentLog.created_at) >= start_date,
                func.date(ShipmentLog.created_at) <= end_date,
            )
            .group_by(ShipmentLog.status)
        )
        for status, cnt in status_rows:
            shipments_by_status.append(
                {
                    "status": status or "Unknown",
                    "count": int(cnt or 0),
                }
            )

    return render_template(
        "admin/analytics/shipment_performance.html",
        start=start_str,
        end=end_str,
        start_date=start_date,
        end_date=end_date,
        total_shipments=total_shipments,
        total_packages=total_packages,
        avg_packages_per_shipment=avg_packages_per_shipment,
        total_weight=total_weight,
        avg_weight_per_shipment=avg_weight_per_shipment,
        daily_shipments=daily_shipments,
        shipments_by_status=shipments_by_status,
    )


@analytics_bp.route("/customer/segments")
@admin_required
def customer_segments():
    """
    Customer segmentation based on package activity in the last 90 days.
    """
    from collections import defaultdict

    today = date.today()
    d90 = today - timedelta(days=90)

    # ---- 1. Get all users (for total + per-user info) ----
    users = db.session.query(
        User.id,
        User.full_name,
        User.registration_number
    ).all()

    # ---- 2. Count shipments per user (last 90 days) ----
    pkg_rows = db.session.query(
        Package.user_id,
        Package.date_received,
        Package.created_at
    ).all()

    shipments_90_by_user = defaultdict(int)

    for user_id, date_received, created_at in pkg_rows:
        d = _to_date(date_received) or _to_date(created_at)
        if not d:
            continue
        if d90 <= d <= today:
            shipments_90_by_user[user_id] += 1

    # ---- 3. Segment counts + sample customers per segment ----
    total_customers = len(users)
    active_customers_90 = 0
    total_shipments_active_90 = 0

    count_none = 0
    count_occasional = 0
    count_regular = 0
    count_power = 0

    customers_occasional = []
    customers_regular = []
    customers_power = []

    for user_id, full_name, reg_no in users:
        n = shipments_90_by_user.get(user_id, 0)

        if n > 0:
            active_customers_90 += 1
            total_shipments_active_90 += n

        if n == 0:
            count_none += 1
        elif 1 <= n <= 2:
            count_occasional += 1
            if len(customers_occasional) < 10:
                customers_occasional.append({
                    "full_name": full_name or "—",
                    "registration_number": reg_no or "",
                    "shipments_90": n,
                })
        elif 3 <= n <= 5:
            count_regular += 1
            if len(customers_regular) < 10:
                customers_regular.append({
                    "full_name": full_name or "—",
                    "registration_number": reg_no or "",
                    "shipments_90": n,
                })
        else:  # n >= 6
            count_power += 1
            if len(customers_power) < 10:
                customers_power.append({
                    "full_name": full_name or "—",
                    "registration_number": reg_no or "",
                    "shipments_90": n,
                })

    inactive_customers_90 = total_customers - active_customers_90

    avg_shipments_per_active = (
        round(total_shipments_active_90 / active_customers_90, 1)
        if active_customers_90
        else 0.0
    )

    # ---- 4. Segment stats for chart + table ----
    segment_labels = [
        "No Shipments (0)",
        "Occasional (1–2)",
        "Regular (3–5)",
        "Power Users (6+)",
    ]
    segment_counts = [
        count_none,
        count_occasional,
        count_regular,
        count_power,
    ]

    total_segment = sum(segment_counts) or 1  # avoid zero-div
    segment_stats = []
    for label, cnt in zip(segment_labels, segment_counts):
        pct = round((cnt / total_segment) * 100, 1) if total_segment else 0
        segment_stats.append({
            "label": label,
            "count": cnt,
            "percent": pct,
        })

    return render_template(
        "admin/analytics/customer_segments.html",
        today=today,
        d90=d90,

        total_customers=total_customers,
        active_customers_90=active_customers_90,
        inactive_customers_90=inactive_customers_90,
        avg_shipments_per_active=avg_shipments_per_active,

        segment_labels=segment_labels,
        segment_counts=segment_counts,
        segment_stats=segment_stats,

        customers_occasional=customers_occasional,
        customers_regular=customers_regular,
        customers_power=customers_power,
    )

# ==============================
# Referral Leaderboard
# ==============================
@analytics_bp.route("/referrals")
@admin_required
def referral_leaderboard():
    """Show top referrers based on how many users they brought in."""
    from datetime import date, datetime

    start_str = request.args.get("start", "")
    end_str   = request.args.get("end", "")

    # default to last 30 days if nothing chosen
    today = date.today()
    default_start = today - timedelta(days=30)

    try:
        start_date = datetime.fromisoformat(start_str).date() if start_str else default_start
    except ValueError:
        start_date = default_start

    try:
        end_date = datetime.fromisoformat(end_str).date() if end_str else today
    except ValueError:
        end_date = today

    # We'll treat User as both 'referrer' and 'referred'
    child = aliased(User)

    # Aggregate: how many users each referrer has
    rows = (
        db.session.query(
            User.id.label("id"),
            User.full_name.label("full_name"),
            User.registration_number.label("reg"),
            User.referral_code.label("referral_code"),
            func.count(child.id).label("ref_count"),
            func.min(child.date_registered).label("first_ref"),
            func.max(child.date_registered).label("last_ref"),
        )
        .outerjoin(child, child.referrer_id == User.id)
        .group_by(
            User.id,
            User.full_name,
            User.registration_number,
            User.referral_code,
        )
        .having(func.count(child.id) > 0)        # only show people who actually referred
        .order_by(sa.desc("ref_count"), User.full_name.asc())
        .all()
    )

    # helper to turn date_registered (stored as string) into a date
    def _safe_date(val):
        if not val:
            return None
        if isinstance(val, date):
            return val
        if isinstance(val, datetime):
            return val.date()
        try:
            return datetime.fromisoformat(str(val)).date()
        except Exception:
            return None

    leaderboard = []
    total_referred = 0

    for r in rows:
        count = int(r.ref_count or 0)
        total_referred += count

        leaderboard.append(
            {
                "id": r.id,
                "name": r.full_name,
                "reg": r.reg,
                "code": r.referral_code,
                "ref_count": count,
                "first_ref": _safe_date(r.first_ref),
                "last_ref": _safe_date(r.last_ref),
            }
        )

    return render_template(
        "admin/analytics/referral_leaderboard.html",
        start=start_str,
        end=end_str,
        start_date=start_date,
        end_date=end_date,
        leaderboard=leaderboard,
        total_referrers=len(leaderboard),
        total_referred=total_referred,
        today=today,
    )

