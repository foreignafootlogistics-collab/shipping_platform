from app.models import CalculatorLog
from app import db

# check columns of the table
columns = [c.name for c in CalculatorLog.__table__.columns]
print(columns)

