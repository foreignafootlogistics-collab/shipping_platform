from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, DecimalField, DateField, FieldList, HiddenField, FormField, RadioField, TextAreaField, FileField, SelectMultipleField, widgets, SelectField, HiddenField, IntegerField, TimeField
from flask_wtf.file import FileField, FileAllowed, FileRequired
from wtforms.validators import DataRequired, Email, EqualTo, NumberRange, Length, Optional, Regexp
from wtforms import Form  # For nested, non-CSRF forms
from wtforms.fields import EmailField
from app.calculator_data import CATEGORIES
from app.calculator_data import categories


class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Log In')

class AdminLoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Login')

class RegisterForm(FlaskForm):
    full_name = StringField('Full Name', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    trn = StringField('TRN', validators=[DataRequired()])
    mobile = StringField('Mobile', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    confirm = PasswordField('Confirm Password', validators=[DataRequired()])
    referrer_code = StringField('Referral Code (Optional)', validators=[Optional()])
    submit = SubmitField('Register')
   

class AdminRegisterForm(FlaskForm):
    full_name = StringField(
        "Full Name",
        validators=[DataRequired(), Length(min=3, max=120)]
    )
    email = StringField(
        "Email",
        validators=[DataRequired(), Email(), Length(max=255)]
    )
    password = PasswordField(
        "Password",
        validators=[DataRequired(), Length(min=6)]
    )
    confirm = PasswordField(
        "Confirm Password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")]
    )

    # 🔹 New: role selector
    role = SelectField(
        "Admin Role",
        choices=[
            ("admin", "General Admin"),
            ("finance", "Finance"),
            ("operations", "Operations"),
            ("accounts_manager", "Accounts & Profiles"),
        ],
        validators=[DataRequired()],
        default="admin",
    )

    submit = SubmitField("Create Admin")


class ScheduledDeliveryForm(FlaskForm):
    # Which packages are being scheduled (IDs as comma-separated string or handle in your route)
    package_ids = HiddenField(validators=[Optional()])

    # Delivery details
    date = DateField("Delivery Date", validators=[DataRequired(message="Select a date")])
    time = TimeField("Delivery Time", validators=[Optional()])  # or DataRequired if mandatory
    location = StringField("Delivery Location", validators=[DataRequired(), Length(max=255)])
    contact_name = StringField("Contact Name", validators=[Optional(), Length(max=120)])
    contact_phone = StringField("Contact Phone", validators=[Optional(), Length(max=40)])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=1000)])

    submit = SubmitField("Schedule Delivery")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField('Current Password', validators=[DataRequired()])
    new_password = PasswordField('New Password', validators=[DataRequired(), EqualTo('confirm', message='Passwords must match')])
    confirm = PasswordField('Confirm New Password', validators=[DataRequired()])
    submit = SubmitField('Change Password')

class UploadUsersForm(FlaskForm):
    file = FileField('Select Excel File (.xlsx)', validators=[
        FileRequired(),
        FileAllowed(['xlsx'], 'Excel files only!')
    ])
    submit = SubmitField('Upload Users')

class ConfirmUploadForm(FlaskForm):
    pass
    submit = SubmitField("Confirm Upload")

class SendMessageForm(FlaskForm):
    subject = StringField("Subject", validators=[DataRequired()])
    body = TextAreaField("Message", validators=[DataRequired()])
    recipient_ids = SelectMultipleField(
        "Recipients",
        coerce=int,
        option_widget=widgets.CheckboxInput(),
        widget=widgets.ListWidget(prefix_label=False)
    )
    submit = SubmitField("Send")


class SettingsForm(FlaskForm):
    company_name = StringField('Company Name', validators=[DataRequired()])
    company_address = TextAreaField('Company Address', validators=[Optional()])
    company_email = StringField('Company Email', validators=[Optional(), Email()])
    base_rate = DecimalField('Base Rate (JMD)', validators=[Optional()])
    handling_fee = DecimalField('Handling Fee (JMD)', validators=[Optional()])
    branches = TextAreaField('Branches (comma-separated)', validators=[Optional()])
    terms = TextAreaField('Terms & Conditions', validators=[Optional()])
    privacy_policy = TextAreaField('Privacy Policy', validators=[Optional()])
    submit = SubmitField('Save Settings')


class AdminMessageForm(FlaskForm):
    recipient_ids = SelectMultipleField('Recipients', coerce=int, validators=[DataRequired()])
    subject = StringField('Subject', validators=[DataRequired()])
    body = TextAreaField('Message', validators=[DataRequired()])
    submit = SubmitField('Send')

