# Cyber Life — Gestor de Inventario de Activos TI (MVP)

Panel simple para que soporte TI controle equipos, licencias y suscripciones,
con alertas visuales de vencimiento (garantías, licencias, contratos de soporte).

## Cómo correrlo

```bash
pip install -r requirements.txt
python app.py
```

Abre `http://localhost:5000`. Al iniciar por primera vez se crea `inventory.db`
(SQLite) con 6 activos de ejemplo para que el panel no se vea vacío en una demo.

## Publicar una demo en vivo (gratis, sin tarjeta)

1. Sube esta carpeta a un repositorio de GitHub (crea uno nuevo en github.com,
   luego `git init`, `git add .`, `git commit -m "mvp"`, `git push`).
2. Entra a **render.com** y crea una cuenta gratis.
3. Click en **New → Web Service**, conecta tu repositorio de GitHub.
4. Render detecta automáticamente `render.yaml` — solo confirma y dale a **Deploy**.
5. En 2-3 minutos tendrás una URL pública tipo `https://cyberlife-inventario.onrender.com`.
6. Copia esa URL y reemplázala en el botón "Ver demo en vivo" de tu landing
   (`cyberlife-demo.html`, busca el texto `TU-URL-AQUI`).

Nota: el plan free de Render "duerme" el servicio tras 15 min sin uso — la
primera visita del día tarda ~30 segundos en despertar. Para una demo real
con clientes está bien; para producción, sube al plan pago o usa Railway/Fly.io.

## Qué incluye este MVP

- **Panel (dashboard)**: total de activos, cuántos están al día / por vencer / vencidos,
  valor total del inventario, próximos vencimientos, distribución por categoría.
- **Listado de activos**: búsqueda por nombre/proveedor/responsable/N° de serie,
  filtros por categoría y estado.
- **Alta / edición / eliminación** de activos (hardware, software, licencias, suscripciones).
- **Indicador tipo LED** (verde/ámbar/rojo) según días para el vencimiento — mismo
  lenguaje visual que un técnico ya reconoce de racks y switches.
- **Exportar a CSV** para reportes rápidos o para pasar a Excel.
- **Login multi-cliente**: cada empresa que se registra tiene su propio espacio
  aislado — nadie ve los activos de otra cuenta. Al registrarse, la cuenta
  llega con 6 activos de ejemplo para que la demo no se vea vacía.

## Próximos pasos para convertirlo en un producto vendible

El multi-tenant con login ya está resuelto. Para venderlo como SaaS real a
varias pymes, en orden de prioridad, falta:

1. **Notificaciones automáticas**: un cron job (o `APScheduler`) que revise
   vencimientos a diario y mande email/WhatsApp cuando algo entra en la
   ventana de 30/15/7 días. Esto es lo que justifica el pago mensual — sin
   esto es "una planilla bonita", con esto es "un sistema que te cuida".
2. **Importación masiva**: subir un CSV/Excel con el inventario existente
   del cliente en la primera reunión de onboarding (usa la librería `openpyxl`
   o `pandas`, ya tienes el patrón de export en `export_csv()`).
3. **Roles**: administrador vs. técnico (el técnico ve y edita, el admin
   también ve costos y reportes).
4. **Cobro**: Stripe o Flow/Webpay (para Chile) con un plan simple por
   cantidad de activos (ej: hasta 50 activos gratis, luego $X por rango).
5. **Producción real**: Postgres en vez de SQLite una vez que tengas varios
   clientes activos a la vez (SQLite es perfecto para la demo gratuita en Render).

## Estructura del proyecto

```
it-inventory/
├── app.py               # rutas, autenticación y lógica de negocio
├── models.py             # modelos: Company, User, Asset
├── requirements.txt
├── Procfile               # comando de arranque para Render/Railway
├── render.yaml            # despliegue de un clic en Render
├── templates/
│   ├── base.html           # layout con sidebar
│   ├── login.html
│   ├── register.html
│   ├── dashboard.html
│   ├── assets.html
│   └── asset_form.html
└── static/
    ├── style.css            # identidad visual propia
    └── logo.png              # tu logo
```
