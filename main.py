from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time, re, json, base64, uuid

app = FastAPI()
TAREAS = {}

@app.get("/")
@app.get("/ping")
def ping():
    return {"status": "ok", "mensaje": "Robot en línea y con cámaras encendidas."}

class Receptor(BaseModel):
    numDocumento: str
    nombre: str
    nrc: str = ""
    codActividad: str = ""
    departamento: str = ""
    municipio: str = ""
    direccion: str = ""
    correo: str = ""
    telefono: str = ""

class Item(BaseModel):
    cantidad: float
    descripcion: str
    precio: float
    tipo_item: str = "1 - Bien"
    tipo_venta: str = "Gravado"

class FacturaRequest(BaseModel):
    nit_empresa: str
    clave_hacienda: str
    clave_firma: str = ""
    tipo_dte: str
    receptor: Receptor
    items: list[Item]
    formas_pago: list[str]
    condicion: str = "Contado"
    observaciones: str = ""

TIPO_ITEM_MAP = {"1 - Bien": "1 - Bien", "2 - Servicio": "2 - Servicio", "3 - Bien y servicio": "3 - Bien y servicio"}

# --- FUNCIÓN QUE TOMA FOTOS Y REPORTA EL ESTADO AL FRONTEND ---
def reportar_paso(task_id, mensaje, page=None):
    b64_img = ""
    if page:
        try:
            # Toma una captura rápida en calidad baja para no consumir internet
            img_bytes = page.screenshot(type="jpeg", quality=40)
            b64_img = base64.b64encode(img_bytes).decode("utf-8")
        except: pass
    TAREAS[task_id]["mensaje"] = mensaje
    if b64_img: TAREAS[task_id]["screenshot"] = b64_img

def fill_field(page, selector, value, timeout=5000):
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.fill(str(value))
    except: pass

def select_opt(page, selector, label=None, value=None, index=None, timeout=5000):
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        if label: el.select_option(label=label)
        elif value: el.select_option(value=value)
        elif index is not None: el.select_option(index=index)
    except: pass

