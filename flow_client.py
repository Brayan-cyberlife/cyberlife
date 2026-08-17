"""
Cliente mínimo para la API REST de Flow.cl (pagos y suscripciones).

Configura estas variables de entorno antes de usarlo:
  FLOW_API_KEY     - Api Key de tu cuenta Flow (sandbox o producción)
  FLOW_SECRET_KEY  - Secret Key de tu cuenta Flow
  FLOW_API_URL     - https://sandbox.flow.cl/api  (pruebas)
                      https://www.flow.cl/api      (producción)

Documentación oficial: https://developers.flow.cl/
"""

import hashlib
import hmac
import os

import requests

FLOW_API_KEY = os.environ.get("FLOW_API_KEY", "")
FLOW_SECRET_KEY = os.environ.get("FLOW_SECRET_KEY", "")
FLOW_API_URL = os.environ.get("FLOW_API_URL", "https://sandbox.flow.cl/api")


class FlowError(Exception):
    """Se lanza cuando Flow responde con un error o faltan credenciales."""


def is_configured():
    return bool(FLOW_API_KEY and FLOW_SECRET_KEY)


def _sign(params: dict) -> str:
    """Firma los parámetros según el algoritmo de Flow: concatenar
    clave+valor ordenados alfabéticamente por clave, y firmar con
    HMAC-SHA256 usando el Secret Key."""
    to_sign = "".join(f"{k}{params[k]}" for k in sorted(params.keys()))
    return hmac.new(
        FLOW_SECRET_KEY.encode("utf-8"),
        to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _call(service: str, params: dict, method: str = "POST") -> dict:
    if not is_configured():
        raise FlowError(
            "Flow no está configurado todavía: faltan FLOW_API_KEY / FLOW_SECRET_KEY."
        )

    full_params = dict(params)
    full_params["apiKey"] = FLOW_API_KEY
    full_params["s"] = _sign(full_params)

    url = f"{FLOW_API_URL}/{service}"
    try:
        if method == "GET":
            resp = requests.get(url, params=full_params, timeout=15)
        else:
            resp = requests.post(url, data=full_params, timeout=15)
    except requests.RequestException as exc:
        raise FlowError(f"No se pudo contactar a Flow: {exc}") from exc

    try:
        data = resp.json()
    except ValueError:
        raise FlowError(f"Respuesta inválida de Flow ({resp.status_code}).")

    if resp.status_code >= 400 or "code" in data:
        message = data.get("message", "Error desconocido de Flow.")
        raise FlowError(message)

    return data


# ------------------------------------------------------------------
# Clientes (customer)
# ------------------------------------------------------------------

def customer_create(name: str, email: str, external_id: str):
    return _call("customer/create", {
        "name": name,
        "email": email,
        "externalId": external_id,
    })


def customer_register(customer_id: str, url_return: str):
    """Devuelve {url, token}: redirige el navegador a url+"?token="+token
    para que el cliente registre su tarjeta en Flow."""
    return _call("customer/register", {
        "customerId": customer_id,
        "url_return": url_return,
    })


def customer_get_register_status(token: str):
    return _call("customer/getRegisterStatus", {"token": token}, method="GET")


def customer_unregister(customer_id: str):
    return _call("customer/unRegister", {"customerId": customer_id})


# ------------------------------------------------------------------
# Suscripciones
# ------------------------------------------------------------------

def subscription_create(plan_id: str, customer_id: str):
    return _call("subscription/create", {
        "planId": plan_id,
        "customerId": customer_id,
    })


def subscription_get(subscription_id: str):
    return _call("subscription/get", {"subscriptionId": subscription_id}, method="GET")


def subscription_cancel(subscription_id: str):
    return _call("subscription/cancel", {"subscriptionId": subscription_id})


# ------------------------------------------------------------------
# Pagos (usados por el webhook de confirmación)
# ------------------------------------------------------------------

def payment_get_status(token: str):
    return _call("payment/getStatus", {"token": token}, method="GET")
