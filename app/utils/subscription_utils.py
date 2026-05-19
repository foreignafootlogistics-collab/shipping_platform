from datetime import datetime, timezone
import math

from app.extensions import db
from app.models import Subscription, SubscriptionUsage, SubscriptionMember


def get_billable_weight(package):
    """
    Uses rounded-up weight for subscription usage.
    Example: 1.2 lb -> 2 lb
    """
    weight = float(package.weight or 0)
    return max(1, math.ceil(weight))


def get_active_subscription(user_id):
    now = datetime.now(timezone.utc)

    # Auto-expire old active/exhausted subscriptions
    expired_subs = (
        Subscription.query
        .filter(
            Subscription.status.in_(["active", "exhausted"]),
            Subscription.end_date.isnot(None),
            Subscription.end_date < now
        )
        .all()
    )

    for s in expired_subs:
        s.status = "expired"

    if expired_subs:
        db.session.flush()

    # First: check if user is owner
    sub = (
        Subscription.query
        .filter(
            Subscription.user_id == user_id,
            Subscription.status.in_(["active", "exhausted"]),
            Subscription.start_date <= now,
            Subscription.end_date >= now
        )
        .order_by(Subscription.created_at.desc())
        .first()
    )

    if sub:
        return sub

    # Second: check if user is family member
    member = (
        SubscriptionMember.query
        .filter(
            SubscriptionMember.user_id == user_id,
            SubscriptionMember.status == "active"
        )
        .first()
    )

    if not member:
        return None

    sub = (
        Subscription.query
        .filter(
            Subscription.id == member.subscription_id,
            Subscription.status.in_(["active", "exhausted"]),
            Subscription.start_date <= now,
            Subscription.end_date >= now
        )
        .first()
    )

    return sub

def ensure_usage(subscription):
    if subscription.usage:
        return subscription.usage

    usage = SubscriptionUsage(subscription_id=subscription.id)
    db.session.add(usage)
    db.session.flush()
    return usage


def subscription_is_exhausted(subscription):
    usage = ensure_usage(subscription)
    plan = subscription.plan

    return (
        usage.packages_used >= plan.package_limit
        or usage.weight_used >= plan.weight_limit
    )


def package_qualifies_for_subscription(subscription, package):
    billable_weight = get_billable_weight(package)
    return billable_weight <= subscription.plan.max_weight_per_package


def apply_subscription_usage(package):
    """
    Call this when a package should count against the customer's subscription.

    Returns:
    - subscription_applied
    - no_subscription
    - already_applied
    - package_over_plan_limit
    - subscription_exhausted
    """

    # ✅ Prevent duplicate counting
    if getattr(package, "subscription_applied", False) and getattr(package, "subscription_id", None):
        return "already_applied"

    if not package.user_id:
        return "no_subscription"

    subscription = get_active_subscription(package.user_id)

    if not subscription:
        return "no_subscription"
    
    if subscription.status == "exhausted":
        return "subscription_exhausted"

    billable_weight = get_billable_weight(package)

    if billable_weight > subscription.plan.max_weight_per_package:
        return "package_over_plan_limit"

    if subscription_is_exhausted(subscription):
        subscription.status = "exhausted"
        db.session.flush()
        return "subscription_exhausted"

    usage = ensure_usage(subscription)

    usage.packages_used += 1
    usage.weight_used += billable_weight

    package.subscription_applied = True
    package.subscription_result = "subscription_applied"
    package.subscription_id = subscription.id
    package.subscription_applied_at = datetime.now(timezone.utc)

    if subscription_is_exhausted(subscription):
        subscription.status = "exhausted"

    db.session.flush()
    return "subscription_applied"


def get_subscription_discount_percent(package):
    """
    Applies after subscription is exhausted.
    Works for both owner and family members.
    """

    if not package.user_id:
        return 0

    subscription = (
        Subscription.query
        .filter(
            Subscription.user_id == package.user_id,
            Subscription.status.in_(["exhausted"])
        )
        .order_by(Subscription.created_at.desc())
        .first()
    )

    if not subscription:
        member = (
            SubscriptionMember.query
            .filter(
                SubscriptionMember.user_id == package.user_id,
                SubscriptionMember.status == "active"
            )
            .first()
        )

        if member:
            subscription = (
                Subscription.query
                .filter(
                    Subscription.id == member.subscription_id,
                    Subscription.status.in_(["exhausted"])
                )
                .first()
            )

    if not subscription:
        return 0
    
    if subscription.end_date:
        now = datetime.now(timezone.utc)

        end_date = subscription.end_date
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        if end_date < now:
            return 0


    billable_weight = get_billable_weight(package)

    # Subscription exhausted but still within active period
    discount_percent = float(subscription.plan.overage_discount_percent or 0)
    max_discount_weight = float(subscription.plan.overage_discount_max_weight or 0)

    # No discount configured
    if discount_percent <= 0:
        return 0

    # No overage limit configured
    if max_discount_weight <= 0:
        return 0

    # Package too heavy for exhausted-plan discount
    if billable_weight > max_discount_weight:
        return 0

    return discount_percent


def get_subscription_summary(user_id):
    subscription = get_active_subscription(user_id)

    if not subscription:
        return None
    
    usage = ensure_usage(subscription)
    plan = subscription.plan

    packages_used = int(usage.packages_used or 0)
    weight_used = float(usage.weight_used or 0)

    package_limit = int(plan.package_limit or 0)
    weight_limit = float(plan.weight_limit or 0)

    package_percent = 0
    weight_percent = 0

    if package_limit > 0:
        package_percent = min(100, round((packages_used / package_limit) * 100))

    if weight_limit > 0:
        weight_percent = min(100, round((weight_used / weight_limit) * 100))

    now = datetime.now(timezone.utc)

    end_date = subscription.end_date
    if end_date and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)

    days_remaining = None
    expires_soon = False
    is_expired = False

    if end_date:
        delta = end_date - now
        days_remaining = max(delta.days, 0)
        is_expired = delta.total_seconds() <= 0
        expires_soon = (not is_expired) and days_remaining <= 5

    members = []
    if bool(getattr(plan, "is_family_plan", False)):
        members = (
            SubscriptionMember.query
            .filter_by(subscription_id=subscription.id, status="active")
            .all()
        )

    return {
        "subscription_id": subscription.id,
        "plan_id": plan.id,
        "plan_name": plan.name,
        "status": subscription.status,
        "start_date": subscription.start_date,
        "end_date": subscription.end_date,

        "packages_used": packages_used,
        "package_limit": package_limit,
        "packages_remaining": max(package_limit - packages_used, 0),
        "package_percent": package_percent,

        "weight_used": weight_used,
        "weight_limit": weight_limit,
        "weight_remaining": max(weight_limit - weight_used, 0),
        "weight_percent": weight_percent,

        "max_weight_per_package": float(plan.max_weight_per_package or 0),
        "is_family_plan": bool(getattr(plan, "is_family_plan", False)),
        "priority_processing": bool(getattr(plan, "priority_processing", False)),

        "days_remaining": days_remaining,
        "expires_soon": expires_soon,
        "is_expired": is_expired,

        "members": members,
    }