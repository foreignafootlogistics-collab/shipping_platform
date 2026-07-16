from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import current_user
from werkzeug.utils import secure_filename
import os

from app.routes.admin_auth_routes import admin_required
from app.extensions import db
from app.models import Settings, AdminRate, Counter, AuditLog
# Where to store the logo
LOGO_UPLOAD_DIR = os.path.join('static', 'uploads', 'logos')
os.makedirs(LOGO_UPLOAD_DIR, exist_ok=True)


def _save_logo(file_storage):
    """Save logo to static/uploads/logos and return relative path."""
    fname = secure_filename(file_storage.filename)
    ext = os.path.splitext(fname)[1].lower()
    if ext not in ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp']:
        raise ValueError("Unsupported logo type.")
    path = os.path.join(LOGO_UPLOAD_DIR, f"company{ext}")
    file_storage.save(path)
    # Return path relative to /static for url_for('static', filename=...)
    return path.replace('static' + os.sep, '').replace('\\', '/')


settings_bp = Blueprint('settings', __name__, url_prefix='/admin/settings')

# Small helper to always work on row id=1
def _get_settings_row(create_if_missing: bool = True) -> Settings | None:
    s = db.session.get(Settings, 1)
    if not s and create_if_missing:
        s = Settings(id=1)
        db.session.add(s)
        db.session.commit()
    return s


@settings_bp.route('/update-logo', methods=['POST'])
@admin_required(roles=["superadmin"])
def update_logo():
    file = request.files.get('logo_file')
    if not file or file.filename.strip() == '':
        flash("No logo selected.", "warning")
        return redirect(url_for('settings.manage_settings'))

    try:
        rel_path = _save_logo(file)  # e.g. 'uploads/logos/company.png'
        settings = _get_settings_row()
        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        settings.logo_path = rel_path
        db.session.commit()
        flash("Logo updated.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating logo: {e}", "danger")

    return redirect(url_for('settings.manage_settings'))


@settings_bp.route('/update-display', methods=['POST'])
@admin_required(roles=["superadmin"])
def update_display():
    currency_code   = (request.form.get('currency_code') or 'USD').strip().upper()
    currency_symbol = request.form.get('currency_symbol') or '$'
    usd_to_jmd_raw  = request.form.get('usd_to_jmd') or 0
    date_format     = (request.form.get('date_format') or '%Y-%m-%d').strip()

    try:
        usd_to_jmd = float(usd_to_jmd_raw)
    except ValueError:
        usd_to_jmd = 0.0

    try:
        settings = _get_settings_row()
        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        old_currency_code = settings.currency_code
        old_currency_symbol = settings.currency_symbol
        old_usd_to_jmd = settings.usd_to_jmd
        old_date_format = settings.date_format

        settings.currency_code = currency_code
        settings.currency_symbol = currency_symbol
        settings.usd_to_jmd = usd_to_jmd
        settings.date_format = date_format

        changes = []

        if str(old_currency_code) != str(settings.currency_code):
            changes.append(f"Currency Code: {old_currency_code} → {settings.currency_code}")

        if str(old_currency_symbol) != str(settings.currency_symbol):
            changes.append(f"Currency Symbol: {old_currency_symbol} → {settings.currency_symbol}")

        if str(old_usd_to_jmd) != str(settings.usd_to_jmd):
            changes.append(f"USD to JMD: {old_usd_to_jmd} → {settings.usd_to_jmd}")

        if str(old_date_format) != str(settings.date_format):
            changes.append(f"Date Format: {old_date_format} → {settings.date_format}")

        if changes:
            db.session.add(AuditLog(
                module="Settings",
                action="Display & Currency Settings Updated",
                admin_id=current_user.id,
                user_id=None,
                entity_type="Settings",
                entity_id=settings.id,
                reason="Display/currency settings update",
                description="; ".join(changes),
                old_value=(
                    f"Currency Code: {old_currency_code}; "
                    f"Currency Symbol: {old_currency_symbol}; "
                    f"USD to JMD: {old_usd_to_jmd}; "
                    f"Date Format: {old_date_format}"
                ),
                new_value=(
                    f"Currency Code: {settings.currency_code}; "
                    f"Currency Symbol: {settings.currency_symbol}; "
                    f"USD to JMD: {settings.usd_to_jmd}; "
                    f"Date Format: {settings.date_format}"
                ),
            ))

        db.session.commit()
        flash("Display & formats updated.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Error updating display: {e}", "danger")

    return redirect(url_for('settings.manage_settings'))