class PackageBulkActionForm(FlaskForm):
    search = StringField('Search', validators=[Optional()])
    status = SelectField('Status', choices=[
        ('', 'All Statuses'),
        ('Overseas', 'Overseas'),
        ('Ready for Pick Up', 'Ready for Pick Up'),
        ('Delivered', 'Delivered')
    ], validators=[Optional()])
    
    date_from = DateField('Date From', validators=[Optional()])
    date_to = DateField('Date To', validators=[Optional()])
    
    new_status = StringField('New Status', validators=[Optional()])
    new_rate = DecimalField('New Rate', validators=[Optional()])

class UploadPackageForm(FlaskForm):
    file = FileField('Excel or CSV File (.xlsx, .csv)', validators=[
        FileRequired(message='Please select a file.'),
        FileAllowed(['xlsx', 'csv'], 'Only Excel (.xlsx) or CSV (.csv) files are allowed.')
    ])
    submit = SubmitField('Upload')

class MultiCheckboxField(SelectMultipleField):
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()

class BulkMessageForm(FlaskForm):
    subject = StringField('Subject', validators=[DataRequired()])
    message = TextAreaField('Message', validators=[DataRequired()])
    user_ids = SelectMultipleField('Select Recipients', coerce=int, validators=[DataRequired()])
    submit = SubmitField('Send Messages')


class PersonalInfoForm(FlaskForm):
    full_name = StringField("Full Name", validators=[
        DataRequired(),
        Length(min=2, max=100)
    ])

    email = EmailField("Email", validators=[
        DataRequired(),
        Email()
    ])

    mobile = StringField("Mobile", validators=[
        DataRequired(),
        Regexp(r'^\d{7,15}$', message="Enter a valid mobile number.")
    ])

    trn = StringField("TRN", validators=[
        DataRequired(),
        Regexp(r'^\d{9}$', message="TRN must be 9 digits.")
    ])

    submit = SubmitField("Update Account")


class AddressForm(FlaskForm):
    address = TextAreaField("Delivery Address", validators=[
        DataRequired(),
        Length(min=5, max=300)
    ])
    submit = SubmitField("Save Address")


class PasswordChangeForm(FlaskForm):
    current_password = PasswordField("Current Password", validators=[DataRequired()])
    new_password = PasswordField("New Password", validators=[
        DataRequired(),
        Length(min=8, message="Password must be at least 8 characters."),
        Regexp(r'.*[A-Z].*', message="Must contain an uppercase letter."),
        Regexp(r'.*[a-z].*', message="Must contain a lowercase letter."),
        Regexp(r'.*[0-9].*', message="Must contain a number."),
        Regexp(r'.*[\W_].*', message="Must contain a special character.")
    ])
    confirm_password = PasswordField("Confirm New Password", validators=[
        DataRequired(),
        EqualTo('new_password', message='Passwords must match.')
    ])
    submit = SubmitField("Update Password")

# ---------------------------
# Single rate form (used for adding or editing one rate)
# ---------------------------
class SingleRateForm(FlaskForm):
    max_weight = DecimalField(
        'Max Weight (lb)',
        validators=[DataRequired(), NumberRange(min=0)],
        places=2  # Optional: limits decimal places in form
    )
    rate = DecimalField(
        'Rate (JMD)',
        validators=[DataRequired(), NumberRange(min=0)],
        places=2
    )
    submit = SubmitField('Add Rate')


# ---------------------------
# Mini rate form (used inside Bulk form)
# ---------------------------
class MiniRateForm(FlaskForm):
    max_weight = DecimalField(
        'Max Weight (lb)',
        validators=[NumberRange(min=0)],
        places=2
    )
    rate = DecimalField(
        'Rate (JMD)',
        validators=[NumberRange(min=0)],
        places=2
    )


# ---------------------------
# Bulk add form (10 rows by default)
# ---------------------------
class BulkRateForm(FlaskForm):
    rates = FieldList(FormField(MiniRateForm), min_entries=10)
    submit = SubmitField('Add Rates')
class InvoiceItemForm(Form):  # No CSRF because it's a nested form
    description = StringField('Description', validators=[DataRequired()])
    weight = DecimalField('Weight (lbs)', validators=[DataRequired(), NumberRange(min=0)])
    rate = DecimalField('Rate ($/lb)', validators=[DataRequired(), NumberRange(min=0)])
    due_date = DateField('Due Date', format='%Y-%m-%d', validators=[DataRequired()])

class InvoiceItemForm(FlaskForm):
    description = StringField('Description', validators=[DataRequired()])
    weight = DecimalField('Weight (lbs)', places=2, validators=[DataRequired(), NumberRange(min=0)])

class InvoiceForm(FlaskForm):
    items = FieldList(FormField(InvoiceItemForm), min_entries=1)
    submit = SubmitField('Generate Invoice')
