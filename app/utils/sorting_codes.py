from datetime import datetime, timezone

from app.models import Package, User


SORT_CODE_LABELS = {
    "THN": "NHT",
    "PRE": "Port Ridge Estate",
    "UE": "Union Estates",
    "GPB": "Gregory Park Branch",
    "RTD": "Round Town Delivery",
    "STH": "Spanish Town Hospital",
    "KED": "Knutsford Express Delivery",
    "UNASSIGNED": "Unassigned",
}

VALID_SORT_CODES = set(SORT_CODE_LABELS)

VALID_SORT_CODE_SOURCES = {
    "customer_default",
    "scheduled_pickup",
    "scheduled_delivery",
    "manual",
    "system",
}


def normalize_sort_code(value):
    code = str(value or "").strip().upper()

    if not code:
        return "UNASSIGNED"

    if code not in VALID_SORT_CODES:
        raise ValueError(
            f"Invalid sorting code: {code}."
        )

    return code


def sort_code_label(value):
    code = normalize_sort_code(value)

    return SORT_CODE_LABELS.get(
        code,
        "Unassigned",
    )


def set_customer_default_sort_code(
    user,
    code,
):
    if not isinstance(user, User):
        raise ValueError(
            "A valid customer is required."
        )

    code = normalize_sort_code(code)
    user.default_sort_code = code

    return code


def set_package_sort_code(
    package,
    code,
    *,
    source="manual",
    admin_id=None,
    lock=None,
    force=False,
):
    """
    Assign an authoritative sorting code to a package.

    Manual assignments are locked by default. Automatic processes
    cannot replace a locked assignment unless force=True.
    """

    if not isinstance(package, Package):
        raise ValueError(
            "A valid package is required."
        )

    code = normalize_sort_code(code)

    source = str(
        source or "system"
    ).strip().lower()

    if source not in VALID_SORT_CODE_SOURCES:
        raise ValueError(
            f"Invalid sorting-code source: {source}."
        )

    if (
        bool(package.sort_code_locked)
        and not force
    ):
        raise ValueError(
            "This package sorting code is locked. "
            "Use a manual override to change it."
        )

    if lock is None:
        lock = source == "manual"

    package.sort_code = code
    package.sort_code_source = source
    package.sort_code_locked = bool(lock)
    package.sort_code_updated_at = datetime.now(
        timezone.utc
    )
    package.sort_code_updated_by_id = admin_id

    return code


def apply_customer_default_to_package(
    package,
    *,
    admin_id=None,
    force=False,
):
    """
    Copy the customer's default code onto a package.

    Existing locked package assignments are preserved unless
    force=True.
    """

    if not isinstance(package, Package):
        raise ValueError(
            "A valid package is required."
        )

    user = package.user

    if not user:
        return set_package_sort_code(
            package,
            "UNASSIGNED",
            source="system",
            admin_id=admin_id,
            lock=False,
            force=force,
        )

    default_code = normalize_sort_code(
        user.default_sort_code
    )

    return set_package_sort_code(
        package,
        default_code,
        source="customer_default",
        admin_id=admin_id,
        lock=False,
        force=force,
    )


def unlock_package_sort_code(
    package,
    *,
    admin_id=None,
):
    if not isinstance(package, Package):
        raise ValueError(
            "A valid package is required."
        )

    package.sort_code_locked = False
    package.sort_code_updated_at = datetime.now(
        timezone.utc
    )
    package.sort_code_updated_by_id = admin_id

    return package