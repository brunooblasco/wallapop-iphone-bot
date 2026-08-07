#!/usr/bin/env python3
"""
Bot diario de búsqueda de iPhone 14 / iPhone 15 en Wallapop.

v3: en vez de scrapear Wallapop directamente (bloqueado por su sistema
anti-bot para IPs de datacenter como las de GitHub Actions), usamos el
actor de Apify "data_alchemist/wallapop-search", que ya resuelve ese
bloqueo con su propia infraestructura de proxies. Coste: ~$5 por cada
1000 páginas buscadas -> con 2 búsquedas/día son céntimos al mes, dentro
del crédito gratuito de Apify ($5/mes, sin tarjeta).
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

SEARCH_TERMS = {
    "iPhone 14": "iphone 14",
    "iPhone 15": "iphone 15",
}

EXCLUDE_WORDS = ["funda", "case", "cargador", "cable", "protector",
                 "pantalla rota", "para piezas", "solo pantalla", "carcasa"]

EXCLUDE_WORDS_TITLE = [
    "pantalla", "repuesto", "reparaci", "cambio de pantalla", "cambio pantalla",
    "despiece", "para piezas", "no enciende", "avería", "averiado",
    "bateria", "batería", "flex", "conector de carga", "tapa trasera",
    "carcasa", "solo pantalla", "cristal", "táctil", "reparar",
]

TOP_N_PER_MODEL = 3
MAX_RESULTS_PER_SEARCH = 40
LOCATION = "40.4165, -3.70256"  # Madrid

APIFY_ACTOR = "data_alchemist~wallapop-search"
APIFY_URL = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items"

DEBUG = os.environ.get("WALLAPOP_DEBUG", "0") == "1"

# --------------------------------------------------------------------------
# BÚSQUEDA VÍA APIFY
# --------------------------------------------------------------------------

def search_wallapop(keyword, api_token):
    """Llama al actor de Apify que scrapea Wallapop y devuelve sus items."""
    payload = {
        "keywords": keyword,
        "minPrice": 0,
        "maxPrice": 0,  # 0 = sin máximo. OJO: el default del actor es 200€,
                        # lo que descartaría casi todos los iPhone 14/15 reales.
        "location": LOCATION,
        "orderBy": "newest",
        "maxResults": MAX_RESULTS_PER_SEARCH,
    }
    try:
        resp = requests.post(
            APIFY_URL,
            params={"token": api_token},
            json=payload,
            timeout=180,  # el actor tarda en arrancar y scrapear, damos margen
        )
        resp.raise_for_status()
        items = resp.json()
        if DEBUG and items:
            print(f"[DEBUG] Ejemplo de item crudo para '{keyword}':")
            print(json.dumps(items[0], indent=2, ensure_ascii=False)[:2000])
        return items
    except Exception as e:
        print(f"[ERROR] Fallo llamando a Apify para '{keyword}': {e}")
        return []


# --------------------------------------------------------------------------
# NORMALIZACIÓN DE CAMPOS
# --------------------------------------------------------------------------
#
# No conocemos con total certeza los nombres exactos de campo que devuelve
# este actor de terceros, así que probamos varias claves habituales para
# cada dato. Si el email sale con precios/títulos vacíos, activa
# WALLAPOP_DEBUG=1 para ver el JSON crudo real y ajustar las listas de abajo.

def first_present(d, keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d.get(k)
    return default


def extract_fields(item):
    title = first_present(item, ["title", "name", "productTitle"], "(sin título)")
    description = first_present(item, ["description", "productDescription"], "")

    price = first_present(item, ["price", "salePrice", "amount"])
    if isinstance(price, dict):
        price = first_present(price, ["amount", "value", "cash"])

    link = first_present(item, ["url", "link", "itemUrl", "webSlug"])
    if link and not str(link).startswith("http"):
        link = f"https://es.wallapop.com/item/{link}"

    seller = first_present(item, ["seller", "user", "sellerInfo"], {})
    if isinstance(seller, dict):
        seller_name = first_present(seller, ["name", "microName", "username"], "Vendedor")
        seller_rating = first_present(seller, ["rating", "ratingAverage", "score"])
        seller_num_ratings = first_present(seller, ["numReviews", "totalReviews", "reviewCount"], 0)
        seller_id = first_present(seller, ["id", "userId"])
    else:
        seller_name, seller_rating, seller_num_ratings, seller_id = "Vendedor", None, 0, None

    city = first_present(item, ["city", "location"], "")
    if isinstance(city, dict):
        city = first_present(city, ["city", "name"], "")

    return {
        "title": str(title),
        "description": str(description or ""),
        "price": float(price) if price not in (None, "") else None,
        "seller_id": seller_id,
        "seller_name": seller_name,
        "seller_rating": float(seller_rating) if seller_rating not in (None, "") else None,
        "seller_num_ratings": int(seller_num_ratings) if seller_num_ratings else 0,
        "link": link,
        "city": city,
    }


def is_excluded(title, description):
    title_lower = title.lower()
    text = f"{title} {description}".lower()
    # Título: lista estricta (si el propio título suena a repuesto/reparación, fuera)
    if any(word in title_lower for word in EXCLUDE_WORDS_TITLE):
        return True
    # Descripción+título: lista más suave, complementaria
    if any(word in text for word in EXCLUDE_WORDS):
        return True
    return False


# --------------------------------------------------------------------------
# PUNTUACIÓN
# --------------------------------------------------------------------------

def score_listings(raw_items):
    parsed = []
    for item in raw_items:
        f = extract_fields(item)
        if f["price"] is None or f["price"] <= 0:
            continue
        if is_excluded(f["title"], f["description"]):
            continue
        parsed.append(f)

    if not parsed:
        return []

    prices = [p["price"] for p in parsed]
    median_price = statistics.median(prices)

    for p in parsed:
        price_component = min(1.5, median_price / p["price"])

        if p["seller_rating"]:
            trust_component = (p["seller_rating"] / 5) * min(1.0, (p["seller_num_ratings"] ** 0.5) / 5)
        else:
            trust_component = 0.15  # sin datos de vendedor: no descartar, pero penalizar

        p["score"] = round(0.6 * price_component + 0.4 * trust_component, 3)

    parsed.sort(key=lambda x: x["score"], reverse=True)
    return parsed


def top_n_diverse(parsed, n):
    seen_sellers = set()
    result = []
    for p in parsed:
        key = p["seller_id"] or p["seller_name"]
        if key in seen_sellers:
            continue
        result.append(p)
        seen_sellers.add(key)
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
            link_html = f"<a href='{l['link']}'>Ver anuncio</a>" if l["link"] else "(sin enlace)"
            parts.append(
                "<li style='margin-bottom:12px;'>"
                f"<b>{l['title']}</b><br>"
                f"💶 {l['price']:.0f} € &nbsp;|&nbsp; 📍 {l['city'] or 'ubicación no indicada'}<br>"
                f"👤 {l['seller_name']} — {rating_txt}<br>"
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
        raw = search_wallapop(keyword, api_token)
        print(f"  -> {len(raw)} anuncios crudos de Apify")
        if raw:
            print(f"[DEBUG] Claves del primer item crudo: {list(raw[0].keys())}")
            print(f"[DEBUG] Item crudo completo:\n{json.dumps(raw[0], indent=2, ensure_ascii=False)[:2500]}")
        scored = score_listings(raw)
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
