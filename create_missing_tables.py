from app import create_app, db
from app import models  # ensures all your models are loaded
from sqlalchemy import inspect

app = create_app()

with app.app_context():
    db.create_all()  # creates any missing tables
    print("✅ All missing tables have been created successfully.")

    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    print("Current tables in the database:")
    for table in tables:
        print(f" - {table}")
