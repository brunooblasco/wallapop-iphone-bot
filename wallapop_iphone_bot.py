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

REAL_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# --------------------------------------------------------------------------
# BÚSQUEDA EN WALLAPOP CON NAVEGADOR VIRTUAL
# --------------------------------------------------------------------------
#
# CLAVE DEL FIX: no navegamos directamente a la URL de la API (eso da 403,
# porque Wallapop detecta que no es una petición hecha por su propio
# JavaScript desde una página real). En vez de eso, cargamos la página de
# búsqueda real de es.wallapop.com e INTERCEPTAMOS la llamada de red que la
# propia web hace internamente a su API. Esa llamada sí lleva las cookies de
# sesión, cabeceras y token que su sistema anti-bot espera.

def
