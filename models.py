from datetime import date, datetime
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
    # RUT tributario de la empresa. Se guarda formateado (XX.XXX.XXX-X).
    # Es nullable para permitir la migración segura de empresas creadas antes
    # de que este campo existiera. Las nuevas empresas sí lo reciben desde /registro.
    rut = db.Column(db.String(20), unique=True, nullable=True)
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

    # Recuperación de contraseña
    reset_token = db.Column(db.String(100), unique=True)
    reset_token_expires = db.Column(db.DateTime)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class FlowSubscription(db.Model):
    __tablename__ = "flow_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), unique=True, nullable=False)
    customer_id = db.Column(db.String(100), unique=True)
    subscription_id = db.Column(db.String(100), unique=True)
    plan_key = db.Column(db.String(30), default="free", nullable=False)
    pending_plan_key = db.Column(db.String(30))
    card_registered = db.Column(db.Boolean, default=False, nullable=False)
    card_brand = db.Column(db.String(30))
    card_last4 = db.Column(db.String(4))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    company = db.relationship("Company", backref=db.backref("flow_subscription", uselist=False))


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    subscription_id = db.Column(db.String(100))
    flow_order = db.Column(db.String(100), unique=True)
    commerce_order = db.Column(db.String(255))
    amount = db.Column(db.Float, default=0.0, nullable=False)
    status = db.Column(db.String(30), default="pending", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    company = db.relationship("Company", backref="invoices")

    def plan_label(self):
        labels = {"free": "Free", "business": "Business", "pro": "Pro"}
        sub = FlowSubscription.query.filter_by(company_id=self.company_id).first()
        return labels.get(sub.plan_key if sub else "free", "Free")


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
