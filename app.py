import csv
import io
import os
import re
import hmac
import hashlib
import secrets
import smtplib
from email.mime.text import MIMEText
import requests
from urllib.parse import urlencode
from functools import wraps
from datetime import datetime, timedelta

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    Response,
    abort,
)
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    login_required,
    current_user,
)

from models import db, Company, User, Asset, FlowSubscription, Invoice, CATEGORIES, STATUSES


app = Flask(
    __name__,
    template_folder=".",
    static_folder=".",
    static_url_path="/static",
)

# Base de datos: en Render usamos PostgreSQL mediante DATABASE_URL.
# En local, si DATABASE_URL no existe, se mantiene SQLite como respaldo.
database_url = os.environ.get("DATABASE_URL", "").strip()
if database_url:
    # Render puede entregar postgres:// o postgresql://;
    # forzamos el driver Psycopg 3 para evitar que SQLAlchemy intente
    # cargar el antiguo psycopg2.
    database_url = re.sub(r"^postgres(?:ql)?://", "postgresql+psycopg://", database_url)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///inventory.db"

app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "cambia-esta-clave-en-produccion"
)
app.config["OWNER_EMAIL"] = os.environ.get("OWNER_EMAIL", "").strip().lower()

# Envío de correo (recuperación de contraseña). Funciona con cualquier SMTP
# estándar — por ejemplo el Google Workspace de cyberlife.cl con una
# "contraseña de aplicación" generada desde la cuenta de Google.
app.config["MAIL_SERVER"] = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
app.config["MAIL_PORT"] = int(os.environ.get("MAIL_PORT", "587"))
app.config["MAIL_USERNAME"] = os.environ.get("MAIL_USERNAME", "")
app.config["MAIL_PASSWORD"] = os.environ.get("MAIL_PASSWORD", "")
app.config["MAIL_DEFAULT_SENDER"] = os.environ.get(
    "MAIL_DEFAULT_SENDER", app.config["MAIL_USERNAME"]
)


def mail_configured():
    return bool(app.config["MAIL_USERNAME"] and app.config["MAIL_PASSWORD"])


