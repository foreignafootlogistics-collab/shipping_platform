from datetime import datetime
from math import ceil
from app.utils.google_maps import get_driving_distance_km

from app.models import Settings


# ---------------------------------------------------
# Helpers
# ---------------------------------------------------

def normalize_day_list(value):
    if not value:
        return []

    return [
        x.strip().lower()
        for x in value.split(",")
        if x.strip()
    ]


def is_free_delivery_day(parish, date_obj, settings):
    """
    Free home delivery is available only for Kingston
    on configured Kingston free-delivery days.

    St. Catherine, Portmore and Spanish Town never qualify
    for free home delivery because customer pickup is available
    from the Gregory Park location.
    """

    parish_norm = (
        (parish or "")
        .strip()
        .lower()
    )

    weekday = (
        date_obj.strftime("%A")
        .strip()
        .lower()
    )

    kingston_days = normalize_day_list(
        getattr(settings, "kingston_free_delivery_days", "")
    )

    if parish_norm == "kingston":
        return weekday in kingston_days

    return False

# ---------------------------------------------------
# Branch Selection
# ---------------------------------------------------

def determine_delivery_branch(parish, settings):

    if (parish or "").strip().lower() == "kingston":
        return (
            settings.kingston_delivery_branch_name
            or "Kingston Dispatch"
        )

    return (
        settings.stc_delivery_branch_name
        or "Gregory Park Branch"
    )


# ---------------------------------------------------
# Delivery Fee Calculation
# ---------------------------------------------------

def calculate_delivery_fee(
    distance_km,
    parish,
    scheduled_date,
    settings
):

    if not scheduled_date:
        return 0, True, "free_route"

    if isinstance(scheduled_date, str):
        scheduled_date = datetime.strptime(
            scheduled_date,
            "%Y-%m-%d"
        ).date()

    # ------------------------------------------------
    # FREE DELIVERY DAYS
    # ------------------------------------------------
    free_day = is_free_delivery_day(
        parish,
        scheduled_date,
        settings
    )

    FREE_RADIUS_KM = 5

    if free_day and distance_km <= FREE_RADIUS_KM:
        return 0, True, "free_route"

    # --------------------------------------------
    # DISTANCE BAND PRICING
    # --------------------------------------------

    parish_norm = (parish or "").strip().lower()

    if distance_km <= 5:
        if parish_norm in (
            "st. catherine",
            "st catherine",
            "portmore",
            "spanish town",
        ):
            return 800, False, "express"

        return 1000, False, "express"

    if distance_km <= 10:
        return 1500, False, "express"

    if distance_km <= 15:
        return 2500, False, "extended"

    if distance_km <= 20:
        return 3500, False, "extended"

    # Over 20 KM
    return 0, False, "admin_review"


# ---------------------------------------------------
# Main Delivery Engine
# ---------------------------------------------------

def build_delivery_details(
    parish,
    distance_km,
    scheduled_date,
    settings
):

    fee, is_free, delivery_type = (
        calculate_delivery_fee(
            distance_km=distance_km,
            parish=parish,
            scheduled_date=scheduled_date,
            settings=settings
        )
    )

    branch = determine_delivery_branch(
        parish,
        settings
    )

    max_distance = float(
        settings.max_delivery_distance_km or 35
    )

    allowed = distance_km <= max_distance

    return {
        "delivery_fee": fee,
        "is_free_delivery": is_free,
        "delivery_type": delivery_type,
        "delivery_branch": branch,
        "allowed": allowed,
        "max_distance": max_distance
    }

def get_dispatch_origin(parish, settings):
    if (parish or "").strip().lower() == "kingston":
        return settings.kingston_dispatch_address

    return settings.stc_dispatch_address


def calculate_real_distance(parish, destination_address, settings):
    api_key = settings.google_maps_api_key

    if not api_key:
        return {
            "success": False,
            "error": "Google Maps API key missing"
        }

    origin = get_dispatch_origin(parish, settings)

    return get_driving_distance_km(
        origin=origin,
        destination=destination_address,
        api_key=api_key
    )