# -----------------------------
# VIEW SETTINGS
# -----------------------------
@settings_bp.route('/', methods=['GET'])
@admin_required(roles=["superadmin"])
def manage_settings():
    settings = _get_settings_row(create_if_missing=True)
    admin_rates = AdminRate.query.order_by(AdminRate.max_weight.asc()).all()
    registration_counter = db.session.get(Counter, "registration_number")

    return render_template('admin/settings/manage_settings.html',
                           settings=settings,
                           admin_rates=admin_rates,
                           registration_counter=registration_counter)



# -----------------------------
# UPDATE COMPANY INFO
# -----------------------------
@settings_bp.route('/update-company-info', methods=['POST'])
@admin_required(roles=["superadmin"])
def update_company_info():
    company_name    = request.form.get('company_name')
    company_address = request.form.get('company_address')
    company_email   = request.form.get('company_email')

    try:
        settings = _get_settings_row()
        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        old_company_name = settings.company_name
        old_company_address = settings.company_address
        old_company_email = settings.company_email

        settings.company_name = company_name
        settings.company_address = company_address
        settings.company_email = company_email

        changes = []

        if str(old_company_name) != str(settings.company_name):
            changes.append(f"Company Name: {old_company_name} → {settings.company_name}")

        if str(old_company_address) != str(settings.company_address):
            changes.append(f"Company Address: {old_company_address} → {settings.company_address}")

        if str(old_company_email) != str(settings.company_email):
            changes.append(f"Company Email: {old_company_email} → {settings.company_email}")

        if changes:
            db.session.add(AuditLog(
                module="Settings",
                action="Company Info Updated",
                admin_id=current_user.id,
                user_id=None,
                entity_type="Settings",
                entity_id=settings.id,
                reason="Company information update",
                description="; ".join(changes),
                old_value=(
                    f"Company Name: {old_company_name}; "
                    f"Company Address: {old_company_address}; "
                    f"Company Email: {old_company_email}"
                ),
                new_value=(
                    f"Company Name: {settings.company_name}; "
                    f"Company Address: {settings.company_address}; "
                    f"Company Email: {settings.company_email}"
                ),
            ))

        db.session.commit()
        flash("Company Info updated successfully.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Error updating Company Info: {e}", "danger")

    return redirect(url_for('settings.manage_settings'))



@settings_bp.route('/update-registration-settings', methods=['POST'])
@admin_required(roles=["superadmin"])
def update_registration_settings():
    def i(name, default=0):
        raw = (request.form.get(name) or "").strip()
        try:
            return int(raw)
        except ValueError:
            return default

    try:
        settings = _get_settings_row()
        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        counter = db.session.get(Counter, "registration_number")

        old_prefix = settings.registration_prefix
        old_width = settings.registration_number_width
        old_reuse = bool(settings.reuse_deleted_registration_numbers)
        old_lock = bool(settings.lock_registration_number)
        old_counter_value = counter.value if counter else None

        prefix = (request.form.get("registration_prefix") or "FAFL").strip().upper()
        width = i("registration_number_width", 5)
        counter_value = i("registration_counter_value", 10000)

        if width < 1:
            width = 5

        settings.registration_prefix = prefix
        settings.registration_number_width = width
        settings.reuse_deleted_registration_numbers = "reuse_deleted_registration_numbers" in request.form
        settings.lock_registration_number = "lock_registration_number" in request.form

        if not counter:
            counter = Counter(name="registration_number", value=counter_value)
            db.session.add(counter)
        else:
            counter.value = counter_value

        changes = []

        if str(old_prefix) != str(settings.registration_prefix):
            changes.append(f"Prefix: {old_prefix} → {settings.registration_prefix}")

        if str(old_width) != str(settings.registration_number_width):
            changes.append(f"Width: {old_width} → {settings.registration_number_width}")

        if str(old_reuse) != str(bool(settings.reuse_deleted_registration_numbers)):
            changes.append(
                f"Reuse Deleted Numbers: {old_reuse} → {bool(settings.reuse_deleted_registration_numbers)}"
            )

        if str(old_lock) != str(bool(settings.lock_registration_number)):
            changes.append(
                f"Lock Registration Number: {old_lock} → {bool(settings.lock_registration_number)}"
            )

        if str(old_counter_value) != str(counter.value):
            changes.append(f"Counter: {old_counter_value} → {counter.value}")

        if changes:
            db.session.add(AuditLog(
                module="Settings",
                action="Registration Settings Updated",
                admin_id=current_user.id,
                user_id=None,
                entity_type="Settings",
                entity_id=settings.id,
                reason="Registration numbering settings update",
                description="; ".join(changes),
                old_value=(
                    f"Prefix: {old_prefix}; "
                    f"Width: {old_width}; "
                    f"Reuse Deleted Numbers: {old_reuse}; "
                    f"Lock Registration Number: {old_lock}; "
                    f"Counter: {old_counter_value}"
                ),
                new_value=(
                    f"Prefix: {settings.registration_prefix}; "
                    f"Width: {settings.registration_number_width}; "
                    f"Reuse Deleted Numbers: {bool(settings.reuse_deleted_registration_numbers)}; "
                    f"Lock Registration Number: {bool(settings.lock_registration_number)}; "
                    f"Counter: {counter.value}"
                ),
            ))

        db.session.commit()
        flash("Registration settings updated successfully.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Error updating registration settings: {e}", "danger")

    return redirect(url_for('settings.manage_settings'))