def send_email(to, subject, body):
    if not mail_configured():
        raise RuntimeError("El envío de correo no está configurado (faltan MAIL_USERNAME/MAIL_PASSWORD).")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = app.config["MAIL_DEFAULT_SENDER"]
    msg["To"] = to
    with smtplib.SMTP(app.config["MAIL_SERVER"], app.config["MAIL_PORT"]) as server:
        server.starttls()
        server.login(app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"])
        server.sendmail(app.config["MAIL_DEFAULT_SENDER"], [to], msg.as_string())

# ============================================================
# FLOW — SUSCRIPCIONES
# ============================================================
FLOW_API_KEY = os.environ.get("FLOW_API_KEY", "").strip()
FLOW_SECRET_KEY = os.environ.get("FLOW_SECRET_KEY", "").strip()
FLOW_PLAN_BUSINESS = os.environ.get("FLOW_PLAN_BUSINESS", "").strip()
FLOW_PLAN_PRO = os.environ.get("FLOW_PLAN_PRO", "").strip()
FLOW_BASE_URL = os.environ.get("FLOW_BASE_URL", "https://www.flow.cl/api").strip().rstrip("/")

# Precios son solo visuales; el monto real de la suscripción lo define el plan en Flow.
try:
    FLOW_PRICE_BUSINESS = float(os.environ.get("FLOW_PRICE_BUSINESS", "0") or 0) or None
except ValueError:
    FLOW_PRICE_BUSINESS = None
try:
    FLOW_PRICE_PRO = float(os.environ.get("FLOW_PRICE_PRO", "0") or 0) or None
except ValueError:
    FLOW_PRICE_PRO = None


def flow_configured():
    return bool(FLOW_API_KEY and FLOW_SECRET_KEY and FLOW_PLAN_BUSINESS and FLOW_PLAN_PRO)


def flow_signature(params):
    """Firma Flow: parámetros ordenados alfabéticamente + HMAC-SHA256."""
    keys = sorted(k for k in params if k != "s")
    payload = "".join(f"{k}{params[k]}" for k in keys)
    return hmac.new(FLOW_SECRET_KEY.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def flow_request(method, path, params=None):
    if not flow_configured():
        raise RuntimeError("Las credenciales de Flow no están configuradas.")
    params = dict(params or {})
    params["apiKey"] = FLOW_API_KEY
    params["s"] = flow_signature(params)
    url = f"{FLOW_BASE_URL}{path}"
    if method.upper() == "GET":
        response = requests.get(url, params=params, timeout=20)
    else:
        response = requests.post(url, data=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("code") and data.get("code") not in ("200", 200):
        raise RuntimeError(data.get("message") or "Flow rechazó la solicitud.")
    return data


def flow_error_message(exc):
    text = str(exc)
    return text[:500] if text else "Error desconocido al comunicarse con Flow."


PLANS = {
    "free": {
        "label": "Free", "price": 0, "flow_plan_id": None, "asset_limit": 10,
        "features": ["Hasta 10 activos", "Panel de inventario", "Exportación CSV"],
    },
    "business": {
        "label": "Business", "price": FLOW_PRICE_BUSINESS, "flow_plan_id": FLOW_PLAN_BUSINESS, "asset_limit": None,
        "features": ["Activos ilimitados", "Funciones avanzadas para equipos", "Soporte prioritario"],
    },
    "pro": {
        "label": "Pro", "price": FLOW_PRICE_PRO, "flow_plan_id": FLOW_PLAN_PRO, "asset_limit": 20,
        "features": ["Hasta 20 activos", "Funciones avanzadas y mayor capacidad", "Soporte preferente"],
    },
}

# Inicializar SQLAlchemy y crear las tablas si todavía no existen.
db.init_app(app)
with app.app_context():
    db.create_all()

    # db.create_all() NO agrega columnas nuevas a tablas que ya existen —
    # solo crea tablas que faltan por completo. Como Postgres persiste entre
    # deploys (a diferencia del SQLite anterior, que se recreaba solo), cada
    # vez que se agrega una columna a un modelo hay que sumarla acá también,
    # o el INSERT/UPDATE falla con "column ... does not exist" (500).
    if database_url:  # esta migración liviana solo aplica a Postgres
        from sqlalchemy import text

        column_additions = [
            ("users", "reset_token", "VARCHAR(100)"),
            ("users", "reset_token_expires", "TIMESTAMP"),
        ]
        try:
            with db.engine.connect() as conn:
                for table, column, coltype in column_additions:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {coltype}")
                    )
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_reset_token "
                        "ON users (reset_token) WHERE reset_token IS NOT NULL"
                    )
                )
                conn.commit()
        except Exception as exc:
            app.logger.warning(f"No se pudo verificar/migrar columnas nuevas: {exc}")


login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Inicia sesión para continuar."
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


def parse_date(value):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def assets_query():
    """Activos pertenecientes a la empresa del usuario actual."""
    return Asset.query.filter_by(company_id=current_user.company_id)


def clean_rut(value):
    """Deja solo dígitos y K/k de un RUT (sin puntos ni guión)."""
    return re.sub(r"[^0-9kK]", "", value or "").upper()


def format_rut(value):
    """Da formato XX.XXX.XXX-X a partir de un RUT limpio o parcialmente formateado."""
    clean = clean_rut(value)
    if len(clean) < 2:
        return clean
    body, dv = clean[:-1], clean[-1]
    parts = []
    while len(body) > 3:
        parts.insert(0, body[-3:])
        body = body[:-3]
    if body:
        parts.insert(0, body)
    return ".".join(parts) + "-" + dv


def is_valid_rut(value):
    """Valida el dígito verificador de un RUT chileno (algoritmo módulo 11)."""
    clean = clean_rut(value)
    if len(clean) < 2:
        return False
    body, dv = clean[:-1], clean[-1]
    if not body.isdigit():
        return False
    total = 0
    multiplier = 2
    for digit in reversed(body):
        total += int(digit) * multiplier
        multiplier = multiplier + 1 if multiplier < 7 else 2
    remainder = 11 - (total % 11)
    computed_dv = "0" if remainder == 11 else "K" if remainder == 10 else str(remainder)
    return computed_dv == dv