class InvoiceFinalizeForm(FlaskForm):
    submit = SubmitField("Generate Customer Invoice(s)")




class PaymentForm(FlaskForm):
    invoice_id = HiddenField("Invoice ID", validators=[DataRequired()])
    user_id = HiddenField("User ID")

    # Main amount – your DB field is amount_jmd
    amount_jmd = DecimalField(
        "Amount (JMD)",
        places=2,
        validators=[
            DataRequired(message="Please enter the payment amount."),
            NumberRange(min=0.01, message="Amount must be greater than 0."),
        ],
    )

    # This maps directly to Payment.method
    method = SelectField(
        "Payment Method",
        choices=[
            ("Cash", "Cash"),
            ("Card", "Card"),
            ("Bank", "Bank Transfer / Deposit"),
            ("Wallet", "Wallet"),
        ],
        default="Cash",
        validators=[DataRequired()],
    )

    # We *collect* who authorised it; we’ll store it in notes for now
    authorized_by = SelectField(
        "Authorised By",
        choices=[],          # populate in the view from your staff/admin list
        validators=[Optional()],
    )

    reference = StringField(
        "Reference / Receipt #",
        validators=[Optional(), Length(max=100)],
    )

    notes = TextAreaField(
        "Notes",
        validators=[Optional(), Length(max=255)],
    )

    submit = SubmitField("Add Payment")

class ExpenseForm(FlaskForm):
    amount = DecimalField('Amount', places=2, validators=[DataRequired()])
    category = SelectField(
        'Category',
        choices=[
            ('Rent', 'Rent'),
            ('Salaries', 'Salaries'),
            ('Promotion', 'Promotion'),
            ('Utilities', 'Utilities'),
            ('Supplies', 'Supplies'),
            ('Other', 'Other')
        ],
        validators=[DataRequired()]
    )
    description = TextAreaField('Description', validators=[Optional()])
    date = DateField('Date', format='%Y-%m-%d', validators=[DataRequired()])
    submit = SubmitField('Add Expense')  # ✅ Added

class AdminProfileForm(FlaskForm):
    name = StringField('Full Name', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[Optional()])  # Optional: leave blank to keep current
    submit = SubmitField('Update Profile')

class WalletUpdateForm(FlaskForm):
    ewallet_balance = DecimalField('Wallet Balance', places=2, validators=[DataRequired()])
    description = StringField('Description (optional)')
    submit = SubmitField('Update Wallet')

class PreAlertForm(FlaskForm):
    vendor_name = StringField('Vendor Name', validators=[DataRequired()])
    courier_name = StringField('Courier Name', validators=[DataRequired()])
    tracking_number = StringField('Tracking Number', validators=[DataRequired()])
    purchase_date = DateField('Purchase Date', validators=[DataRequired()])
    package_contents = StringField('Package Contents', validators=[DataRequired()])
    item_value_usd = DecimalField('Item Value (USD)', validators=[DataRequired(), NumberRange(min=0)])
    invoice = FileField('Upload Invoice', validators=[
        FileAllowed(['pdf', 'jpg', 'jpeg', 'png'], 'Only PDF/JPG/PNG files are allowed')
    ])
    submit = SubmitField('Submit')


class PackageUpdateForm(FlaskForm):
    pkg_id = HiddenField()  # <-- add this hidden field to track which package
    declared_value = DecimalField(
        'Declared Value (USD)',
        validators=[Optional(), NumberRange(min=0)],
        places=2
    )
    invoice_file = FileField(
        'Upload Invoice',
        validators=[Optional(), FileAllowed(['pdf', 'jpg', 'jpeg', 'png'], 'PDF/JPG/PNG only')]
    )
    submit = SubmitField('Submit')

class CalculatorForm(FlaskForm):
    category = SelectField("Category", choices=[(c, c) for c in categories], validators=[DataRequired()])
    invoice_usd = DecimalField("Item Value (USD)", validators=[DataRequired(), NumberRange(min=0.01)])
    weight = DecimalField("Weight (lbs)", validators=[DataRequired(), NumberRange(min=1)])
    submit = SubmitField("Calculate")

class AdminCalculatorForm(FlaskForm):
    category = SelectField("Category", choices=[(c, c) for c in CATEGORIES.keys()], validators=[DataRequired()])
    invoice_usd = DecimalField("Item Value (USD)", validators=[DataRequired(), NumberRange(min=0.01)])
    weight = DecimalField("Weight (lbs)", validators=[DataRequired(), NumberRange(min=1)])
    submit = SubmitField("Calculate")

class ReferralForm(FlaskForm):
    friend_email = StringField("Friend's Email", validators=[DataRequired(), Email()])
    submit = SubmitField("Send Invite")