# -----------------------------
# UPDATE RATES & FEES
# -----------------------------
@settings_bp.route('/update-rates', methods=['POST'])
@admin_required(roles=["superadmin"])
def update_rates():
    def f(name, default=0.0):
        raw = request.form.get(name, "").strip()
        if raw == "":
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def i(name, default=0):
        raw = request.form.get(name, "").strip()
        if raw == "":
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    try:
        settings = _get_settings_row()
        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        tracked_fields = [
            "base_rate",
            "handling_fee",
            "special_below_1lb_jmd",
            "per_0_1lb_below_1lb_jmd",
            "min_billable_weight",
            "per_lb_above_100_jmd",
            "handling_above_100_jmd",
            "weight_round_method",
            "customs_enabled",
            "customs_exchange_rate",
            "diminis_point_usd",
            "default_duty_rate",
            "insurance_rate",
            "scf_rate",
            "envl_rate",
            "stamp_duty_jmd",
            "gct_25_rate",
            "gct_15_rate",
            "caf_residential_jmd",
            "caf_commercial_jmd",
            "bad_address_fee_jmd",
        ]

        old_values = {
            field: getattr(settings, field, None)
            for field in tracked_fields
        }

        old_admin_rates = {
            rate.id: {
                "max_weight": rate.max_weight,
                "rate": float(rate.rate or 0),
            }
            for rate in AdminRate.query.all()
        }

        # --- Freight tab values ---
        settings.base_rate                = f('base_rate')
        settings.handling_fee             = f('handling_fee')
        settings.special_below_1lb_jmd    = f('special_below_1lb_jmd')
        settings.per_0_1lb_below_1lb_jmd  = f('per_0_1lb_below_1lb_jmd')
        settings.min_billable_weight      = i('min_billable_weight', 1)
        settings.per_lb_above_100_jmd     = f('per_lb_above_100_jmd')
        settings.handling_above_100_jmd   = f('handling_above_100_jmd')
        settings.weight_round_method      = request.form.get('weight_round_method') or "round_up"

        # --- Customs / Duty tab values ---
        settings.customs_enabled          = ('customs_enabled' in request.form)
        settings.customs_exchange_rate    = f('customs_exchange_rate', 165.0)
        settings.diminis_point_usd        = f('diminis_point_usd')
        settings.default_duty_rate        = f('default_duty_rate')
        settings.insurance_rate           = f('insurance_rate')
        settings.scf_rate                 = f('scf_rate')
        settings.envl_rate                = f('envl_rate')
        settings.stamp_duty_jmd           = f('stamp_duty_jmd')
        settings.gct_25_rate              = f('gct_25_rate')
        settings.gct_15_rate              = f('gct_15_rate')
        settings.caf_residential_jmd      = f('caf_residential_jmd')
        settings.caf_commercial_jmd       = f('caf_commercial_jmd')
        settings.bad_address_fee_jmd      = f('bad_address_fee_jmd', 500.0)

        # --- Optional: update per-lb AdminRate table ---
        changed_admin_rates = []

        for rate in AdminRate.query.all():
            field_name = f"rate_{rate.max_weight}"

            if field_name in request.form:
                old_rate = float(rate.rate or 0)
                new_rate = f(field_name, rate.rate or 0)

                if old_rate != float(new_rate or 0):
                    changed_admin_rates.append(
                        f"{rate.max_weight} lb: {old_rate:,.2f} → {float(new_rate or 0):,.2f}"
                    )

                rate.rate = new_rate

        changed_fields = []

        for field in tracked_fields:
            old = old_values.get(field)
            new = getattr(settings, field, None)

            if str(old) != str(new):
                changed_fields.append(
                    f"{field}: {old} → {new}"
                )

        if changed_fields or changed_admin_rates:
            description_parts = []

            if changed_fields:
                description_parts.append(
                    "Settings changed: " + "; ".join(changed_fields)
                )

            if changed_admin_rates:
                description_parts.append(
                    "Admin per-lb rates changed: " + "; ".join(changed_admin_rates)
                )

            db.session.add(AuditLog(
                module="Settings",
                action="Rates & Fees Updated",
                admin_id=current_user.id,
                user_id=None,
                entity_type="Settings",
                entity_id=settings.id,
                reason="Rates and fees settings update",
                description=" | ".join(description_parts),
                old_value="; ".join(
                    f"{field}: {old_values.get(field)}"
                    for field in tracked_fields
                ),
                new_value="; ".join(
                    f"{field}: {getattr(settings, field, None)}"
                    for field in tracked_fields
                ),
            ))

        db.session.commit()
        flash("Rates & Fees updated successfully.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Error updating Rates & Fees: {e}", "danger")

    return redirect(url_for('settings.manage_settings'))



