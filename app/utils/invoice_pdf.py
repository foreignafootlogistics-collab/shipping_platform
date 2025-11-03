import os
from flask import render_template, current_app, url_for
from weasyprint import HTML, CSS

def generate_invoice_pdf(invoice: dict) -> str:
    # filename pieces
    bill = invoice.get('number') or f"INV-{invoice.get('id','0')}"
    filename = f"invoice_{bill}.pdf"

    # paths
    static_dir = current_app.static_folder
    relpath = f"invoices/{filename}"
    abspath = os.path.join(static_dir, 'invoices', filename)
    os.makedirs(os.path.dirname(abspath), exist_ok=True)

    # asset urls
    logo_url = url_for('static', filename='logo.png')
    css_url  = url_for('static', filename='css/invoice.css')

    # render
    html = render_template(
        'admin/invoice_template.html',
        invoice=invoice,
        logo_url=logo_url,
        css_url=css_url,
    )

    HTML(string=html, base_url=current_app.root_path).write_pdf(
        abspath,
        stylesheets=[CSS(os.path.join(static_dir, 'css', 'invoice.css'))]
    )
    return relpath
