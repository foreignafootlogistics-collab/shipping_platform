from flask import Blueprint, render_template, request, redirect, url_for, flash, session
import sqlite3
from app.sqlite_utils import get_db 
from app.routes.admin_auth_routes import admin_required
from app.config import DB_PATH
import os
from werkzeug.utils import secure_filename

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

settings_bp = Blueprint('settings', __name__, url_prefix='/settings')

@settings_bp.route('/update-logo', methods=['POST'])
@admin_required
def update_logo():
    file = request.files.get('logo_file')
    if not file or file.filename.strip() == '':
        flash("No logo selected.", "warning")
        return redirect(url_for('settings.manage_settings'))
    try:
        rel_path = _save_logo(file)  # e.g. 'uploads/logos/company.png'
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE settings SET logo_path=? WHERE id=1", (rel_path,))
        conn.commit(); conn.close()
        flash("Logo updated.", "success")
    except Exception as e:
        flash(f"Error updating logo: {e}", "danger")
    return redirect(url_for('settings.manage_settings'))

@settings_bp.route('/update-display', methods=['POST'])
@admin_required
def update_display():
    currency_code   = (request.form.get('currency_code') or 'USD').strip().upper()
    currency_symbol = request.form.get('currency_symbol') or '$'
    usd_to_jmd      = float(request.form.get('usd_to_jmd') or 0)
    date_format     = (request.form.get('date_format') or '%Y-%m-%d').strip()

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""
          UPDATE settings
             SET currency_code=?,
                 currency_symbol=?,
                 usd_to_jmd=?,
                 date_format=?
           WHERE id=1
        """, (currency_code, currency_symbol, usd_to_jmd, date_format))
        conn.commit()
        flash("Display & formats updated.", "success")
    except Exception as e:
        flash(f"Error updating display: {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for('settings.manage_settings'))


# -----------------------------
# VIEW SETTINGS
# -----------------------------
@settings_bp.route('/', methods=['GET'])
@admin_required
def manage_settings():
    
    conn = get_db()
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM settings WHERE id = 1")
    settings = c.fetchone()
    conn.close()

    return render_template('admin/settings/manage_settings.html', settings=settings)

# -----------------------------
# UPDATE COMPANY INFO
# -----------------------------
@settings_bp.route('/update-company-info', methods=['POST'])
@admin_required
def update_company_info():
    
    company_name = request.form.get('company_name')
    company_address = request.form.get('company_address')
    company_email = request.form.get('company_email')

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""
            UPDATE settings
            SET company_name = ?, company_address = ?, company_email = ?
            WHERE id = 1
        """, (company_name, company_address, company_email))
        conn.commit()
        flash("Company Info updated successfully.", "success")
    except Exception as e:
        flash(f"Error updating Company Info: {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for('settings.manage_settings'))

# -----------------------------
# UPDATE RATES & FEES
# -----------------------------
@settings_bp.route('/update-rates', methods=['POST'])
@admin_required
def update_rates():
    

    base_rate = request.form.get('base_rate')
    handling_fee = request.form.get('handling_fee')

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("""
            UPDATE settings
            SET base_rate = ?, handling_fee = ?
            WHERE id = 1
        """, (float(base_rate or 0), float(handling_fee or 0)))
        conn.commit()
        flash("Rates & Fees updated successfully.", "success")
    except Exception as e:
        flash(f"Error updating Rates & Fees: {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for('settings.manage_settings'))

# -----------------------------
# UPDATE BRANCHES & LOCATIONS
# -----------------------------
@settings_bp.route('/update-branches', methods=['POST'])
@admin_required
def update_branches():
    

    branches = request.form.get('branches')

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("UPDATE settings SET branches = ? WHERE id = 1", (branches,))
        conn.commit()
        flash("Branches & Locations updated successfully.", "success")
    except Exception as e:
        flash(f"Error updating Branches & Locations: {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for('settings.manage_settings'))

# -----------------------------
# UPDATE TERMS & SERVICES
# -----------------------------
@settings_bp.route('/update-terms', methods=['POST'])
@admin_required
def update_terms():
    
    terms = request.form.get('terms')

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute("UPDATE settings SET terms = ? WHERE id = 1", (terms,))
        conn.commit()
        flash("Terms & Services updated successfully.", "success")
    except Exception as e:
        flash(f"Error updating Terms & Services: {e}", "danger")
    finally:
        conn.close()

    return redirect(url_for('settings.manage_settings'))
