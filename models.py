from datetime import date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

CATEGORIES = ["Hardware", "Software", "Licencia", "Servicio/Suscripción"]
STATUSES = ["Activo", "En reparación", "De baja", "En bodega"]


class Company(db.Model):
    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    rut = db.Column(db.String(20), unique=True, nullable=False)
    created_at = db.Column(db.Date, default=date.today)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    users = db.relationship("User", backref="company", cascade="all, delete-orphan")
    assets = db.relationship("Asset", backref="company", cascade="all, delete-orphan")


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="admin")  # admin | tecnico
    is_super_admin = db.Column(db.Boolean, default=False, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Asset(db.Model):
    __tablename__ = "assets"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(50), nullable=False, default="Hardware")
    vendor = db.Column(db.String(120))
    assigned_to = db.Column(db.String(120))
    serial_number = db.Column(db.String(120))
    purchase_date = db.Column(db.Date)
    expiration_date = db.Column(db.Date)  # garantía o vencimiento de licencia
    cost = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(50), nullable=False, default="Activo")
    notes = db.Column(db.Text)

    def days_to_expire(self):
        if not self.expiration_date:
            return None
        return (self.expiration_date - date.today()).days

    def health(self):
        """Devuelve el estado tipo LED: ok / warn / danger / none"""
        d = self.days_to_expire()
        if d is None:
            return "none"
        if d < 0:
            return "danger"
        if d <= 30:
            return "warn"
        return "ok"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "vendor": self.vendor,
            "assigned_to": self.assigned_to,
            "serial_number": self.serial_number,
            "purchase_date": self.purchase_date.isoformat() if self.purchase_date else "",
            "expiration_date": self.expiration_date.isoformat() if self.expiration_date else "",
            "cost": self.cost,
            "status": self.status,
            "notes": self.notes,
            "health": self.health(),
            "days_to_expire": self.days_to_expire(),
        }
