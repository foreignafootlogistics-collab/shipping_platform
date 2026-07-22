from datetime import datetime
import math

from app.extensions import db
from app.models import (
    Subscription,
    SubscriptionUsage,
    SubscriptionMember,
)


def utc_now():
    """
    Subscription.start_date and Subscription.end_date use timezone-naive
    DateTime columns, so consistently use naive UTC for those fields.
    """
    return datetime.utcnow()


def get_billable_weight(package):
    """
    Subscription usage is based on rounded-up billable weight.

    Examples:
    1.0 lb -> 1 lb
    1.2 lb -> 2 lb
    """
    try:
        weight = float(package.weight or 0)
    except (TypeError, ValueError):
        weight = 0

    return max(1, math.ceil(weight))


def sync_expired_subscriptions(commit=False):
    """
    Mark ended active/exhausted subscriptions as expired.

    Set commit=True for scheduled/admin maintenance routes.
    Ordinary page requests can use the default flush behaviour.
    """
    now = utc_now()

    expired_subscriptions = (
        Subscription.query
        .filter(
            Subscription.status.in_(["active", "exhausted"]),
            Subscription.end_date.isnot(None),
            Subscription.end_date < now,
        )
        .all()
    )

    for subscription in expired_subscriptions:
        subscription.status = "expired"

    if expired_subscriptions:
        if commit:
            db.session.commit()
        else:
            db.session.flush()

    return len(expired_subscriptions)


def get_active_subscription(user_id):
    """
    Return the current subscription for either:

    - The subscription owner
    - An active Family-plan member

    Exhausted subscriptions are returned while still inside their active
    date range because they may provide an overage discount.
    """
    if not user_id:
        return None

    now = utc_now()
    valid_statuses = ["active", "exhausted"]

    # Check subscriptions owned directly by this user.
    subscription = (
        Subscription.query
        .filter(
            Subscription.user_id == user_id,
            Subscription.status.in_(valid_statuses),
            Subscription.start_date.isnot(None),
            Subscription.end_date.isnot(None),
            Subscription.start_date <= now,
            Subscription.end_date >= now,
        )
        .order_by(
            Subscription.start_date.desc(),
            Subscription.created_at.desc(),
            Subscription.id.desc(),
        )
        .first()
    )

    if subscription:
        return subscription

    # Check active Family-plan membership. Joining Subscription prevents an
    # old membership from being chosen ahead of a current membership.
    subscription = (
        Subscription.query
        .join(
            SubscriptionMember,
            SubscriptionMember.subscription_id == Subscription.id,
        )
        .filter(
            SubscriptionMember.user_id == user_id,
            SubscriptionMember.status == "active",
            Subscription.status.in_(valid_statuses),
            Subscription.start_date.isnot(None),
            Subscription.end_date.isnot(None),
            Subscription.start_date <= now,
            Subscription.end_date >= now,
        )
        .order_by(
            Subscription.start_date.desc(),
            Subscription.created_at.desc(),
            Subscription.id.desc(),
        )
        .first()
    )

    return subscription


def ensure_usage(subscription):
    """
    Return the subscription usage record, creating it when missing.
    """
    if subscription.usage:
        return subscription.usage

    usage = SubscriptionUsage(
        subscription_id=subscription.id,
        packages_used=0,
        weight_used=0.0,
    )

    db.session.add(usage)
    db.session.flush()

    return usage


def subscription_is_exhausted(subscription):
    """
    A subscription is exhausted when either its package allowance or its
    total billable-weight allowance has been used.
    """
    if not subscription or not subscription.plan:
        return True

    usage = ensure_usage(subscription)
    plan = subscription.plan

    packages_used = int(usage.packages_used or 0)
    weight_used = float(usage.weight_used or 0)

    package_limit = int(plan.package_limit or 0)
    weight_limit = float(plan.weight_limit or 0)

    package_exhausted = (
        package_limit > 0 and packages_used >= package_limit
    )

    weight_exhausted = (
        weight_limit > 0 and weight_used >= weight_limit
    )

    # A plan with neither allowance configured should not provide coverage.
    no_allowance_configured = (
        package_limit <= 0 and weight_limit <= 0
    )

    return (
        package_exhausted
        or weight_exhausted
        or no_allowance_configured
    )


