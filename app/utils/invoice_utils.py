import os
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

INVOICE_FOLDER = os.path.join('static', 'invoices')
os.makedirs(INVOICE_FOLDER, exist_ok=True)

def generate_invoice(payment):
    """
    Generate a PDF invoice for a payment.
    payment: dict containing bill_number, user_id, payment_date, payment_type, amount, authorized_by
    """
    filename = f"invoice_{payment['bill_number']}.pdf"
    filepath = os.path.join(INVOICE_FOLDER, filename)

    c = canvas.Canvas(filepath, pagesize=letter)
    width, height = letter

    # Title
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, height - 50, "INVOICE")

    # Payment info
    c.setFont("Helvetica", 12)
    c.drawString(50, height - 100, f"Bill #: {payment['bill_number']}")
    c.drawString(50, height - 120, f"Date: {payment['payment_date']}")
    c.drawString(50, height - 140, f"Payment Type: {payment['payment_type']}")
    c.drawString(50, height - 160, f"Amount Paid: ${payment['amount']:.2f}")
    c.drawString(50, height - 180, f"Authorized By: {payment['authorized_by']}")

    # Footer
    c.drawString(50, 50, "Foreign A Foot Logistics Limited")

    c.save()
    return filepath  # store this path in invoice_path column
