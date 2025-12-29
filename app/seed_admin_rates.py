# app/seed_admin_rates.py
from app import create_app
from app.extensions import db
from app.models import AdminRate

rates = [
    (1, 550), (2, 950), (3, 1350), (4, 1750), (5, 2000),
    (6, 2400), (7, 2800), (8, 3000), (9, 3400), (10, 3800),
    (11, 4200), (12, 4800), (13, 5000), (14, 5400), (15, 5600),
    (16, 6000), (17, 6400), (18, 6800), (19, 7200), (20, 7400),
    (21, 7800), (22, 8200), (23, 8600), (24, 8800), (25, 9200),
    (26, 9400), (27, 9800), (28, 10200), (29, 10400), (30, 10800),
    (31, 11200), (32, 11600), (33, 12000), (34, 12400), (35, 12800),
    (36, 13200), (37, 13600), (38, 14000), (39, 14400), (40, 14800),
    (41, 15200), (42, 15600), (43, 16000), (44, 16400), (45, 16800),
    (46, 17200), (47, 17600), (48, 18000), (49, 18400), (50, 18800),
]

def run():
    app = create_app()
    with app.app_context():
        db.session.query(AdminRate).delete()  # wipe old brackets
        for w, r in rates:
            db.session.add(AdminRate(max_weight=w, rate=r))
        db.session.commit()
        print("✅ AdminRate (rate_brackets) seeded successfully")

if __name__ == "__main__":
    run()