def procesar_dte_en_fondo(task_id: str, req: FacturaRequest):
    TAREAS[task_id] = {"status": "procesando", "mensaje": "Iniciando navegador...", "screenshot": ""}
    
    hw_user = req.nit_empresa
    hw_pass = req.clave_hacienda
    hw_clave = req.clave_firma if req.clave_firma else req.clave_hacienda
    tipo_dte = req.tipo_dte
    es_nota = tipo_dte in ("Nota de Crédito", "Nota de Débito")
    forma_pago = req.formas_pago[0] if req.formas_pago else "01"
    
    monto_pago = sum([i.cantidad * i.precio for i in req.items])
    if tipo_dte == "Comprobante de Crédito Fiscal": monto_pago = monto_pago * 1.13

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
            # OBLIGATORIO: Usar User-Agent de Windows para que Hacienda no bloquee el VPS de Render
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                accept_downloads=True
            )
            page = context.new_page()
            page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["media"] else route.continue_())

            reportar_paso(task_id, "Abriendo portal de Hacienda...", page)
            page.goto("https://factura.gob.sv/", wait_until="domcontentloaded")
            
            with context.expect_page() as npi: page.click("text=Ingresar")
            page = npi.value
            page.wait_for_load_state("domcontentloaded")
            
            reportar_paso(task_id, "Iniciando sesión (Login)...", page)
            try: page.locator("//h5[contains(text(),'Emisores DTE')]/..//button").click()
            except: pass
            
            page.get_by_placeholder("NIT/DUI").fill(hw_user.replace("-", ""))
            page.locator("input[type='password']").fill(hw_pass)
            try:
                page.locator("select[formcontrolname='ambiente']").first.select_option(value="/test")
                time.sleep(0.5) 
            except: pass

            page.click("button:has-text('Iniciar sesión')")
            try: page.wait_for_selector(".swal2-confirm", timeout=3000); page.click(".swal2-confirm")
            except: pass

            reportar_paso(task_id, "Entrando al Sistema de Facturación...", page)
            try:
                page.locator("text=Sistema de facturación").wait_for(state="visible", timeout=10000)
                page.click("text=Sistema de facturación", force=True)
                page.wait_for_load_state("domcontentloaded")
            except: pass

            reportar_paso(task_id, f"Seleccionando tipo: {tipo_dte}", page)
            try:
                page.wait_for_selector(".swal2-popup select, select.swal2-select", timeout=10000)
                page.locator(".swal2-popup select, select.swal2-select").first.select_option(label=tipo_dte)
                page.locator("button.swal2-confirm, button:has-text('OK')").first.click()
            except: pass

            reportar_paso(task_id, "Llenando datos del cliente...", page)
            if not es_nota and not ("Crédito Fiscal" in tipo_dte):
                try: page.locator("select[formcontrolname='tipoDocumento']").first.select_option(label="NIT")
                except: pass

            nit_clean = req.receptor.numDocumento.replace("-", "")
            try: page.locator("input[placeholder^='Digite el número de']").first.fill(nit_clean)
            except: fill_field(page, "input[formcontrolname='numDocumento']", nit_clean)

            if ("Crédito Fiscal" in tipo_dte) or es_nota:
                if req.receptor.nrc: fill_field(page, "input[formcontrolname='nrc']", req.receptor.nrc.replace("-", ""))
                if req.receptor.codActividad:
                    try:
                        cod = req.receptor.codActividad.split(" - ")[0].strip()
                        a = page.locator("ng-select[formcontrolname='actividadEconomica']").first
                        a.click(); a.locator("input").fill(cod)
                        page.locator(f"div.ng-option:has-text('{cod}')").first.click()
                    except: pass

            fill_field(page, "input[formcontrolname='nombre']", req.receptor.nombre)
            fill_field(page, "textarea[formcontrolname='complemento']", req.receptor.direccion)

            reportar_paso(task_id, "Llenando productos de la factura...", page)
            for i, item in enumerate(req.items):
                if i == 0:
                    try:
                        page.locator("button:has-text('Agregar Detalle'), #btnGroupDrop2").first.click()
                        time.sleep(0.5)
                        page.locator("a.dropdown-item:has-text('Producto o Servicio')").first.click()
                        time.sleep(0.5)
                    except: pass
                select_opt(page, "xpath=//label[contains(text(),'Tipo:')]/following-sibling::*//select", label=TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien"))
                try:
                    ci = page.locator("input[formcontrolname='cantidad']").first
                    ci.clear(); ci.fill(str(item.cantidad)); ci.press("Tab")
                except: pass
                select_opt(page, "xpath=//label[contains(text(),'Unidad')]/following-sibling::*//select", label="Unidad")
                fill_field(page, "input[formcontrolname='descripcion']", item.descripcion)
                select_opt(page, "xpath=//label[contains(text(),'Tipo Venta')]/following-sibling::*//select", label=item.tipo_venta)
                try:
                    pi = page.locator("input[formcontrolname='precioUnitario']").first
                    pi.clear(); pi.fill(f"{item.precio:.2f}"); pi.press("Tab")
                except: pass
                try: page.locator("button.btn-primary:has-text('Agregar ítem'), button.btn-primary:has-text('Agregar Ítem')").last.click()
                except: pass
                if i < len(req.items) - 1:
                    try: page.locator("button:has-text('Seguir adicionando')").click()
                    except: pass
                else:
                    try: page.locator("button:has-text('Regresar al documento')").click()
                    except: pass

            if not es_nota:
                reportar_paso(task_id, "Configurando Forma de Pago...", page)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.5)
                fp_val = {"01": "0: 01", "02": "3: 04", "03": "4: 05", "07": "1: 02", "99": "11: 99"}.get(forma_pago, "0: 01")
                try: page.locator("select[formcontrolname='codigo']").first.select_option(value=fp_val)
                except: pass
                try:
                    mp = page.locator("input[formcontrolname='montoPago']").first
                    mp.clear(); mp.fill(f"{monto_pago:.2f}"); mp.press("Tab")
                except: pass
                try: page.locator("button.btn-block:has(i.fa-plus)").first.click(force=True)
                except: pass

            reportar_paso(task_id, "Generando Documento Final...", page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)
            try: page.locator("input[type='submit'][value='Generar Documento'], button:has-text('Generar Documento')").last.click(force=True)
            except: pass
            try: page.locator("button.swal2-confirm:has-text('Si, crear documento')").first.click(force=True)
            except: pass
            
            reportar_paso(task_id, "Ingresando Clave Privada...", page)
            try:
                ic = page.get_by_placeholder("Ingrese la clave privada de validación")
                ic.wait_for(state="visible", timeout=12000)
                ic.fill(hw_clave)
                bok = page.locator("button:has-text('OK')").last
                bok.wait_for(state="visible", timeout=5000)
                bok.click(force=True)
            except: pass

            reportar_paso(task_id, "Esperando respuesta oficial de Hacienda (UUID)...", page)
            codigo_generacion = ""
            pdf_base64 = ""
            
            def _extract_uuid(text):
                m = re.search(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", text)
                return m.group(0).upper() if m else ""

            t0 = time.time()
            pdf_tab = None
            while time.time() - t0 < 20:
                for pg in context.pages:
                    if "pdf" in pg.url.lower() or "blob:" in pg.url.lower():
                        pdf_tab = pg
                        break
                if pdf_tab: break
                time.sleep(1)

            if pdf_tab:
                codigo_generacion = _extract_uuid(pdf_tab.url)
                if not codigo_generacion: codigo_generacion = _extract_uuid(pdf_tab.title())
                try:
                    pdf_bytes_js = pdf_tab.evaluate("""async () => {
                        const r = await fetch(document.URL);
                        const buf = await r.arrayBuffer(); return Array.from(new Uint8Array(buf));
                    }""")
                    pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode('utf-8')
                except: pass

            if not codigo_generacion:
                reportar_paso(task_id, "Buscando errores en la pantalla...", page)
                try:
                    body_txt = page.inner_text("body")
                    if "incorrecta" in body_txt.lower() or "inválida" in body_txt.lower() or "rechazado" in body_txt.lower():
                        raise Exception("Hacienda rechazó la emisión. Clave privada incorrecta.")
                    codigo_generacion = _extract_uuid(body_txt)
                except Exception as e:
                    if "Hacienda rechazó" in str(e): raise e

            if not codigo_generacion:
                raise Exception("El portal de Hacienda no devolvió el UUID. Ver monitor para detalles.")

            browser.close()
            TAREAS[task_id] = {
                "status": "completado", "exito": True, "sello_recepcion": "IMPRESO-EN-EL-PDF", 
                "codigo_generacion": codigo_generacion, "pdf_base64": pdf_base64, "json_content": "{}"
            }

    except Exception as e:
        # SI OCURRE UN ERROR CRÍTICO, TOMAMOS FOTO FINAL DEL ERROR
        try: reportar_paso(task_id, f"ERROR CRÍTICO: {str(e)}", page)
        except: pass
        TAREAS[task_id] = {"status": "error", "exito": False, "detail": str(e), "screenshot": TAREAS[task_id].get("screenshot", "")}

@app.post("/facturar")
def facturar_inmediato(req: FacturaRequest, bg_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    bg_tasks.add_task(procesar_dte_en_fondo, task_id, req)
    return {"exito": True, "task_id": task_id, "status": "procesando"}

@app.get("/status/{task_id}")
def verificar_status(task_id: str):
    return TAREAS.get(task_id, {"status": "no_encontrado"})
