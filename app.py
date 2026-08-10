import csv
import io
import os
from functools import wraps
from datetime import datetime, date

import click
from flask import Flask, render_template, request, redirect, url_for, flash, Response, abort
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from models import db, Company, User, Asset, CATEGORIES, STATUSES

app = Flask(__name__, template_folder=".", static_folder=".", static_url_path="/static")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///inventory.db"
app.config["SECRET_KEY"] = "cambia-esta-clave-en-produccion"
# El correo que pongas en esta variable de entorno queda marcado como superadmin
# automáticamente al registrarse. Configúrala en Render (Environment) con tu email.
app.config["OWNER_EMAIL"] = os.environ.get("OWNER_EMAIL", "").strip().lower()
db.init_app(app)

# Crear automáticamente las tablas si la base de datos está vacía en Render.
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
    """Todas las consultas de activos quedan acotadas a la empresa del usuario logueado."""
    return Asset.query.filter_by(company_id=current_user.company_id)


def admin_required(view_func):
    """Solo deja pasar al superadmin (vos). El resto recibe 403."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_super_admin:
            abort(403)
        return view_func(*args, **kwargs)
    return wrapped


# ---------- Autenticación ----------

@app.route("/registro", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        company_name = request.form["company_name"].strip()
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        if User.query.filter_by(email=email).first():
            flash("Ya existe una cuenta con ese correo.", "danger")
            return render_template("register.html")

        company = Company(name=company_name)
        db.session.add(company)
        db.session.flush()  # para obtener company.id antes del commit

        user = User(company_id=company.id, name=name, email=email, role="admin")
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
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            if not user.is_super_admin and user.company and not user.company.is_active:
                flash("Esta cuenta está desactivada. Contacta al administrador.", "danger")
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


# ---------- Panel ----------

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
    for a in assets:
        by_category[a.category] = by_category.get(a.category, 0) + 1

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

    assets = query.order_by(Asset.expiration_date.is_(None), Asset.expiration_date).all()
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
        asset = Asset(
            company_id=current_user.company_id,
            name=request.form["name"],
            category=request.form["category"],
            vendor=request.form.get("vendor"),
            assigned_to=request.form.get("assigned_to"),
            serial_number=request.form.get("serial_number"),
            purchase_date=parse_date(request.form.get("purchase_date")),
            expiration_date=parse_date(request.form.get("expiration_date")),
            cost=float(request.form.get("cost") or 0),
            status=request.form.get("status", "Activo"),
            notes=request.form.get("notes"),
        )
        db.session.add(asset)
        db.session.commit()
        flash(f'Activo "{asset.name}" agregado correctamente.', "success")
        return redirect(url_for("list_assets"))

    return render_template("asset_form.html", asset=None, categories=CATEGORIES, statuses=STATUSES)


@app.route("/assets/<int:asset_id>/edit", methods=["GET", "POST"])
@login_required
def edit_asset(asset_id):
    asset = assets_query().filter_by(id=asset_id).first_or_404()
    if request.method == "POST":
        asset.name = request.form["name"]
        asset.category = request.form["category"]
        asset.vendor = request.form.get("vendor")
        asset.assigned_to = request.form.get("assigned_to")
        asset.serial_number = request.form.get("serial_number")
        asset.purchase_date = parse_date(request.form.get("purchase_date"))
        asset.expiration_date = parse_date(request.form.get("expiration_date"))
        asset.cost = float(request.form.get("cost") or 0)
        asset.status = request.form.get("status", "Activo")
        asset.notes = request.form.get("notes")
        db.session.commit()
        flash(f'Activo "{asset.name}" actualizado.', "success")
        return redirect(url_for("list_assets"))

    return render_template("asset_form.html", asset=asset, categories=CATEGORIES, statuses=STATUSES)


@app.route("/assets/<int:asset_id>/delete", methods=["POST"])
@login_required
def delete_asset(asset_id):
    asset = assets_query().filter_by(id=asset_id).first_or_404()
    db.session.delete(asset)
    db.session.commit()
    flash(f'Activo "{asset.name}" eliminado.', "info")
    return redirect(url_for("list_assets"))


@app.route("/export.csv")
@login_required
def export_csv():
    assets = assets_query().all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        ["Nombre", "Categoría", "Proveedor", "Asignado a", "N° Serie",
         "Fecha compra", "Fecha vencimiento", "Costo", "Estado", "Notas"]
    )
    for a in assets:
        writer.writerow([
            a.name, a.category, a.vendor, a.assigned_to, a.serial_number,
            a.purchase_date, a.expiration_date, a.cost, a.status, a.notes,
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=inventario_activos.csv"},
    )


# ---------- Panel de administrador (superadmin) ----------

@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    # Capturar parámetros de filtrado desde el HTML
    search_query = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "")

    companies_query = Company.query

    if search_query:
        companies_query = companies_query.filter(Company.name.ilike(f"%{search_query}%"))
    
    if status_filter == "active":
        companies_query = companies_query.filter(Company.is_active == True)
    elif status_filter == "inactive":
        companies_query = companies_query.filter(Company.is_active == False)

    companies = companies_query.order_by(Company.created_at.desc()).all()
    
    rows = []
    for c in companies:
        rows.append({
            "company": c,
            "user_count": len(c.users),
            "asset_count": len(c.assets),
            "total_value": sum(a.cost or 0 for a in c.assets),
        })
        
    return render_template(
        "admin_dashboard.html",
        rows=rows,

        )
