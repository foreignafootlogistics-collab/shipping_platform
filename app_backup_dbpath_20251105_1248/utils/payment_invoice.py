import os
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from flask import current_app

def generate_payment_invoice(payment: dict) -> str:
    static_dir = current_app.static_folder
    rel = f"invoices/receipt_{payment['bill_number']}.pdf"
    abs_ = os.path.join(static_dir, rel)
    os.makedirs(os.path.dirname(abs_), exist_ok=True)

    c = canvas.Canvas(abs_, pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 16); c.drawString(50, h-50, "PAYMENT RECEIPT")
    c.setFont("Helvetica", 12)
    c.drawString(50, h-100, f"Bill #: {payment['bill_number']}")
    c.drawString(50, h-120, f"Date: {payment['payment_date']}")
    c.drawString(50, h-140, f"Payment Type: {payment['payment_type']}")
    c.drawString(50, h-160, f"Amount Paid: ${float(payment['amount']):.2f}")
    c.drawString(50, h-180, f"Authorized By: {payment['authorized_by']}")
    c.drawString(50, 50, "Foreign A Foot Logistics Limited")
    c.save()
    return rel
