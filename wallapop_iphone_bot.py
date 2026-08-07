#!/usr/bin/env python3
"""
Bot diario de búsqueda de iPhone 14 / iPhone 15 en Wallapop.

Busca anuncios, valora la fiabilidad del vendedor (rating + nº de ventas/valoraciones)
combinada con el precio frente a la media del mercado, y envía un email con las
3 mejores opciones de cada modelo.

IMPORTANTE: Wallapop no publica una API oficial. Este script usa los mismos
endpoints internos que usa la web (api.wallapop.com). Si Wallapop cambia su
API, esto puede romperse. Ejecuta con WALLAPOP_DEBUG=1 para imprimir el JSON
crudo de un anuncio y así poder ajustar las rutas de los campos si hace falta.
"""

import os
import sys
import json
import time
import smtplib
import statistics
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# --------------------------------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------------------------------

SEARCH_TERMS = {
    "iPhone 14": "iphone 14",
    "iPhone 15": "iphone 15",
}

# Excluye accesorios / fundas / piezas sueltas que a veces cuelan en la búsqueda
EXCLUDE_WORDS = ["funda", "case", "cargador", "cable", "protector",
                 "pantalla rota", "para piezas", "solo pantalla", "carcasa"]

MAX_RESULTS_PER_SEARCH = 50
TOP_N_PER_MODEL = 3
LATITUDE = 40.4168   # Madrid, ajusta si quieres centrar la búsqueda en otro sitio
LONGITUDE = -3.7038
SEARCH_DISTANCE_M = 100000  # 100km a la redonda

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "X-DeviceOS": "0",
    "Accept": "application/json",
}

DEBUG = os.environ.get("WALLAPOP_DEBUG", "0") == "1"


def debug_print(*args):
    if DEBUG:
        print("[DEBUG]", *args)


# --------------------------------------------------------------------------
# BÚSQUEDA EN WALLAPOP
# --------------------------------------------------------------------------

