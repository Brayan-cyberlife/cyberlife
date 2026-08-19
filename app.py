import csv
import io
import os
import re
from functools import wraps
from datetime import datetime

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

from models import db, Company, User, Asset, CATEGORIES, STATUSES


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

# Inicializar SQLAlchemy y crear las tablas si todavía no existen.
db.init_app(app)
with app.app_context():
    db.create_all()


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
# SUSCRIPCIÓN
# ============================================================

# Catálogo visual de planes.
# La integración de pagos con Flow todavía no está conectada al modelo
# de datos, así que esta pantalla no depende de campos que aún no existen
# en Company (como plan o card_registered) ni de un modelo Invoice.
PLANS = {
    "free": {
        "label": "Free",
        "price": 0,
        "features": [
            "Gestión básica de activos",
            "Panel de inventario",
            "Exportación CSV",
        ],
    },
    "business": {
        "label": "Business",
        "price": None,
        "features": [
            "Todas las funciones de Free",
            "Funciones avanzadas para equipos",
            "Soporte prioritario",
        ],
    },
    "pro": {
        "label": "Pro",
        "price": None,
        "features": [
            "Todas las funciones de Business",
            "Funciones avanzadas y mayor capacidad",
            "Soporte preferente",
        ],
    },
}


@app.route("/cuenta/suscripcion")
@login_required
def subscription():
    # Estado seguro hasta implementar persistencia de suscripciones y Flow.
    current_plan_key = "free"
    current_plan = PLANS[current_plan_key]

    return render_template(
        "subscription.html",
        plans=PLANS,
        current_plan=current_plan,
        current_plan_key=current_plan_key,
        flow_configured=False,
        card_registered=False,
        invoices=[],
    )


# ============================================================
# ARRANQUE LOCAL
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
