from datetime import date, datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

CATEGORIES = ["Hardware", "Software", "Licencia", "Servicio/Suscripción"]
STATUSES = ["Activo", "En reparación", "De baja", "En bodega"]

# ------------------------------------------------------------------
# Planes de suscripción
# ------------------------------------------------------------------
# El "flow_plan_id" se completa una vez que el plan se crea en el
# panel de Flow (Planes de Suscripción) o vía API — se deja vacío
# por defecto y se puede configurar con variables de entorno
# FLOW_PLAN_PRO / FLOW_PLAN_BUSINESS sin tocar este archivo.
PLANS = {
    "free": {
        "label": "Free",
        "price": 0,
        "asset_limit": 15,
        "user_limit": 1,
        "features": [
            "Hasta 15 activos",
            "1 usuario",
            "Alertas de vencimiento",
            "Exportación CSV",
        ],
    },
    "pro": {
        "label": "Pro",
        "price": 9990,
        "asset_limit": None,
        "user_limit": 5,
        "features": [
            "Activos ilimitados",
            "Hasta 5 usuarios",
            "Alertas de vencimiento",
            "Exportación CSV",
            "Soporte por correo",
        ],
    },
    "business": {
        "label": "Business",
        "price": 19990,
        "asset_limit": None,
        "user_limit": None,
        "features": [
            "Activos ilimitados",
            "Usuarios ilimitados",
            "Alertas de vencimiento",
            "Exportación CSV",
            "Soporte prioritario",
        ],
    },
}

INVOICE_STATUSES = ["pending", "paid", "failed"]


class Company(db.Model):
    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    rut = db.Column(db.String(20), unique=True, nullable=False)
    created_at = db.Column(db.Date, default=date.today)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

    # Suscripción / facturación (Flow.cl)
    plan = db.Column(db.String(20), default="free", nullable=False)
    flow_customer_id = db.Column(db.String(80))
    flow_subscription_id = db.Column(db.String(80))
    card_registered = db.Column(db.Boolean, default=False, nullable=False)
    plan_updated_at = db.Column(db.Date)

    users = db.relationship("User", backref="company", cascade="all, delete-orphan")
    assets = db.relationship("Asset", backref="company", cascade="all, delete-orphan")
    invoices = db.relationship(
        "Invoice", backref="company", cascade="all, delete-orphan",
        order_by="Invoice.created_at.desc()",
    )

    def plan_info(self):
        return PLANS.get(self.plan, PLANS["free"])


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


class Invoice(db.Model):
    """Registro local de cobros de suscripción (se completa vía Flow)."""

    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    plan = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="pending")
    flow_order = db.Column(db.String(80))
    flow_token = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime)

    def plan_label(self):
        return PLANS.get(self.plan, {}).get("label", self.plan)
