from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, TimeoutError
import time, re, base64, uuid

app = FastAPI()
TAREAS = {}

@app.get("/")
@app.get("/ping")
def ping(): return {"status": "ok", "mensaje": "Robot ERP en línea"}

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

def reportar_paso(task_id, mensaje, page=None):
    b64_img = ""
    if page:
        try:
            img_bytes = page.screenshot(type="jpeg", quality=40)
            b64_img = base64.b64encode(img_bytes).decode("utf-8")
        except: pass
    TAREAS[task_id]["mensaje"] = mensaje
    if b64_img: TAREAS[task_id]["screenshot"] = b64_img

def procesar_dte_en_fondo(task_id: str, req: FacturaRequest):
    TAREAS[task_id] = {"status": "procesando", "mensaje": "Iniciando navegador...", "screenshot": ""}
    hw_user = req.nit_empresa
    hw_pass = req.clave_hacienda
    hw_clave = req.clave_firma if req.clave_firma else req.clave_hacienda
    tipo_dte = req.tipo_dte
    es_nota = tipo_dte in ("Nota de Crédito", "Nota de Débito")
    forma_pago = req.formas_pago[0] if req.formas_pago else "01"
    
    monto_pago = sum([i.cantidad * i.precio for i in req.items])
    if tipo_dte == "Comprobante de Crédito Fiscal": 
        monto_pago = monto_pago * 1.13 # Sumar IVA

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, 
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu'
                ]
            )
            context = browser.new_context(viewport={"width": 1366, "height": 768}, accept_downloads=True)
            page = context.new_page()

            # 1. LOGIN
            reportar_paso(task_id, "Iniciando sesión...", page)
            page.goto("https://factura.gob.sv/", wait_until="domcontentloaded")
            with context.expect_page() as npi: 
                page.click("text=Ingresar")
            page = npi.value
            
            # Cerrar popup si existe
            try: page.locator("//h5[contains(text(),'Emisores DTE')]/..//button").click(timeout=3000)
            except: pass
            
            page.get_by_placeholder("NIT/DUI").fill(hw_user.replace("-", ""))
            page.locator("input[type='password']").fill(hw_pass)
            
            # Selector de ambiente de pruebas si estuviera visible
            try: page.locator("select[formcontrolname='ambiente']").first.select_option(value="/test", timeout=1000) 
            except: pass
            
            page.click("button:has-text('Iniciar sesión')")
            try: page.locator(".swal2-confirm").click(timeout=4000)
            except: pass

            # 2. SELECCIONAR TIPO DE DOCUMENTO
            reportar_paso(task_id, "Entrando al facturador...", page)
            page.locator("text=Sistema de facturación").wait_for(state="visible", timeout=15000)
            page.click("text=Sistema de facturación", force=True)
            
            try:
                page.wait_for_selector(".swal2-popup select, select.swal2-select", timeout=10000)
                page.locator(".swal2-popup select, select.swal2-select").first.select_option(label=tipo_dte)
                page.locator("button.swal2-confirm, button:has-text('OK')").first.click()
            except: pass

            time.sleep(2) # Esperar a que cargue el formulario Angular

            # 3. LLENAR DATOS DEL CLIENTE
            reportar_paso(task_id, "Llenando cliente...", page)
            nit_clean = req.receptor.numDocumento.replace("-", "")
            
            if not es_nota and not ("Crédito Fiscal" in tipo_dte):
                try: page.locator("select[formcontrolname='tipoDocumento']").first.select_option(label="NIT")
                except: pass

            try: 
                page.locator("input[placeholder^='Digite el número de']").first.fill(nit_clean)
            except: 
                page.locator("input[formcontrolname='numDocumento']").first.fill(nit_clean)

            # Llenado especial para CCF
            if ("Crédito Fiscal" in tipo_dte) or es_nota:
                if req.receptor.nrc: 
                    try: page.locator("input[formcontrolname='nrc']").first.fill(req.receptor.nrc.replace("-", ""))
                    except: pass
                if req.receptor.codActividad:
                    try:
                        cod = req.receptor.codActividad.split(" - ")[0].strip()
                        a = page.locator("ng-select[formcontrolname='actividadEconomica']").first
                        a.click(force=True); time.sleep(0.5)
                        a.locator("input[type='text']").first.fill(cod); time.sleep(1)
                        page.locator(f"div.ng-option:has-text('{cod}')").first.click(force=True)
                    except: pass

            try: page.locator("input[formcontrolname='nombre']").first.fill(req.receptor.nombre)
            except: pass
            
            dir_val = req.receptor.direccion.strip() or "San Salvador, El Salvador"
            try:
                comp = page.locator("textarea[formcontrolname='complemento']").first
                comp.fill(dir_val)
                comp.press("Tab") 
            except: pass

            # 4. AGREGAR ÍTEMS (Basado en las capturas)
            reportar_paso(task_id, "Agregando ítems...", page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
            
            for i, item in enumerate(req.items):
                # Desplegar menú: "Agregar Ítem" -> "Producto o Servicio"
                page.locator("button:has-text('Agregar Ítem'), button:has-text('Agregar Detalle')").first.click()
                time.sleep(0.5)
                page.locator("a.dropdown-item:has-text('Producto o Servicio')").first.click()
                
                # Esperar a que abra el modal
                page.wait_for_selector("div.modal-content", timeout=5000)
                time.sleep(1)

                # Llenar modal de Item
                page.locator("select[formcontrolname='tipo']").first.select_option(label=TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien"))
                
                cant_input = page.locator("input[formcontrolname='cantidad']").first
                cant_input.clear(); cant_input.fill(str(item.cantidad))
                
                # Intentar seleccionar unidad "59 - Unidad" (basado en la captura) o solo "Unidad"
                try: page.locator("select[formcontrolname='unidad']").first.select_option(label="59 - Unidad", timeout=1000)
                except: page.locator("select[formcontrolname='unidad']").first.select_option(label="Unidad", timeout=1000)

                desc_input = page.locator("input[formcontrolname='producto'], input[formcontrolname='descripcion']").first
                desc_input.clear(); desc_input.fill(item.descripcion)
                
                page.locator("select[formcontrolname='tipoVenta']").first.select_option(label=item.tipo_venta)
                
                precio_input = page.locator("input[formcontrolname='precio'], input[formcontrolname='precioUnitario']").first
                precio_input.clear(); precio_input.fill(f"{item.precio:.2f}"); precio_input.press("Tab")
                
                time.sleep(1) # Pequeña pausa para que Angular calcule los totales del item
                
                # Clic al botón azul "Agregar ítem" abajo a la izquierda del modal
                page.locator("div.modal-footer button.btn-primary:has-text('Agregar ítem'), div.modal-footer button.btn-primary:has-text('Agregar Ítem')").first.click()
                
                time.sleep(1)

                # Popup de confirmación (basado en la captura 5)
                if i < len(req.items) - 1:
                    page.locator("button:has-text('Seguir adicionando')").first.click()
                else:
                    page.locator("button:has-text('Regresar al documento')").first.click()
                
                time.sleep(1)

            # 5. AGREGAR FORMA DE PAGO (Basado en capturas 7 y 8)
            if not es_nota:
                reportar_paso(task_id, "Validando Pagos...", page)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1)

                # Seleccionar Billetes y Monedas
                try:
                    # El select de hacienda a veces usa valores en index. Intentamos por label primero.
                    page.locator("select[formcontrolname='codigo']").first.select_option(label=re.compile("Billetes y monedas", re.IGNORECASE))
                except: pass
                
                # Llenar monto
                try:
                    mp = page.locator("input[formcontrolname='montoPago']").first
                    mp.clear(); mp.fill(f"{monto_pago:.2f}"); mp.press("Tab")
                except: pass
                
                time.sleep(0.5)
                
                # Clic al botón AZUL [+] (basado en captura 7)
                try:
                    page.locator("button.btn-primary[tooltip='Agregar forma de pago'], button.btn-primary i.fa-plus").first.click(force=True)
                except: pass
                
                time.sleep(1)

            # 6. GENERAR Y FIRMAR
            reportar_paso(task_id, "Firma y Envío...", page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
            
            # Botón "Generar Documento" (Captura 9)
            page.locator("button:has-text('Generar Documento'), input[value='Generar Documento']").first.click()
            time.sleep(1)
            
            # Modal de "¿Está seguro?"
            page.locator("button.swal2-confirm:has-text('Si, crear documento')").first.click()
            
            # 7. CLAVE PRIVADA (Captura 10)
            reportar_paso(task_id, "Clave Privada...", page)
            try:
                ic = page.get_by_placeholder("Ingrese la clave privada de validación")
                ic.wait_for(state="visible", timeout=8000)
                ic.fill(hw_clave)
                page.locator("button.swal2-confirm:has-text('OK')").first.click()
            except TimeoutError:
                errs = page.locator("div.alert-danger, div.toast-message").all_inner_texts()
                if errs: raise Exception("Error en validación: " + " | ".join(errs))

            # 8. OBTENER PDF Y UUID
            reportar_paso(task_id, "Esperando sello oficial...", page)
            codigo_generacion, pdf_base64 = "", ""
            
            def _extract_uuid(text):
                m = re.search(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", text)
                return m.group(0).upper() if m else ""

            # Esperar a que salga la pantalla de éxito con el UUID
            try:
                page.wait_for_selector(".swal2-popup, .swal2-content", timeout=20000)
                time.sleep(1.5)
                body_txt = page.inner_text("body")
                if "incorrecta" in body_txt.lower() or "inválida" in body_txt.lower(): 
                    raise Exception("Clave privada incorrecta.")
                codigo_generacion = _extract_uuid(body_txt)
            except Exception as e:
                if "Clave" in str(e): raise e

            # Capturar la pestaña nueva (PDF)
            t0 = time.time()
            pdf_tab = None
            while time.time() - t0 < 10:
                for pg in context.pages:
                    if "pdf" in pg.url.lower() or "blob:" in pg.url.lower(): 
                        pdf_tab = pg
                        break
                if pdf_tab: break
                time.sleep(1)

            if pdf_tab:
                if not codigo_generacion:
                    codigo_generacion = _extract_uuid(pdf_tab.url) or _extract_uuid(pdf_tab.title())
                try:
                    pdf_bytes_js = pdf_tab.evaluate("async () => { const r = await fetch(document.URL); const buf = await r.arrayBuffer(); return Array.from(new Uint8Array(buf)); }")
                    pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode('utf-8')
                except: pass

            if not pdf_base64 and codigo_generacion:
                reportar_paso(task_id, "Recuperando PDF del portal...", page)
                try:
                    from urllib.parse import urlparse
                    base_url = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}"
                    page.goto(f"{base_url}/consultaDteEmitidos", wait_until="domcontentloaded"); time.sleep(1)
                    page.locator("input[formcontrolname='codigoGeneracion']").first.fill(codigo_generacion)
                    page.locator("button:has-text('Consultar')").first.click()
                    page.wait_for_selector("tbody tr", timeout=10000); time.sleep(1.5)
                    pages_antes = set(id(p) for p in context.pages)
                    page.locator("button[tooltip='Versión legible']").first.click(force=True); time.sleep(4)
                    for pg in context.pages:
                        if id(pg) not in pages_antes or "pdf" in pg.url.lower() or "blob:" in pg.url.lower():
                            try:
                                pdf_bytes_js = pg.evaluate("async () => { const r = await fetch(document.URL); const buf = await r.arrayBuffer(); return Array.from(new Uint8Array(buf)); }")
                                pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode('utf-8')
                            except: pass
                            break
                except: pass

            if not codigo_generacion: 
                raise Exception("El portal de Hacienda no devolvió el UUID.")

            browser.close()
            TAREAS[task_id] = {
                "status": "completado", 
                "exito": True, 
                "sello_recepcion": "IMPRESO-EN-EL-PDF", 
                "codigo_generacion": codigo_generacion, 
                "pdf_base64": pdf_base64, 
                "json_content": "{}"
            }

    except Exception as e:
        try: reportar_paso(task_id, f"ERROR: {str(e)}", page)
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