def package_qualifies_for_subscription(subscription, package):
    """
    Check whether a package can be fully covered by the subscription.

    This checks:
    - Subscription status
    - Per-package weight
    - Remaining package allowance
    - Remaining total-weight allowance
    """
    if not subscription or subscription.status != "active":
        return False

    if not subscription.plan:
        return False

    usage = ensure_usage(subscription)
    plan = subscription.plan

    if subscription_is_exhausted(subscription):
        return False

    billable_weight = get_billable_weight(package)

    max_weight_per_package = float(
        plan.max_weight_per_package or 0
    )

    if (
        max_weight_per_package <= 0
        or billable_weight > max_weight_per_package
    ):
        return False

    package_limit = int(plan.package_limit or 0)
    weight_limit = float(plan.weight_limit or 0)

    packages_used = int(usage.packages_used or 0)
    weight_used = float(usage.weight_used or 0)

    if package_limit > 0 and packages_used + 1 > package_limit:
        return False

    if weight_limit > 0 and weight_used + billable_weight > weight_limit:
        return False

    return True


def apply_subscription_usage(package):
    """
    Count a package against the applicable subscription.

    Returns:
    - subscription_applied
    - already_applied
    - no_subscription
    - package_over_plan_limit
    - subscription_allowance_exceeded
    - subscription_exhausted
    """
    if (
        getattr(package, "subscription_applied", False)
        and getattr(package, "subscription_id", None)
    ):
        if getattr(
            package,
            "subscription_billable_weight",
            None,
        ) is None:
            package.subscription_billable_weight = (
                get_billable_weight(package)
            )
            db.session.flush()

        return "already_applied"

    if not getattr(package, "user_id", None):
        return "no_subscription"

    subscription = get_active_subscription(package.user_id)

    if not subscription:
        return "no_subscription"

    if not subscription.plan:
        return "no_subscription"

    if subscription.status == "exhausted":
        return "subscription_exhausted"

    usage = ensure_usage(subscription)
    plan = subscription.plan

    if subscription_is_exhausted(subscription):
        subscription.status = "exhausted"
        db.session.flush()
        return "subscription_exhausted"

    billable_weight = get_billable_weight(package)

    max_weight_per_package = float(
        plan.max_weight_per_package or 0
    )

    if (
        max_weight_per_package <= 0
        or billable_weight > max_weight_per_package
    ):
        return "package_over_plan_limit"

    packages_used = int(usage.packages_used or 0)
    weight_used = float(usage.weight_used or 0)

    package_limit = int(plan.package_limit or 0)
    weight_limit = float(plan.weight_limit or 0)

    # Do not partly cover a package.
    if package_limit > 0 and packages_used + 1 > package_limit:
        subscription.status = "exhausted"
        db.session.flush()
        return "subscription_exhausted"

    # The package cannot use more than the remaining total-weight allowance.
    # Keep the plan active if a smaller future package could still fit.
    if (
        weight_limit > 0
        and weight_used + billable_weight > weight_limit
    ):
        return "subscription_allowance_exceeded"

    usage.packages_used = packages_used + 1
    usage.weight_used = weight_used + billable_weight

    package.subscription_applied = True
    package.subscription_result = "subscription_applied"
    package.subscription_id = subscription.id
    package.subscription_applied_at = utc_now()
    package.subscription_billable_weight = billable_weight

    if subscription_is_exhausted(subscription):
        subscription.status = "exhausted"

    db.session.flush()

    return "subscription_applied"

def clear_package_subscription(package, result=None):
    """
    Remove the subscription connection from a package without changing
    SubscriptionUsage. Use release_subscription_usage() when previously
    deducted usage must also be restored.
    """
    package.subscription_applied = False
    package.subscription_result = result
    package.subscription_id = None
    package.subscription_applied_at = None
    package.subscription_billable_weight = None


def release_subscription_usage(package, result=None):
    """
    Restore subscription allowance previously deducted for a package.

    Use this before:
    - Deleting a covered package
    - Reassigning it to another customer
    - Recalculating coverage after its weight changes

    Returns True when recorded usage was reversed.
    """
    was_applied = bool(
        getattr(package, "subscription_applied", False)
    )

    subscription_id = getattr(
        package,
        "subscription_id",
        None,
    )

    if not was_applied or not subscription_id:
        clear_package_subscription(package, result=result)
        db.session.flush()
        return False

    subscription = db.session.get(
        Subscription,
        subscription_id,
    )

    stored_weight = getattr(
        package,
        "subscription_billable_weight",
        None,
    )

    if stored_weight is None:
        # Compatibility fallback for packages covered before the new
        # subscription_billable_weight column was introduced.
        stored_weight = get_billable_weight(package)

    try:
        stored_weight = float(stored_weight or 0)
    except (TypeError, ValueError):
        stored_weight = 0.0

    if subscription and subscription.usage:
        usage = subscription.usage

        usage.packages_used = max(
            int(usage.packages_used or 0) - 1,
            0,
        )

        usage.weight_used = max(
            float(usage.weight_used or 0)
            - stored_weight,
            0.0,
        )

    clear_package_subscription(
        package,
        result=result,
    )

    # If allowance was restored during the valid subscription period,
    # an exhausted subscription may become active again.
    if (
        subscription
        and subscription.status == "exhausted"
        and subscription.start_date
        and subscription.end_date
    ):
        now = utc_now()

        if (
            subscription.start_date <= now
            <= subscription.end_date
            and not subscription_is_exhausted(subscription)
        ):
            subscription.status = "active"

    db.session.flush()

    return True


