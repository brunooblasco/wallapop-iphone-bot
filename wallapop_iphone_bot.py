#!/usr/bin/env python3
"""
Bot diario de búsqueda de iPhone 14 / iPhone 15 en Wallapop.

v4: usa el actor de Apify "data_alchemist/wallapop-search" (resuelve el
bloqueo anti-bot de Wallapop con su propia infraestructura de proxies).
Campos confirmados con datos reales del actor -- ya no se adivina nada:
title, description, price.amount, location.city, web_slug/item_url,
is_top_profile (insignia de vendedor destacado de Wallapop),
has_warranty (el anuncio incluye garantía), is_refurbished.

Este actor NO expone rating/nº de reseñas del vendedor -- no existe ese
dato en su output. Por eso la "fiabilidad" se mide con las dos señales
que sí trae Wallapop de forma nativa: is_top_profile y has_warranty.
"""

import os
import json
import time
import smtplib
import statistics
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --------------------------------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------------------------------

import re

SEARCH_TERMS = {
    "iPhone 14": "iphone 14",
    "iPhone 15": "iphone 15",
}

# El buscador de Wallapop hace coincidencias laxas (p.ej. "iphone 14" también
# devuelve un "Xiaomi Redmi Note 14 Pro" o accesorios Apple sin relación).
# Exigimos que el título contenga de verdad "iphone" + el número de modelo,
# permitiendo variantes de la misma familia (Plus/Pro/Pro Max/mini) pero
# excluyendo modelos distintos (12, 13, 16...).
MODEL_PATTERNS = {
    "iPhone 14": re.compile(r"iphone\s*14\b", re.IGNORECASE),
    "iPhone 15": re.compile(r"iphone\s*15\b", re.IGNORECASE),
}

# Solo filtramos por título: si el propio título es un servicio de
# reparación/repuesto o un accesorio, no es un iPhone a la venta.
EXCLUDE_WORDS_TITLE = [
    "pantalla", "repuesto", "reparaci", "reparar", "despiece", "para piezas",
    "no enciende", "avería", "averiado", "bateria", "batería", "flex",
    "conector de carga", "tapa trasera", "carcasa", "cristal", "táctil",
    "funda", "case", "cargador", "cable", "protector", "magsafe", "cartera",
]

TOP_N_PER_MODEL = 3
MAX_RESULTS_PER_SEARCH = 40
LOCATION = "40.4165, -3.70256"  # Madrid

# Umbral solo para la búsqueda ordenada por precio: por debajo de esto en
# Wallapop casi todo son fundas, cables y protectores, no iPhones reales.
# No afecta a qué anuncios acaban en el email (eso lo decide el filtro por
# título), solo evita gastar el cupo de 40 resultados en accesorios.
CHEAP_SEARCH_MIN_PRICE = 100

APIFY_ACTOR = "data_alchemist~wallapop-search"
APIFY_URL = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"

DEBUG = os.environ.get("WALLAPOP_DEBUG", "0") == "1"

# --------------------------------------------------------------------------
# BÚSQUEDA VÍA APIFY
# --------------------------------------------------------------------------