def admin_required(view_func):
    """Solo permite acceder al panel al superadministrador."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_super_admin:
            abort(403)
        return view_func(*args, **kwargs)

    return wrapped


# ============================================================
# AUTENTICACIÓN
# ============================================================

@app.route("/registro", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        company_rut = request.form.get("company_rut", "").strip()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not company_name or not company_rut or not name or not email or not password:
            flash("Completa todos los campos obligatorios.", "danger")
            return render_template("register.html")

        if not is_valid_rut(company_rut):
            flash("Ingresa un RUT de empresa válido (ej: 12.345.678-5).", "danger")
            return render_template("register.html")

        rut_formatted = format_rut(company_rut)

        if Company.query.filter_by(rut=rut_formatted).first():
            flash("Ya existe una empresa registrada con ese RUT.", "danger")
            return render_template("register.html")

        if User.query.filter_by(email=email).first():
            flash("Ya existe una cuenta con ese correo.", "danger")
            return render_template("register.html")

        company = Company(name=company_name, rut=rut_formatted)
        db.session.add(company)
        db.session.flush()

        user = User(
            company_id=company.id,
            name=name,
            email=email,
            role="admin",
        )
        user.set_password(password)

        if app.config["OWNER_EMAIL"] and email == app.config["OWNER_EMAIL"]:
            user.is_super_admin = True

        db.session.add(user)
        db.session.commit()

        login_user(user)
        flash(f"Cuenta creada. Bienvenido a Cyber Life, {name}.", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            if (
                not user.is_super_admin
                and user.company
                and not user.company.is_active
            ):
                flash(
                    "Esta cuenta está desactivada. Contacta al administrador.",
                    "danger",
                )
                return render_template("login.html")

            login_user(user)
            return redirect(url_for("dashboard"))

        flash("Correo o contraseña incorrectos.", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/recuperar", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()

        # Mismo mensaje exista o no el correo, para no revelar qué cuentas existen.
        generic_msg = "Si el correo está registrado, te enviamos un enlace para restablecer tu contraseña."

        if user:
            user.reset_token = secrets.token_urlsafe(32)
            user.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
            db.session.commit()

            reset_url = url_for("reset_password", token=user.reset_token, _external=True)
            body = (
                f"Hola {user.name},\n\n"
                "Recibimos una solicitud para restablecer tu contraseña en Cyber Life.\n"
                f"Si fuiste tú, entra a este enlace (válido por 1 hora):\n{reset_url}\n\n"
                "Si no fuiste tú, puedes ignorar este correo.\n"
            )
            try:
                send_email(user.email, "Restablecer tu contraseña · Cyber Life", body)
            except Exception:
                # No revelamos el error de envío al usuario para no filtrar
                # si el correo existe o no; queda registrado igual el token.
                pass

        flash(generic_msg, "success")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/restablecer/<token>", methods=["GET", "POST"])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    user = User.query.filter_by(reset_token=token).first()
    if not user or not user.reset_token_expires or user.reset_token_expires < datetime.utcnow():
        flash("El enlace de recuperación no es válido o ya venció. Solicita uno nuevo.", "danger")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")

        if len(password) < 6:
            flash("La contraseña debe tener al menos 6 caracteres.", "danger")
            return render_template("reset_password.html", token=token)

        if password != password_confirm:
            flash("Las contraseñas no coinciden.", "danger")
            return render_template("reset_password.html", token=token)

        user.set_password(password)
        user.reset_token = None
        user.reset_token_expires = None
        db.session.commit()

        flash("Contraseña actualizada. Ya puedes iniciar sesión.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


# ============================================================
# PANEL PRINCIPAL
# ============================================================

@app.route("/")
@login_required
def dashboard():
    assets = assets_query().all()
    total = len(assets)
    total_cost = sum(a.cost or 0 for a in assets)
    expired = [a for a in assets if a.health() == "danger"]
    warning = [a for a in assets if a.health() == "warn"]
    ok = [a for a in assets if a.health() == "ok"]

    by_category = {}
    for asset in assets:
        by_category[asset.category] = by_category.get(asset.category, 0) + 1

    upcoming = sorted(
        [a for a in assets if a.health() in ("warn", "danger")],
        key=lambda a: (a.expiration_date is None, a.expiration_date),
    )[:8]

    return render_template(
        "dashboard.html",
        total=total,
        total_cost=total_cost,
        expired_count=len(expired),
        warning_count=len(warning),
        ok_count=len(ok),
        by_category=by_category,
        upcoming=upcoming,
    )


# ============================================================
# ACTIVOS
# ============================================================

@app.route("/assets")
@login_required
def list_assets():
    query = assets_query()
    category = request.args.get("category", "")
    status = request.args.get("status", "")
    q = request.args.get("q", "")

    if category:
        query = query.filter(Asset.category == category)

    if status:
        query = query.filter(Asset.status == status)

    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Asset.name.ilike(like),
                Asset.vendor.ilike(like),
                Asset.assigned_to.ilike(like),
                Asset.serial_number.ilike(like),
            )
        )

    assets = query.order_by(
        Asset.expiration_date.is_(None),
        Asset.expiration_date,
    ).all()

    return render_template(
        "assets.html",
        assets=assets,
        categories=CATEGORIES,
        statuses=STATUSES,
        filters={"category": category, "status": status, "q": q},
    )


@app.route("/assets/new", methods=["GET", "POST"])
@login_required
def new_asset():
    record = get_flow_subscription(current_user.company)
    plan_key = record.plan_key if record and record.plan_key in PLANS else "free"
    asset_limit = PLANS[plan_key]["asset_limit"]
    if asset_limit is not None:
        current_count = Asset.query.filter_by(company_id=current_user.company_id).count()
        if current_count >= asset_limit:
            flash(
                f"Llegaste al límite de {asset_limit} activos del plan {PLANS[plan_key]['label']}. "
                "Sube de plan para agregar más.",
                "warning",
            )
            return redirect(url_for("subscription"))

    if request.method == "POST":
        try:
            cost = float(request.form.get("cost") or 0)
            purchase_date = parse_date(request.form.get("purchase_date"))
            expiration_date = parse_date(request.form.get("expiration_date"))
        except (ValueError, TypeError):
            flash("Revisa las fechas y el costo ingresado.", "danger")
            return render_template(
                "asset_form.html",
                asset=None,
                categories=CATEGORIES,
                statuses=STATUSES,
            )

        asset = Asset(
            company_id=current_user.company_id,
            name=request.form.get("name", "").strip(),
            category=request.form.get("category", "Hardware"),
            vendor=request.form.get("vendor"),
            assigned_to=request.form.get("assigned_to"),
            serial_number=request.form.get("serial_number"),
            purchase_date=purchase_date,
            expiration_date=expiration_date,
            cost=cost,
            status=request.form.get("status", "Activo"),
            notes=request.form.get("notes"),
        )

        db.session.add(asset)
        db.session.commit()
        flash(f'Activo "{asset.name}" agregado correctamente.', "success")
        return redirect(url_for("list_assets"))

    return render_template(
        "asset_form.html",
        asset=None,
        categories=CATEGORIES,
        statuses=STATUSES,
    )


@app.route("/assets/<int:asset_id>/edit", methods=["GET", "POST"])
@login_required
def edit_asset(asset_id):
    asset = assets_query().filter_by(id=asset_id).first_or_404()

    if request.method == "POST":
        try:
            asset.purchase_date = parse_date(request.form.get("purchase_date"))
            asset.expiration_date = parse_date(request.form.get("expiration_date"))
            asset.cost = float(request.form.get("cost") or 0)
        except (ValueError, TypeError):
            flash("Revisa las fechas y el costo ingresado.", "danger")
            return render_template(
                "asset_form.html",
                asset=asset,
                categories=CATEGORIES,
                statuses=STATUSES,
            )

        asset.name = request.form.get("name", "").strip()
        asset.category = request.form.get("category", "Hardware")
        asset.vendor = request.form.get("vendor")
        asset.assigned_to = request.form.get("assigned_to")
        asset.serial_number = request.form.get("serial_number")
        asset.status = request.form.get("status", "Activo")
        asset.notes = request.form.get("notes")

        db.session.commit()
        flash(f'Activo "{asset.name}" actualizado.', "success")
        return redirect(url_for("list_assets"))

    return render_template(
        "asset_form.html",
        asset=asset,
        categories=CATEGORIES,
        statuses=STATUSES,
    )


@app.route("/assets/<int:asset_id>/delete", methods=["POST"])
@login_required
def delete_asset(asset_id):
    asset = assets_query().filter_by(id=asset_id).first_or_404()
    name = asset.name
    db.session.delete(asset)
    db.session.commit()
    flash(f'Activo "{name}" eliminado.', "info")
    return redirect(url_for("list_assets"))


@app.route("/export.csv")
@login_required
def export_csv():
    assets = assets_query().all()
    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "Nombre",
        "Categoría",
        "Proveedor",
        "Asignado a",
        "N° Serie",
        "Fecha compra",
        "Fecha vencimiento",
        "Costo",
        "Estado",
        "Notas",
    ])

    for asset in assets:
        writer.writerow([
            asset.name,
            asset.category,
            asset.vendor,
            asset.assigned_to,
            asset.serial_number,
            asset.purchase_date,
            asset.expiration_date,
            asset.cost,
            asset.status,
            asset.notes,
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
            "attachment;filename=inventario_activos.csv"
        },
    )


# ============================================================
# PANEL DE ADMINISTRADOR / SUPERADMIN
# ============================================================

@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    search_query = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "")

    # Estadísticas generales, independientes de los filtros.
    total_companies = Company.query.count()
    total_users = User.query.count()
    total_assets = Asset.query.count()

    companies_query = Company.query

    if search_query:
        companies_query = companies_query.filter(
            Company.name.ilike(f"%{search_query}%")
        )

    if status_filter == "active":
        companies_query = companies_query.filter(Company.is_active.is_(True))
    elif status_filter == "inactive":
        companies_query = companies_query.filter(Company.is_active.is_(False))

    companies = companies_query.order_by(Company.created_at.desc()).all()

    rows = []
    for company in companies:
        rows.append({
            "company": company,
            "user_count": len(company.users),
            "asset_count": len(company.assets),
            "total_value": sum(asset.cost or 0 for asset in company.assets),
        })

    return render_template(
        "admin_dashboard.html",
        rows=rows,
        total_companies=total_companies,
        total_users=total_users,
        total_assets=total_assets,
    )


@app.route("/admin/empresas/nueva", methods=["GET", "POST"])
@login_required
@admin_required
def create_company():
    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        company_rut = request.form.get("company_rut", "").strip()
        admin_name = request.form.get("admin_name", "").strip()
        admin_email = request.form.get("admin_email", "").strip().lower()
        admin_password = request.form.get("admin_password", "")

        if not company_name or not company_rut or not admin_name or not admin_email or not admin_password:
            flash("Completa todos los campos obligatorios.", "danger")
            return render_template("create_company.html")

        if not is_valid_rut(company_rut):
            flash("Ingresa un RUT de empresa válido (ej: 12.345.678-5).", "danger")
            return render_template("create_company.html")

        rut_formatted = format_rut(company_rut)

        existing_company = Company.query.filter(
            db.func.lower(Company.name) == company_name.lower()
        ).first()

        if existing_company:
            flash("Ya existe una empresa con ese nombre.", "danger")
            return render_template("create_company.html")

        if Company.query.filter_by(rut=rut_formatted).first():
            flash("Ya existe una empresa registrada con ese RUT.", "danger")
            return render_template("create_company.html")

        if User.query.filter_by(email=admin_email).first():
            flash("Ya existe una cuenta con ese correo.", "danger")
            return render_template("create_company.html")

        company = Company(name=company_name, rut=rut_formatted, is_active=True)
        db.session.add(company)
        db.session.flush()

        admin_user = User(
            company_id=company.id,
            name=admin_name,
            email=admin_email,
            role="admin",
        )
        admin_user.set_password(admin_password)
        db.session.add(admin_user)
        db.session.commit()

        flash(f'Empresa "{company.name}" creada correctamente, junto con su cuenta de administrador.', "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("create_company.html")


@app.route("/admin/empresas/<int:company_id>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_company(company_id):
    company = Company.query.get_or_404(company_id)

    # Evitar que el superadmin se desactive a sí mismo.
    if company.id == current_user.company_id:
        flash("No puedes desactivar tu propia empresa desde este panel.", "warning")
        return redirect(url_for("admin_dashboard"))

    company.is_active = not company.is_active
    db.session.commit()

    estado = "activada" if company.is_active else "desactivada"
    flash(f'Empresa "{company.name}" {estado}.', "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/empresas/<int:company_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_company(company_id):
    company = Company.query.get_or_404(company_id)

    # Evitar borrar accidentalmente la empresa del superadmin actual.
    if company.id == current_user.company_id:
        flash("No puedes eliminar tu propia empresa desde este panel.", "warning")
        return redirect(url_for("admin_dashboard"))

    company_name = company.name
    db.session.delete(company)
    db.session.commit()

    flash(f'Empresa "{company_name}" eliminada correctamente.', "info")
    return redirect(url_for("admin_dashboard"))



# ============================================================
# CUENTA
# ============================================================

@app.route("/cuenta/perfil", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()

        if not name or not email:
            flash("El nombre y el correo son obligatorios.", "danger")
            return render_template("profile.html")

        existing_user = User.query.filter(
            User.email == email,
            User.id != current_user.id,
        ).first()

        if existing_user:
            flash("Ese correo ya está asociado a otra cuenta.", "danger")
            return render_template("profile.html")

        current_user.name = name
        current_user.email = email
        db.session.commit()

        flash("Perfil actualizado correctamente.", "success")
        return redirect(url_for("profile"))

    return render_template("profile.html")


@app.route("/cuenta/ajustes", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_user.check_password(current_password):
            flash("La contraseña actual no es correcta.", "danger")
            return render_template("settings.html")

        if len(new_password) < 8:
            flash("La nueva contraseña debe tener al menos 8 caracteres.", "danger")
            return render_template("settings.html")

        if new_password != confirm_password:
            flash("Las contraseñas nuevas no coinciden.", "danger")
            return render_template("settings.html")

        current_user.set_password(new_password)
        db.session.commit()

        flash("Contraseña actualizada correctamente.", "success")
        return redirect(url_for("settings"))

    return render_template("settings.html")


# ============================================================
# SUSCRIPCIÓN + FLOW
# ============================================================

def get_flow_subscription(company):
    return FlowSubscription.query.filter_by(company_id=company.id).first()


def ensure_flow_customer(company, user):
    record = get_flow_subscription(company)
    if record and record.customer_id:
        return record

    data = flow_request("POST", "/customer/create", {
        "name": user.name,
        "email": user.email,
        "externalId": f"cyberlife-company-{company.id}",
    })
    customer_id = data.get("customerId")
    if not customer_id:
        raise RuntimeError("Flow no devolvió customerId al crear el cliente.")

    if not record:
        record = FlowSubscription(company_id=company.id)
        db.session.add(record)
    record.customer_id = customer_id
    db.session.commit()
    return record


def create_flow_subscription(record, plan_key):
    plan = PLANS[plan_key]
    if not plan["flow_plan_id"]:
        raise RuntimeError("El plan seleccionado no tiene un plan asociado en Flow.")
    data = flow_request("POST", "/subscription/create", {
        "planId": plan["flow_plan_id"],
        "customerId": record.customer_id,
    })
    subscription_id = data.get("subscriptionId")
    if not subscription_id:
        raise RuntimeError("Flow no devolvió subscriptionId al crear la suscripción.")
    record.plan_key = plan_key
    record.subscription_id = subscription_id
    record.card_registered = True
    record.pending_plan_key = None
    db.session.commit()
    return data


@app.route("/cuenta/suscripcion")
@login_required
def subscription():
    record = get_flow_subscription(current_user.company)
    current_plan_key = record.plan_key if record and record.plan_key in PLANS else "free"
    current_plan = PLANS[current_plan_key]
    invoices = Invoice.query.filter_by(company_id=current_user.company_id).order_by(Invoice.created_at.desc()).limit(20).all()
    return render_template(
        "subscription.html",
        plans=PLANS,
        current_plan=current_plan,
        current_plan_key=current_plan_key,
        flow_configured=flow_configured(),
        card_registered=bool(record and record.card_registered),
        invoices=invoices,
        flow_environment="sandbox" if "sandbox.flow.cl" in FLOW_BASE_URL else "production",
    )


@app.route("/cuenta/suscripcion/choose/<plan_key>", methods=["POST"])
@login_required
def subscription_choose(plan_key):
    if plan_key not in PLANS:
        abort(404)

    if plan_key == "free":
        flash("El plan Free se mantiene disponible. Para cancelar una suscripción pagada, gestionaremos la baja en Flow en una próxima etapa.", "info")
        return redirect(url_for("subscription"))

    if not flow_configured():
        flash("Flow todavía no está configurado en este ambiente. Revisa FLOW_API_KEY, FLOW_SECRET_KEY, FLOW_PLAN_BUSINESS y FLOW_PLAN_PRO.", "warning")
        return redirect(url_for("subscription"))

    company = current_user.company
    try:
        record = ensure_flow_customer(company, current_user)
        if record.card_registered and record.customer_id:
            create_flow_subscription(record, plan_key)
            flash(f"Suscripción {PLANS[plan_key]['label']} creada correctamente en Flow.", "success")
            return redirect(url_for("subscription"))

        record.pending_plan_key = plan_key
        db.session.commit()
        callback = url_for("flow_card_callback", _external=True)
        data = flow_request("POST", "/customer/register", {
            "customerId": record.customer_id,
            "url_return": callback,
        })
        flow_url = data.get("url")
        token = data.get("token")
        if not flow_url or not token:
            raise RuntimeError("Flow no devolvió la URL/token para registrar la tarjeta.")
        return redirect(f"{flow_url}?{urlencode({'token': token})}")
    except Exception as exc:
        db.session.rollback()
        flash(f"No fue posible iniciar la suscripción con Flow: {flow_error_message(exc)}", "danger")
        return redirect(url_for("subscription"))


@app.route("/flow/card-callback", methods=["POST", "GET"])
def flow_card_callback():
    token = request.values.get("token", "").strip()
    if not token:
        return redirect(url_for("subscription"))

    try:
        data = flow_request("GET", "/customer/getRegisterStatus", {"token": token})
        customer_id = data.get("customerId")
        status = str(data.get("status", "0"))
        if status != "1" or not customer_id:
            flash("Flow no confirmó el registro de la tarjeta. Puedes intentarlo nuevamente.", "warning")
            return redirect(url_for("subscription"))

        record = FlowSubscription.query.filter_by(customer_id=customer_id).first()
        if not record:
            flash("No encontramos la cuenta Cyber Life asociada al registro de Flow.", "danger")
            return redirect(url_for("login"))

        record.card_registered = True
        record.card_brand = data.get("creditCardType")
        record.card_last4 = data.get("last4CardDigits")
        plan_key = record.pending_plan_key
        record.pending_plan_key = None
        db.session.commit()

        if plan_key in ("business", "pro"):
            create_flow_subscription(record, plan_key)
            flash(f"Tarjeta registrada y plan {PLANS[plan_key]['label']} activado correctamente.", "success")
        else:
            flash("Tarjeta registrada correctamente en Flow.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Flow confirmó el registro, pero Cyber Life no pudo finalizar la suscripción: {flow_error_message(exc)}", "danger")
    return redirect(url_for("subscription"))


@app.route("/flow/payment-callback", methods=["POST"])
def flow_payment_callback():
    """Callback de pagos recurrentes. Flow envía un token; luego consultamos payment/getStatus."""
    token = request.form.get("token", "").strip()
    if not token:
        return "OK", 200
    try:
        data = flow_request("GET", "/payment/getStatus", {"token": token})
        commerce_order = str(data.get("commerceOrder", ""))
        status = str(data.get("status", ""))
        # Flow recomienda usar commerceOrder para relacionar el cobro con la suscripción/importe.
        subscription_id = commerce_order.split("-")[0] if commerce_order else ""
        record = FlowSubscription.query.filter_by(subscription_id=subscription_id).first() if subscription_id else None
        if record:
            inv = Invoice(
                company_id=record.company_id,
                subscription_id=record.subscription_id,
                flow_order=str(data.get("flowOrder", "")) or None,
                amount=float(data.get("amount") or 0),
                status="paid" if status == "1" else "failed",
                commerce_order=commerce_order,
            )
            db.session.add(inv)
            db.session.commit()
    except Exception:
        db.session.rollback()
    return "OK", 200


# ============================================================
# ARRANQUE LOCAL
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
