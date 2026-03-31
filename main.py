from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time, re, json, base64, uuid

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
    hw_user, hw_pass, hw_clave = req.nit_empresa, req.clave_hacienda, (req.clave_firma if req.clave_firma else req.clave_hacienda)
    tipo_dte, es_nota = req.tipo_dte, req.tipo_dte in ("Nota de Crédito", "Nota de Débito")
    forma_pago = req.formas_pago[0] if req.formas_pago else "01"
    
    monto_pago = sum([i.cantidad * i.precio for i in req.items])
    if tipo_dte == "Comprobante de Crédito Fiscal": monto_pago = monto_pago * 1.13

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
    headless=True, 
    args=[
        '--disable-blink-features=AutomationControlled',
        '--disable-dev-shm-usage', # Evita crasheos por falta de memoria en Docker
        '--no-sandbox',            # Obligatorio para correr Chrome dentro de Docker
        '--disable-gpu'            # Render no tiene GPU
    ]
)
            context = browser.new_context(viewport={"width": 1366, "height": 768}, accept_downloads=True, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            page = context.new_page()
            page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["media"] else route.continue_())

            reportar_paso(task_id, "Iniciando sesión...", page)
            page.goto("https://factura.gob.sv/", wait_until="domcontentloaded")
            with context.expect_page() as npi: page.click("text=Ingresar")
            page = npi.value
            try: page.locator("//h5[contains(text(),'Emisores DTE')]/..//button").click()
            except: pass
            
            page.get_by_placeholder("NIT/DUI").fill(hw_user.replace("-", ""))
            page.locator("input[type='password']").fill(hw_pass)
            try: page.locator("select[formcontrolname='ambiente']").first.select_option(value="/test"); time.sleep(0.5) 
            except: pass
            page.click("button:has-text('Iniciar sesión')")
            try: page.wait_for_selector(".swal2-confirm", timeout=3000); page.click(".swal2-confirm")
            except: pass

            reportar_paso(task_id, "Entrando al facturador...", page)
            try:
                page.locator("text=Sistema de facturación").wait_for(state="visible", timeout=10000)
                page.click("text=Sistema de facturación", force=True)
                page.wait_for_load_state("domcontentloaded")
            except: pass

            try:
                page.wait_for_selector(".swal2-popup select, select.swal2-select", timeout=10000)
                page.locator(".swal2-popup select, select.swal2-select").first.select_option(label=tipo_dte)
                page.locator("button.swal2-confirm, button:has-text('OK')").first.click()
            except: pass

            reportar_paso(task_id, "Llenando cliente...", page)
            if not es_nota and not ("Crédito Fiscal" in tipo_dte):
                try: page.locator("select[formcontrolname='tipoDocumento']").first.select_option(label="NIT")
                except: pass

            nit_clean = req.receptor.numDocumento.replace("-", "")
            try: page.locator("input[placeholder^='Digite el número de']").first.fill(nit_clean)
            except: 
                try: page.locator("input[formcontrolname='numDocumento']").first.fill(nit_clean)
                except: pass

            if ("Crédito Fiscal" in tipo_dte) or es_nota:
                if req.receptor.nrc: 
                    try: page.locator("input[formcontrolname='nrc']").first.fill(req.receptor.nrc.replace("-", ""))
                    except: pass
                if req.receptor.codActividad:
                    try:
                        cod = req.receptor.codActividad.split(" - ")[0].strip()
                        a = page.locator("ng-select[formcontrolname='actividadEconomica']").first
                        a.click(force=True); time.sleep(0.5)
                        a.locator("input[type='text']").first.fill(cod)
                        time.sleep(1)
                        page.locator(f"div.ng-option:has-text('{cod}')").first.click(force=True)
                        time.sleep(0.5)
                    except: pass

            try: page.locator("input[formcontrolname='nombre']").first.fill(req.receptor.nombre)
            except: pass
            
            dir_val = req.receptor.direccion.strip()
            if not dir_val: dir_val = "San Salvador, El Salvador"
            try:
                comp = page.locator("textarea[formcontrolname='complemento']").first
                comp.clear(); comp.fill(dir_val); comp.press("Tab") 
            except: pass

            reportar_paso(task_id, "Agregando ítems...", page)
            for i, item in enumerate(req.items):
                if i == 0:
                    try:
                        page.locator("button:has-text('Agregar Detalle'), #btnGroupDrop2").first.click()
                        time.sleep(0.5)
                        page.locator("a.dropdown-item:has-text('Producto o Servicio')").first.click()
                        page.wait_for_selector("div.modal-dialog", timeout=5000)
                        time.sleep(0.5)
                    except: pass
                
                try: page.locator("select[formcontrolname='tipo']").first.select_option(label=TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien"))
                except: pass
                try: ci = page.locator("input[formcontrolname='cantidad']").first; ci.clear(); ci.fill(str(item.cantidad)); ci.press("Tab")
                except: pass
                try: page.locator("select[formcontrolname='unidad']").first.select_option(label="Unidad")
                except: pass
                try: desc_input = page.locator("input[formcontrolname='producto'], input[formcontrolname='descripcion']").first; desc_input.clear(); desc_input.fill(item.descripcion)
                except: pass
                try: page.locator("select[formcontrolname='tipoVenta']").first.select_option(label=item.tipo_venta)
                except: pass
                try: pi = page.locator("input[formcontrolname='precio'], input[formcontrolname='precioUnitario']").first; pi.clear(); pi.fill(f"{item.precio:.2f}"); pi.press("Tab"); time.sleep(0.5)
                except: pass
                
                # INYECCIÓN JS 1: Forzar Agregar Item
                try: 
                    page.evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Agregar ítem') || b.textContent.includes('Agregar Ítem')).click();")
                    time.sleep(1.5)
                except: pass
                
                # INYECCIÓN JS 2: Forzar Cerrar popup de éxito
                try:
                    if i < len(req.items) - 1: page.evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Seguir adicionando')).click();")
                    else: page.evaluate("Array.from(document.querySelectorAll('button')).find(b => b.textContent.includes('Regresar al documento') || b.textContent.includes('OK')).click();")
                except: pass
                time.sleep(1)

                try:
                    btn_cancelar = page.locator("button[data-dismiss='modal']:has-text('Cancelar')").first
                    if btn_cancelar.is_visible(): btn_cancelar.click(force=True); time.sleep(0.5)
                except: pass

            if not es_nota:
                reportar_paso(task_id, "Validando Pagos...", page)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.5)
                
                # INYECCIÓN JS 3: ELIMINAR FILAS DE PAGO AUTO-CREADAS POR HACIENDA
                try:
                    page.evaluate("""
                        const btns = document.querySelectorAll('table tbody tr button.btn-outline-primary');
                        btns.forEach(b => b.click());
                    """)
                    time.sleep(0.5)
                except: pass

                fp_val = {"01": "0: 01", "02": "3: 04", "03": "4: 05", "07": "1: 02", "99": "11: 99"}.get(forma_pago, "0: 01")
                try: page.locator("select[formcontrolname='codigo']").first.select_option(value=fp_val)
                except: pass
                try:
                    mp = page.locator("input[formcontrolname='montoPago']").first
                    mp.clear(); mp.fill(f"{monto_pago:.2f}"); mp.press("Tab"); time.sleep(0.5)
                except: pass
                
                # INYECCIÓN JS 4: AGREGAR FORMA DE PAGO SIN IMPORTAR OBSTÁCULOS
                try: 
                    page.evaluate("""
                        const btn = document.querySelector("button[tooltip='Agregar forma de pago']") || Array.from(document.querySelectorAll('button')).find(b => b.innerHTML.includes('fa-plus'));
                        if(btn) btn.click();
                    """)
                    time.sleep(1)
                except: pass

            reportar_paso(task_id, "Firma y Envío...", page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)
            
            try: page.locator("input[value='Generar Documento'], button:has-text('Generar Documento')").first.click(force=True); time.sleep(1)
            except: pass
            
            # INYECCIÓN JS 5: CONFIRMAR CREACIÓN AUNQUE HAYA OVERLAYS
            try: 
                page.evaluate("Array.from(document.querySelectorAll('button.swal2-confirm')).find(b => b.textContent.includes('crear documento')).click();")
                time.sleep(1)
            except: 
                errs = page.locator("div[style*='background-color: #f8d7da'], div.alert-danger").all_inner_texts()
                if errs: raise Exception("Datos incompletos en el formulario: " + " | ".join([e.replace('×', '').strip() for e in errs]))
            
            reportar_paso(task_id, "Clave Privada...", page)
            try:
                ic = page.get_by_placeholder("Ingrese la clave privada de validación")
                ic.wait_for(state="visible", timeout=8000)
                ic.fill(hw_clave)
                page.evaluate("Array.from(document.querySelectorAll('button.swal2-confirm')).find(b => b.textContent === 'OK').click();")
            except: pass

            reportar_paso(task_id, "Esperando sello oficial...", page)
            codigo_generacion, pdf_base64 = "", ""
            def _extract_uuid(text):
                m = re.search(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", text)
                return m.group(0).upper() if m else ""

            try:
                page.wait_for_selector(".swal2-popup, .swal2-content", timeout=20000)
                time.sleep(1.5)
                body_txt = page.inner_text("body")
                if "incorrecta" in body_txt.lower() or "inválida" in body_txt.lower(): raise Exception("Clave privada incorrecta.")
                codigo_generacion = _extract_uuid(body_txt)
            except Exception as e:
                if "Clave" in str(e) or "incompleto" in str(e): raise e

            t0 = time.time()
            pdf_tab = None
            while time.time() - t0 < 10:
                for pg in context.pages:
                    if "pdf" in pg.url.lower() or "blob:" in pg.url.lower(): pdf_tab = pg; break
                if pdf_tab: break
                time.sleep(1)

            if pdf_tab:
                if not codigo_generacion:
                    codigo_generacion = _extract_uuid(pdf_tab.url) or _extract_uuid(pdf_tab.title())
                try:
                    pdf_bytes_js = pdf_tab.evaluate("async () => { const r = await fetch(document.URL); const buf = await r.arrayBuffer(); return Array.from(new Uint8Array(buf)); }")
                    pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode('utf-8')
                except: pass

            for btn_txt in ["OK", "Aceptar", "Cerrar"]:
                try: page.locator(f"button:has-text('{btn_txt}')").last.click(force=True, timeout=1000); time.sleep(0.3)
                except: pass

            if not pdf_base64 and codigo_generacion:
                reportar_paso(task_id, "Recuperando PDF...", page)
                try:
                    from urllib.parse import urlparse
                    base_url = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}"
                    page.goto(f"{base_url}/consultaDteEmitidos", wait_until="domcontentloaded"); time.sleep(1)
                    page.locator("input[formcontrolname='codigoGeneracion']").first.fill(codigo_generacion)
                    page.locator("button:has-text('Consultar')").first.click()
                    page.wait_for_selector("tbody tr", timeout=10000); time.sleep(1.5)
                    pages_antes = set(id(p) for p in context.pages)
                    page.locator("button[tooltip='Versión legible']").first.click(force=True); time.sleep(3)
                    for pg in context.pages:
                        if id(pg) not in pages_antes or "pdf" in pg.url.lower() or "blob:" in pg.url.lower():
                            try:
                                pdf_bytes_js = pg.evaluate("async () => { const r = await fetch(document.URL); const buf = await r.arrayBuffer(); return Array.from(new Uint8Array(buf)); }")
                                pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode('utf-8')
                            except: pass
                            break
                except: pass

            if not codigo_generacion: raise Exception("El portal de Hacienda no devolvió el UUID. Ver monitor.")

            browser.close()
            TAREAS[task_id] = {"status": "completado", "exito": True, "sello_recepcion": "IMPRESO-EN-EL-PDF", "codigo_generacion": codigo_generacion, "pdf_base64": pdf_base64, "json_content": "{}"}

    except Exception as e:
        try: reportar_paso(task_id, f"ERROR CRÍTICO: {str(e)}", page)
        except: pass
        TAREAS[task_id] = {"status": "error", "exito": False, "detail": str(e), "screenshot": TAREAS[task_id].get("screenshot", "")}

@app.post("/facturar")
def facturar_inmediato(req: FacturaRequest, bg_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    bg_tasks.add_task(procesar_dte_en_fondo, task_id, req)
    return {"exito": True, "task_id": task_id, "status": "procesando"}

@app.get("/status/{task_id}")
def verificar_status(task_id: str): return TAREAS.get(task_id, {"status": "no_encontrado"})
