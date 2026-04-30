from datetime import datetime
import math

from app.extensions import db
from app.models import Subscription, SubscriptionUsage


def get_billable_weight(package):
    """
    Uses rounded-up weight for subscription usage.
    Example: 1.2 lb -> 2 lb
    """
    weight = float(package.weight or 0)
    return max(1, math.ceil(weight))


def get_active_subscription(user_id):
    now = datetime.utcnow()

    return (
        Subscription.query
        .filter(
            Subscription.user_id == user_id,
            Subscription.status == "active",
            Subscription.start_date <= now,
            Subscription.end_date >= now
        )
        .order_by(Subscription.created_at.desc())
        .first()
    )


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
    - package_over_plan_limit
    - subscription_exhausted
    """

    if not package.user_id:
        return "no_subscription"

    subscription = get_active_subscription(package.user_id)

    if not subscription:
        return "no_subscription"

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

    if subscription_is_exhausted(subscription):
        subscription.status = "exhausted"

    db.session.flush()
    return "subscription_applied"


def get_subscription_discount_percent(package):
    """
    Applies after subscription is exhausted:
    5% discount only for packages <= 10 lb billable weight.
    """

    if not package.user_id:
        return 0

    subscription = (
        Subscription.query
        .filter(
            Subscription.user_id == package.user_id,
            Subscription.status == "exhausted"
        )
        .order_by(Subscription.created_at.desc())
        .first()
    )

    if not subscription:
        return 0

    billable_weight = get_billable_weight(package)

    if billable_weight <= subscription.plan.overage_discount_max_weight:
        return float(subscription.plan.overage_discount_percent or 0)

    return 0

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

    from datetime import datetime, timezone

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

    return {
        "subscription_id": subscription.id,
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
        "is_family_plan": bool(plan.is_family_plan),
        "priority_processing": bool(plan.priority_processing),
        "days_remaining": days_remaining,
        "expires_soon": expires_soon,
        "is_expired": is_expired,
    }