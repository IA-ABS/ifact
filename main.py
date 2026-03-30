from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time

app = FastAPI()

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
        with sync_playwright() as p:
            # 1. Escudo anti-bots (Hacer creer a Hacienda que somos una PC normal)
            browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                accept_downloads=True
            )
            page = context.new_page()

            # 2. Ir a la página principal
            page.goto("https://factura.gob.sv/", wait_until="domcontentloaded")
            
            # 3. Clic en "Ingresar" y ESPERAR A QUE ABRA LA NUEVA PESTAÑA
            with context.expect_page() as nueva_pestana_info:
                page.locator("text=Ingresar").first.click()
            
            # 4. Saltamos a la nueva pestaña
            page = nueva_pestana_info.value
            page.wait_for_load_state("domcontentloaded")

            # 5. Ahora sí, buscar el NIT y Contraseña en la pestaña correcta
            nit_input = page.locator("input[placeholder*='NIT'], input[placeholder*='DUI']").first
            nit_input.wait_for(state="visible", timeout=15000)
            nit_input.fill(req.nit_empresa.replace("-", ""))

            page.locator("input[type='password']").fill(req.clave_hacienda)
            
            page.locator("button:has-text('Iniciar sesión')").first.click()
            page.wait_for_load_state("networkidle")

            # 6. Navegar a Facturación
            page.locator("text=Sistema de facturación").first.click()
            page.wait_for_load_state("networkidle")

            # 7. Seleccionar Tipo de Documento
            page.locator(".swal2-popup select, select.swal2-select").first.select_option(label=req.tipo_dte)
            page.locator("button.swal2-confirm, button:has-text('OK')").first.click()

            # ==========================================================
            # AQUÍ EMPIEZA EL LLENADO DE CLIENTES Y PRODUCTOS
            # (Añade aquí tu lógica original de Playwright para llenar
            #  los datos del cliente, agregar los ítems, darle a generar 
            #  y firmar con la clave privada)
            # ==========================================================
            
            # (Simulamos extracción para este ejemplo)
            codigo_extraido = "UUID-CAPTURADO"
            sello_extraido = "SELLO-CAPTURADO"

            browser.close()

            return {
                "exito": True,
                "sello_recepcion": sello_extraido,
                "codigo_generacion": codigo_extraido,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")
            }

    except Exception as e:
        # Si falla, capturamos el título de la página para saber dónde se quedó atascado
        titulo_pagina = "Desconocido"
        try:
            titulo_pagina = page.title()
        except:
            pass
            
        return {
            "exito": False, 
            "detail": f"Error: {str(e)} | Se atascó en la página: '{titulo_pagina}'"
        }