# -----------------------------
# UPDATE BRANCHES & LOCATIONS
# -----------------------------
@settings_bp.route('/update-branches', methods=['POST'])
@admin_required(roles=["superadmin"])
def update_branches():
    branches = request.form.get('branches')

    try:
        settings = _get_settings_row()
        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        settings.branches = branches
        db.session.commit()
        flash("Branches & Locations updated successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating Branches & Locations: {e}", "danger")

    return redirect(url_for('settings.manage_settings'))

# -----------------------------
# UPDATE DELIVERY SETTINGS
# -----------------------------
@settings_bp.route('/update-delivery-settings', methods=['POST'])
@admin_required(roles=["superadmin"])
def update_delivery_settings():

    def f(name, default=0.0):
        raw = request.form.get(name, "").strip()

        if raw == "":
            return default

        try:
            return float(raw)
        except ValueError:
            return default

    try:
        settings = _get_settings_row()

        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        tracked_fields = [
            "kingston_dispatch_address",
            "stc_dispatch_address",
            "kingston_delivery_branch_name",
            "stc_delivery_branch_name",
            "delivery_base_km",
            "delivery_base_fee_jmd",
            "delivery_per_km_jmd",
            "kingston_free_delivery_days",
            "stc_free_delivery_days",
            "max_delivery_distance_km",
            "saturday_delivery_fee_jmd",
            "allow_saturday_delivery",
            "google_maps_api_key",
        ]

        old_values = {
            field: getattr(settings, field, None)
            for field in tracked_fields
        }

        settings.kingston_dispatch_address = request.form.get(
            'kingston_dispatch_address'
        )

        settings.stc_dispatch_address = request.form.get(
            'stc_dispatch_address'
        )

        settings.kingston_delivery_branch_name = request.form.get(
            'kingston_delivery_branch_name'
        )

        settings.stc_delivery_branch_name = request.form.get(
            'stc_delivery_branch_name'
        )

        settings.delivery_base_km = f(
            'delivery_base_km',
            10
        )

        settings.delivery_base_fee_jmd = f(
            'delivery_base_fee_jmd',
            1000
        )

        settings.delivery_per_km_jmd = f(
            'delivery_per_km_jmd',
            100
        )

        settings.kingston_free_delivery_days = request.form.get(
            'kingston_free_delivery_days'
        )

        settings.stc_free_delivery_days = request.form.get(
            'stc_free_delivery_days'
        )

        settings.max_delivery_distance_km = f(
            'max_delivery_distance_km',
            35
        )

        settings.saturday_delivery_fee_jmd = f(
            'saturday_delivery_fee_jmd',
            1000
        )

        settings.allow_saturday_delivery = (
            'allow_saturday_delivery' in request.form
        )

        settings.google_maps_api_key = request.form.get(
            'google_maps_api_key'
        )

        changes = []

        for field in tracked_fields:
            old = old_values.get(field)
            new = getattr(settings, field, None)

            # Do not expose full Google Maps API key in audit logs
            if field == "google_maps_api_key":
                old_display = "Set" if old else "Not Set"
                new_display = "Set" if new else "Not Set"
            else:
                old_display = old
                new_display = new

            if str(old_display) != str(new_display):
                changes.append(
                    f"{field}: {old_display} → {new_display}"
                )

        if changes:
            db.session.add(AuditLog(
                module="Settings",
                action="Delivery Settings Updated",
                admin_id=current_user.id,
                user_id=None,
                entity_type="Settings",
                entity_id=settings.id,
                reason="Delivery settings update",
                description="; ".join(changes),
                old_value="; ".join(
                    f"{field}: {'Set' if field == 'google_maps_api_key' and old_values.get(field) else ('Not Set' if field == 'google_maps_api_key' else old_values.get(field))}"
                    for field in tracked_fields
                ),
                new_value="; ".join(
                    f"{field}: {'Set' if field == 'google_maps_api_key' and getattr(settings, field, None) else ('Not Set' if field == 'google_maps_api_key' else getattr(settings, field, None))}"
                    for field in tracked_fields
                ),
            ))

        db.session.commit()

        flash(
            "Delivery settings updated successfully.",
            "success"
        )

    except Exception as e:
        db.session.rollback()

        flash(
            f"Error updating delivery settings: {e}",
            "danger"
        )

    return redirect(url_for('settings.manage_settings'))

