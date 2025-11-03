# update_password.py
from app import create_app, db
from app.models import User
import bcrypt

# 1️⃣ Create your Flask app
app = create_app()

# 2️⃣ Push an application context
with app.app_context():
    # 3️⃣ Fetch the admin user
    admin = User.query.filter_by(email='foreignafootlogistics@gmail.com').first()

    if not admin:
        print("Admin user not found!")
    else:
        # 4️⃣ Set new password (replace 'MyNewAdminPassword' with your password)
        new_password = b'MyNewAdminPassword'
        admin.password = bcrypt.hashpw(new_password, bcrypt.gensalt())
        admin.role = 'admin'        # make sure the role is admin
        admin.is_admin = True       # also mark the flag if you use it

        # 5️⃣ Commit changes
        db.session.commit()
        print("Admin password updated successfully!")