def search_wallapop(keyword, api_token, order_by, min_price=0):
    """Llama al actor de Apify que scrapea Wallapop y devuelve sus items."""
    payload = {
        "keywords": keyword,
        "minPrice": min_price,
        "maxPrice": 0,  # 0 = sin máximo. El default del actor es 200€, lo
                        # que descartaría casi todos los iPhone 14/15 reales.
        "location": LOCATION,
        "orderBy": order_by,
        "maxResults": MAX_RESULTS_PER_SEARCH,
    }
    try:
        resp = requests.post(
            APIFY_URL,
            params={"token": api_token},
            json=payload,
            timeout=180,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[ERROR] Fallo llamando a Apify para '{keyword}' (orden: {order_by}): {e}")
        return []


def search_wallapop_combined(keyword, api_token):
    """
    Combina dos vistas de Wallapop para no perdernos ni lo recién publicado
    ni los chollos más antiguos: si solo pidiéramos 'newest', un anuncio
    barato de hace unos días podría quedar fuera de los primeros N resultados
    y nunca llegar a puntuarse.

    La vista por precio lleva un mínimo (CHEAP_SEARCH_MIN_PRICE) solo para no
    malgastar sus 40 huecos en fundas y accesorios de pocos euros; la vista
    por 'newest' no tiene mínimo, así que un iPhone genuino recién publicado
    y barato se sigue viendo igualmente.
    """
    newest = search_wallapop(keyword, api_token, "newest")
    cheapest = search_wallapop(keyword, api_token, "price_low_to_high", min_price=CHEAP_SEARCH_MIN_PRICE)

    seen_ids = set()
    combined = []
    for item in newest + cheapest:
        item_id = item.get("id")
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        combined.append(item)
    return combined


# --------------------------------------------------------------------------
# NORMALIZACIÓN DE CAMPOS (schema real confirmado)
# --------------------------------------------------------------------------

def extract_fields(item):
    title = item.get("title") or "(sin título)"
    description = item.get("description") or ""

    price_block = item.get("price") or {}
    price = price_block.get("amount") if isinstance(price_block, dict) else price_block

    location = item.get("location") or {}
    city = location.get("city", "") if isinstance(location, dict) else ""

    link = item.get("item_url")
    if not link:
        slug = item.get("web_slug")
        link = f"https://es.wallapop.com/item/{slug}" if slug else None

    return {
        "title": str(title),
        "description": str(description),
        "price": float(price) if price not in (None, "") else None,
        "user_id": item.get("user_id"),
        "link": link,
        "city": city,
        "is_top_profile": bool(item.get("is_top_profile")),
        "has_warranty": bool(item.get("has_warranty")),
        "is_refurbished": bool(item.get("is_refurbished")),
    }


def is_excluded(title):
    title_lower = title.lower()
    return any(word in title_lower for word in EXCLUDE_WORDS_TITLE)


def matches_model(title, model_pattern):
    return bool(model_pattern.search(title))


# --------------------------------------------------------------------------
# PUNTUACIÓN
# --------------------------------------------------------------------------

def score_listings(raw_items, model_pattern):
    parsed = []
    for item in raw_items:
        f = extract_fields(item)
        if f["price"] is None or f["price"] <= 0:
            continue
        if is_excluded(f["title"]):
            continue
        if not matches_model(f["title"], model_pattern):
            continue
        parsed.append(f)

    if not parsed:
        return []

    prices = [p["price"] for p in parsed]
    median_price = statistics.median(prices)

    for p in parsed:
        # Precio: más barato que la mediana del día = mejor (tope en 1.5x)
        price_component = min(1.5, median_price / p["price"])

        # Fiabilidad: insignia de vendedor destacado de Wallapop + garantía
        trust_component = (0.65 if p["is_top_profile"] else 0.0) + \
                           (0.35 if p["has_warranty"] else 0.0)

        p["score"] = round(0.6 * price_component + 0.4 * trust_component, 3)

    parsed.sort(key=lambda x: x["score"], reverse=True)
    return parsed


def top_n_diverse(parsed, n):
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
            badges = []
            if l["is_top_profile"]:
                badges.append("⭐ Vendedor destacado")
            if l["has_warranty"]:
                badges.append("🛡️ Con garantía")
            if l["is_refurbished"]:
                badges.append("♻️ Reacondicionado")
            badges_txt = " · ".join(badges) if badges else "Sin insignias de Wallapop"

            link_html = f"<a href='{l['link']}'>Ver anuncio</a>" if l["link"] else "(sin enlace)"
            parts.append(
                "<li style='margin-bottom:12px;'>"
                f"<b>{l['title']}</b><br>"
                f"💶 {l['price']:.0f} € &nbsp;|&nbsp; 📍 {l['city'] or 'ubicación no indicada'}<br>"
                f"{badges_txt}<br>"
                f"{link_html}"
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
    api_token = os.environ["APIFY_TOKEN"]
    results_by_model = {}

    for model_label, keyword in SEARCH_TERMS.items():
        print(f"Buscando: {keyword}...")
        raw = search_wallapop_combined(keyword, api_token)
        print(f"  -> {len(raw)} anuncios crudos de Apify (newest + price_low_to_high, sin duplicados)")
        scored = score_listings(raw, MODEL_PATTERNS[model_label])
        top = top_n_diverse(scored, TOP_N_PER_MODEL)
        results_by_model[model_label] = top
        print(f"  -> {len(top)} seleccionados tras filtrar/puntuar")
        time.sleep(1)

    html = build_email_html(results_by_model)

    if DEBUG:
        with open("preview.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("Modo debug: email guardado en preview.html, no se envía.")
        return

    send_email(html)


if __name__ == "__main__":
    main()
