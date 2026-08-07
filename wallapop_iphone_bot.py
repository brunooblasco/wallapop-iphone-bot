#!/usr/bin/env python3
"""
Bot diario de búsqueda de iPhone 14 / iPhone 15 en Wallapop usando Playwright.
"""

import os
import sys
import json
import time
import smtplib
import statistics
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# --------------------------------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------------------------------

SEARCH_TERMS = {
    "iPhone 14": "iphone 14",
    "iPhone 15": "iphone 15",
}

EXCLUDE_WORDS = ["funda", "case", "cargador", "cable", "protector",
                 "pantalla rota", "para piezas", "solo pantalla", "carcasa"]

MAX_RESULTS_PER_SEARCH = 50
TOP_N_PER_MODEL = 3
LATITUDE = 40.4168   # Madrid
LONGITUDE = -3.7038
SEARCH_DISTANCE_M = 100000

DEBUG = os.environ.get("WALLAPOP_DEBUG", "0") == "1"

# --------------------------------------------------------------------------
# BÚSQUEDA EN WALLAPOP CON NAVEGADOR VIRTUAL
# --------------------------------------------------------------------------

def search_wallapop(keyword):
    """Lanza un Chromium headless para obtener datos como un usuario real."""
    wallapop_url = f"https://api.wallapop.com/api/v3/general/search?keywords={keyword}&latitude={LATITUDE}&longitude={LONGITUDE}&distance={SEARCH_DISTANCE_M}&order_by=newest"
    
    items = []
    with sync_playwright() as p:
        # Lanzamos un navegador Chromium real
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="es-ES"
        )
        page = context.new_page()
        
        try:
            # Primero visitamos la home para aceptar galletas/tokens
            page.goto("https://es.wallapop.com", timeout=30000, wait_until="domcontentloaded")
            time.sleep(2)
            
            # Consultamos la API desde la sesión activa del navegador
            response = page.goto(wallapop_url, timeout=30000)
            if response and response.ok:
                data = response.json()
                items = data.get("search_objects", []) or data.get("objects", [])
            else:
                print(f"[ERROR] Estado de la respuesta: {response.status if response else 'Sin respuesta'}")
        except Exception as e:
            print(f"[ERROR] Fallo en la navegación para '{keyword}': {e}")
        finally:
            browser.close()

    return items[:MAX_RESULTS_PER_SEARCH]


def get_seller_reputation(user_id):
    if not user_id:
        return None, 0
    url = f"https://api.wallapop.com/api/v3/users/{user_id}/reviews"
    rating, total = None, 0
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        try:
            response = page.goto(url, timeout=15000)
            if response and response.ok:
                d = response.json()
                rating = d.get("rating_average") or d.get("average_rating")
                total = d.get("total") or d.get("total_reviews") or len(d.get("reviews", []))
        except Exception:
            pass
        finally:
            browser.close()
            
    return rating, total

# --------------------------------------------------------------------------
# NORMALIZACIÓN DE CAMPOS Y PUNTUACIÓN
# --------------------------------------------------------------------------

def extract_fields(item):
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

def score_listings(raw_items):
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

        price_component = min(1.5, median_price / p["price"]) if p["price"] > 0 else 0

        if rating:
            trust_component = (rating / 5) * min(1.0, (num_ratings ** 0.5) / 5)
        else:
            trust_component = 0.15

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