# -----------------------------
# UPDATE TERMS & SERVICES
# -----------------------------
@settings_bp.route('/update-terms', methods=['POST'])
@admin_required(roles=["superadmin"])
def update_terms():
    terms = request.form.get('terms')

    try:
        settings = _get_settings_row()
        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        settings.terms = terms
        db.session.commit()
        flash("Terms & Services updated successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating Terms & Services: {e}", "danger")

    return redirect(url_for('settings.manage_settings'))

# -----------------------------
# UPDATE US WAREHOUSE ADDRESS
# -----------------------------
@settings_bp.route('/update-us-address', methods=['POST'])
@admin_required(roles=["superadmin"])
def update_us_address():
    us_street       = request.form.get('us_street')
    us_suite_prefix = request.form.get('us_suite_prefix')
    us_city         = request.form.get('us_city')
    us_state        = request.form.get('us_state')
    us_zip          = request.form.get('us_zip')

    try:
        settings = _get_settings_row()
        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        old_street = settings.us_street
        old_suite_prefix = settings.us_suite_prefix
        old_city = settings.us_city
        old_state = settings.us_state
        old_zip = settings.us_zip

        settings.us_street       = us_street
        settings.us_suite_prefix = us_suite_prefix
        settings.us_city         = us_city
        settings.us_state        = us_state
        settings.us_zip          = us_zip

        changes = []

        if str(old_street) != str(settings.us_street):
            changes.append(f"Street: {old_street} → {settings.us_street}")

        if str(old_suite_prefix) != str(settings.us_suite_prefix):
            changes.append(f"Suite Prefix: {old_suite_prefix} → {settings.us_suite_prefix}")

        if str(old_city) != str(settings.us_city):
            changes.append(f"City: {old_city} → {settings.us_city}")

        if str(old_state) != str(settings.us_state):
            changes.append(f"State: {old_state} → {settings.us_state}")

        if str(old_zip) != str(settings.us_zip):
            changes.append(f"ZIP: {old_zip} → {settings.us_zip}")

        if changes:
            db.session.add(AuditLog(
                module="Settings",
                action="US Warehouse Address Updated",
                admin_id=current_user.id,
                user_id=None,
                entity_type="Settings",
                entity_id=settings.id,
                reason="US warehouse address update",
                description="; ".join(changes),
                old_value=(
                    f"Street: {old_street}; "
                    f"Suite Prefix: {old_suite_prefix}; "
                    f"City: {old_city}; "
                    f"State: {old_state}; "
                    f"ZIP: {old_zip}"
                ),
                new_value=(
                    f"Street: {settings.us_street}; "
                    f"Suite Prefix: {settings.us_suite_prefix}; "
                    f"City: {settings.us_city}; "
                    f"State: {settings.us_state}; "
                    f"ZIP: {settings.us_zip}"
                ),
            ))

        db.session.commit()
        flash("US warehouse address updated successfully.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Error updating US warehouse address: {e}", "danger")

    return redirect(url_for('settings.manage_settings'))