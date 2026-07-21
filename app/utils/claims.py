from app.models import Package


ELIGIBLE_CLAIM_PACKAGE_STATUSES = (
    "Overseas",
    "Received at Local Port",
    "Ready for Pick Up",
)


def get_eligible_claim_packages(user_id: int):
    """
    Return packages belonging to a customer that are eligible
    for a missing in-transit claim.
    """
    return (
        Package.query
        .filter(
            Package.user_id == user_id,
            Package.status.in_(
                ELIGIBLE_CLAIM_PACKAGE_STATUSES
            ),
        )
        .order_by(Package.created_at.desc())
        .all()
    )


def get_eligible_claim_package(
    user_id: int,
    package_id: int,
):
    """
    Return one eligible package belonging to the customer.

    Returns None if:
      - the package does not exist;
      - it belongs to another customer; or
      - its status is not eligible.
    """
    if not user_id or not package_id:
        return None

    return (
        Package.query
        .filter(
            Package.id == package_id,
            Package.user_id == user_id,
            Package.status.in_(
                ELIGIBLE_CLAIM_PACKAGE_STATUSES
            ),
        )
        .first()
    )