def search_wallapop(keyword):
    """Llama al endpoint de búsqueda de Wallapop y devuelve la lista de anuncios."""
    url = "https://api.wallapop.com/api/v3/general/search"
    params = {
        "keywords": keyword,
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "distance": SEARCH_DISTANCE_M,
        "order_by": "newest",
        "filters_source": "quick_filters",
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[ERROR] Fallo buscando '{keyword}': {e}")
        return []

    items = data.get("search_objects", []) or data.get("objects", [])
    if DEBUG and items:
        debug_print(f"Ejemplo de item crudo para '{keyword}':")
        debug_print(json.dumps(items[0], indent=2, ensure_ascii=False)[:2000])

    return items[:MAX_RESULTS_PER_SEARCH]


def get_seller_reputation(user_id):
    """
    Intenta obtener rating medio y nº de valoraciones del vendedor.
    Devuelve (rating_0_5, num_valoraciones). Si falla, devuelve (None, 0).
    """
    if not user_id:
        return None, 0
    url = f"https://api.wallapop.com/api/v3/users/{user_id}/reviews"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            return None, 0
        d = r.json()
        rating = d.get("rating_average") or d.get("average_rating")
        total = d.get("total") or d.get("total_reviews") or len(d.get("reviews", []))
        return rating, total
    except Exception:
        return None, 0


# --------------------------------------------------------------------------
# NORMALIZACIÓN DE CAMPOS (defensivo, porque el JSON de Wallapop es inestable)
# --------------------------------------------------------------------------

def extract_fields(item):
    """Extrae de forma defensiva los campos que nos interesan de un anuncio."""
    title = item.get("title") or item.get("content", {}).get("title", "")
    description = item.get("description") or item.get("content", {}).get("description", "")

    price_block = item.get("price") or item.get("content", {}).get("price", {})
    if isinstance(price_block, dict):
        price = price_block.get("amount") or price_block.get("cash", {}).get("amount")
    else:
        price = price_block

    user_block = item.get("user") or item.get("content", {}).get("user", {})
    user_id = user_block.get("id")
    seller_name = user_block.get("micro_name") or user_block.get("name") or "Vendedor"

    slug = item.get("web_slug") or item.get("content", {}).get("web_slug")
    item_id = item.get("id") or item.get("content", {}).get("id")
    link = f"https://es.wallapop.com/item/{slug}" if slug else (
        f"https://es.wallapop.com/item/{item_id}" if item_id else None
    )

    location = item.get("location") or item.get("content", {}).get("location", {})
    city = location.get("city", "") if isinstance(location, dict) else ""

    return {
        "title": title or "(sin título)",
        "description": description or "",
        "price": price,
        "user_id": user_id,
        "seller_name": seller_name,
        "link": link,
        "city": city,
    }


def is_excluded(title, description):
    text = f"{title} {description}".lower()
    return any(word in text for word in EXCLUDE_WORDS)


# --------------------------------------------------------------------------
# PUNTUACIÓN
# --------------------------------------------------------------------------

def score_listings(raw_items):
    """
    Convierte anuncios crudos en listados enriquecidos con score.
    Score = combinación de (precio relativo a la mediana, más barato = mejor)
            y (fiabilidad del vendedor: rating * log(nº valoraciones + 1)).
    """
    parsed = []
    for item in raw_items:
        f = extract_fields(item)
        if f["price"] is None:
            continue
        if is_excluded(f["title"], f["description"]):
            continue
        parsed.append(f)

    if not parsed:
        return []

    prices = [p["price"] for p in parsed]
    median_price = statistics.median(prices)

    for p in parsed:
        rating, num_ratings = get_seller_reputation(p["user_id"])
        p["seller_rating"] = rating
        p["seller_num_ratings"] = num_ratings
        time.sleep(0.15)  # evitar martillear la API

        # Componente precio: cuanto más barato respecto a la mediana, mejor (tope en 1.5)
        price_component = min(1.5, median_price / p["price"]) if p["price"] > 0 else 0

        # Componente fiabilidad: valoración media (0-5) ponderada por volumen de ventas
        if rating:
            trust_component = (rating / 5) * min(1.0, (num_ratings ** 0.5) / 5)
        else:
            trust_component = 0.15  # vendedor sin datos: no descartar, pero penalizar

        p["score"] = round(0.6 * price_component + 0.4 * trust_component, 3)

    parsed.sort(key=lambda x: x["score"], reverse=True)
    return parsed


def top_n_diverse(parsed, n):
    """Coge el top N evitando devolver 3 anuncios del mismo vendedor."""
    seen_sellers = set()
    result = []
    for p in parsed:
        if p["user_id"] in seen_sellers and p["user_id"] is not None:
            continue
        result.append(p)
        seen_sellers.add(p["user_id"])
        if len(result) == n:
            break
    return result


# --------------------------------------------------------------------------
# EMAIL
# --------------------------------------------------------------------------

def build_email_html(results_by_model):
    parts = ["<h2>📱 Resumen diario Wallapop: iPhone 14 y 15</h2>"]
    for model, listings in results_by_model.items():
        parts.append(f"<h3>{model}</h3>")
        if not listings:
            parts.append("<p>No se encontraron anuncios válidos hoy.</p>")
            continue
        parts.append("<ol>")
        for l in listings:
            rating_txt = (
                f"{l['seller_rating']:.1f}★ ({l['seller_num_ratings']} valoraciones)"
                if l["seller_rating"] else "sin valoraciones"
            )
            parts.append(
                "<li style='margin-bottom:12px;'>"
                f"<b>{l['title']}</b><br>"
                f"💶 {l['price']} € &nbsp;|&nbsp; 📍 {l['city'] or 'ubicación no indicada'}<br>"
                f"👤 {l['seller_name']} — {rating_txt}<br>"
                f"<a href='{l['link']}'>Ver anuncio</a>"
                "</li>"
            )
        parts.append("</ol>")
    return "".join(parts)


def send_email(html_body):
    sender = os.environ["GMAIL_USER"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    receiver = os.environ["RECEIVER_EMAIL"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "📱 iPhone 14 / 15 en Wallapop — mejores opciones de hoy"
    msg["From"] = sender
    msg["To"] = receiver
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, receiver, msg.as_string())

    print("Email enviado correctamente.")


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main():
    results_by_model = {}

    for model_label, keyword in SEARCH_TERMS.items():
        print(f"Buscando: {keyword}...")
        raw = search_wallapop(keyword)
        print(f"  -> {len(raw)} anuncios crudos")
        scored = score_listings(raw)
        top = top_n_diverse(scored, TOP_N_PER_MODEL)
        results_by_model[model_label] = top
        print(f"  -> {len(top)} seleccionados tras filtrar/puntuar")

    html = build_email_html(results_by_model)

    if DEBUG:
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Modo debug: email guardado en preview.html, no se envía.")
        return

    send_email(html)


if __name__ == "__main__":
    main()