def reconcile_subscription_usage(package):
    """
    Re-evaluate subscription coverage after the package's customer or
    weight changes.

    Existing usage is restored first. The package is then evaluated
    against the currently applicable subscription.

    Returns the same result values as apply_subscription_usage().
    """
    had_previous_application = bool(
        getattr(package, "subscription_applied", False)
        and getattr(package, "subscription_id", None)
    )

    if had_previous_application:
        release_subscription_usage(
            package,
            result=None,
        )
    else:
        clear_package_subscription(
            package,
            result=None,
        )
        db.session.flush()

    result = apply_subscription_usage(package)

    if result not in (
        "subscription_applied",
        "already_applied",
    ):
        clear_package_subscription(
            package,
            result=result or "no_subscription",
        )
        db.session.flush()

    return result


def get_subscription_discount_percent(package):
    """
    Return the exhausted-plan overage discount.

    This works for both subscription owners and active Family members.
    The discount remains available only until the subscription end date.
    """
    if not getattr(package, "user_id", None):
        return 0

    subscription = get_active_subscription(package.user_id)

    if not subscription or subscription.status != "exhausted":
        return 0

    if not subscription.plan:
        return 0

    billable_weight = get_billable_weight(package)

    discount_percent = float(
        subscription.plan.overage_discount_percent or 0
    )

    max_discount_weight = float(
        subscription.plan.overage_discount_max_weight or 0
    )

    if discount_percent <= 0:
        return 0

    if max_discount_weight <= 0:
        return 0

    if billable_weight > max_discount_weight:
        return 0

    return discount_percent


def get_subscription_summary(user_id):
    """
    Build the subscription information displayed in the admin and customer
    accounts.
    """
    subscription = get_active_subscription(user_id)

    if not subscription or not subscription.plan:
        return None

    usage = ensure_usage(subscription)
    plan = subscription.plan

    packages_used = int(usage.packages_used or 0)
    weight_used = float(usage.weight_used or 0)

    package_limit = int(plan.package_limit or 0)
    weight_limit = float(plan.weight_limit or 0)

    packages_remaining = max(
        package_limit - packages_used,
        0,
    )

    weight_remaining = max(
        weight_limit - weight_used,
        0,
    )

    package_percent = 0
    if package_limit > 0:
        package_percent = min(
            100,
            round((packages_used / package_limit) * 100),
        )

    weight_percent = 0
    if weight_limit > 0:
        weight_percent = min(
            100,
            round((weight_used / weight_limit) * 100),
        )

    now = utc_now()
    end_date = subscription.end_date

    days_remaining = None
    expires_soon = False
    is_expired = False

    if end_date:
        seconds_remaining = (end_date - now).total_seconds()
        is_expired = seconds_remaining <= 0

        if is_expired:
            days_remaining = 0
        else:
            days_remaining = math.ceil(
                seconds_remaining / 86400
            )

        expires_soon = (
            not is_expired
            and days_remaining <= 5
        )

    members = []

    if bool(plan.is_family_plan):
        members = (
            SubscriptionMember.query
            .filter_by(
                subscription_id=subscription.id,
                status="active",
            )
            .order_by(
                SubscriptionMember.role.desc(),
                SubscriptionMember.added_at.asc(),
            )
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
        "packages_remaining": packages_remaining,
        "package_percent": package_percent,

        "weight_used": weight_used,
        "weight_limit": weight_limit,
        "weight_remaining": weight_remaining,
        "weight_percent": weight_percent,

        "max_weight_per_package": float(
            plan.max_weight_per_package or 0
        ),

        "is_family_plan": bool(plan.is_family_plan),
        "max_users": int(plan.max_users or 1),
        "priority_processing": bool(
            plan.priority_processing
        ),

        "days_remaining": days_remaining,
        "expires_soon": expires_soon,
        "is_expired": is_expired,

        "is_owner": subscription.user_id == user_id,
        "owner_id": subscription.user_id,
        "owner_name": (
            subscription.user.full_name
            if subscription.user
            else None
        ),

        "is_admin_waived": bool(
            getattr(
                subscription,
                "is_admin_waived",
                False,
            )
        ),
        "waiver_reason": (
            getattr(
                subscription,
                "waiver_reason",
                None,
            )
            or ""
        ),
        "waived_by_name": (
            subscription.waived_by_admin.full_name
            if getattr(
                subscription,
                "waived_by_admin",
                None,
            )
            else None
        ),

        "members": members,
    }