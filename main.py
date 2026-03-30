# main.py - EL ROBOT SILENCIOSO
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time

app = FastAPI()

# Definimos los datos que Cloudflare nos va a enviar
class FacturaRequest(BaseModel):
    nit_empresa: str
    clave_hacienda: str
    tipo_dte: str
    receptor: dict
    items: list
    formas_pago: list
    condicion: str
    observaciones: str = ""

@app.post("/facturar")
def automatizar_hacienda(req: FacturaRequest):
    try:
        # Iniciamos Playwright en modo OCULTO (headless=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True) # 100% silencioso y en segundo plano
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()

            # 1. Login en el portal gratuito
            page.goto("https://factura.gob.sv/")
            page.click("text=Ingresar")
            page.get_by_placeholder("NIT/DUI").fill(req.nit_empresa.replace("-", ""))
            page.locator("input[type='password']").fill(req.clave_hacienda)
            page.click("button:has-text('Iniciar sesión')")
            page.wait_for_load_state("networkidle")

            # 2. Navegar a Facturación
            page.click("text=Sistema de facturación")
            page.wait_for_load_state("networkidle")

            # 3. Tipo de Documento
            page.locator(".swal2-popup select").first.select_option(label=req.tipo_dte)
            page.locator("button.swal2-confirm").click()

            # ==========================================================
            # AQUÍ PEGAS EL RESTO DE TU LÓGICA DE PLAYWRIGHT ORIGINAL
            # Llenar cliente: req.receptor['numDocumento']
            # Llenar ítems: for item in req.items: ...
            # Clic en Generar
            # Extraer Código de Generación y Sello
            # ==========================================================

            codigo_extraido = "UUID-CAPTURADO-DEL-PORTAL"
            sello_extraido = "SELLO-CAPTURADO-DEL-PORTAL"

            browser.close()

            # Devolvemos el éxito a Cloudflare
            return {
                "exito": True,
                "sello_recepcion": sello_extraido,
                "codigo_generacion": codigo_extraido,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")
            }

    except Exception as e:
        return {"exito": False, "detail": str(e)}