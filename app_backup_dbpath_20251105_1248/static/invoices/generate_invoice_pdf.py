import os
from datetime import datetime
from flask import render_template
from weasyprint import HTML

INVOICE_FOLDER = os.path.join('static', 'invoices')
os.makedirs(INVOICE_FOLDER, exist_ok=True)

def generate_invoice_pdf(data):
    """Generate PDF and return relative path for payments.invoice_path"""
    from weasyprint import HTML
    html = render_template('admin/invoice_template.html', data=data)
    filename = f"invoice_{data['bill_number']}_{data['payment_id']}.pdf"
    filepath = os.path.join('static', 'invoices', filename)
    HTML(string=html).write_pdf(filepath)
    return f"invoices/{filename}"  # relative path to use in url_for
