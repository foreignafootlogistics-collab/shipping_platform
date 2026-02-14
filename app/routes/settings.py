from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.utils import secure_filename
import os

from app.routes.admin_auth_routes import admin_required
from app.extensions import db
from app.models import Settings, AdminRate  # make sure this model exists and has the used columns

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
@admin_required
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
@admin_required
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

        settings.currency_code = currency_code
        settings.currency_symbol = currency_symbol
        settings.usd_to_jmd = usd_to_jmd
        settings.date_format = date_format

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
    return render_template('admin/settings/manage_settings.html',
                           settings=settings,
                           admin_rates=admin_rates)



# -----------------------------
# UPDATE COMPANY INFO
# -----------------------------
@settings_bp.route('/update-company-info', methods=['POST'])
@admin_required
def update_company_info():
    company_name    = request.form.get('company_name')
    company_address = request.form.get('company_address')
    company_email   = request.form.get('company_email')

    try:
        settings = _get_settings_row()
        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        settings.company_name = company_name
        settings.company_address = company_address
        settings.company_email = company_email

        db.session.commit()
        flash("Company Info updated successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating Company Info: {e}", "danger")

    return redirect(url_for('settings.manage_settings'))


# -----------------------------
# UPDATE RATES & FEES
# -----------------------------
@settings_bp.route('/update-rates', methods=['POST'])
@admin_required
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

        # --- Freight tab values ---
        settings.base_rate            = f('base_rate')
        settings.handling_fee         = f('handling_fee')
        settings.special_below_1lb_jmd   = f('special_below_1lb_jmd')
        settings.per_0_1lb_below_1lb_jmd = f('per_0_1lb_below_1lb_jmd')
        settings.min_billable_weight     = i('min_billable_weight', 1)
        settings.per_lb_above_100_jmd    = f('per_lb_above_100_jmd')
        settings.handling_above_100_jmd  = f('handling_above_100_jmd')
        settings.weight_round_method     = request.form.get('weight_round_method') or "round_up"

        # --- Customs / Duty tab values ---
        settings.customs_enabled       = ('customs_enabled' in request.form)  # checkbox
        settings.customs_exchange_rate = f('customs_exchange_rate', 165.0)
        settings.diminis_point_usd     = f('diminis_point_usd')
        settings.default_duty_rate     = f('default_duty_rate')
        settings.insurance_rate        = f('insurance_rate')
        settings.scf_rate              = f('scf_rate')
        settings.envl_rate             = f('envl_rate')
        settings.stamp_duty_jmd        = f('stamp_duty_jmd')
        settings.gct_25_rate           = f('gct_25_rate')
        settings.gct_15_rate           = f('gct_15_rate')
        settings.caf_residential_jmd   = f('caf_residential_jmd')
        settings.caf_commercial_jmd    = f('caf_commercial_jmd')

        # --- Optional: update per-lb AdminRate table ---
        # inputs like rate_1, rate_2, ... rate_50
        for rate in AdminRate.query.all():
            field_name = f"rate_{rate.max_weight}"
            if field_name in request.form:
                rate.rate = f(field_name, rate.rate or 0)

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
@admin_required
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
# UPDATE TERMS & SERVICES
# -----------------------------
@settings_bp.route('/update-terms', methods=['POST'])
@admin_required
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
@admin_required
def update_us_address():
    us_street       = request.form.get('us_street')
    us_suite_prefix = request.form.get('us_suite_prefix')   # e.g. "KCDA-FAFL# "
    us_city         = request.form.get('us_city')
    us_state        = request.form.get('us_state')
    us_zip          = request.form.get('us_zip')

    try:
        settings = _get_settings_row()
        if not settings:
            flash("Settings row not found.", "danger")
            return redirect(url_for('settings.manage_settings'))

        settings.us_street       = us_street
        settings.us_suite_prefix = us_suite_prefix
        settings.us_city         = us_city
        settings.us_state        = us_state
        settings.us_zip          = us_zip

        db.session.commit()
        flash("US warehouse address updated successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error updating US warehouse address: {e}", "danger")

    return redirect(url_for('settings.manage_settings'))

