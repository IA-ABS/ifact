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

def reportar_paso(task_id, mensaje, page=None):
    b64_img = ""
    if page:
        try:
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

            # ==========================================================
            # SECCIÓN CORREGIDA: LLENADO DEL MODAL AZUL DE ÍTEMS
            # ==========================================================
            reportar_paso(task_id, "Abriendo panel de productos...", page)
            for i, item in enumerate(req.items):
                if i == 0:
                    try:
                        page.locator("button:has-text('Agregar Detalle'), #btnGroupDrop2").first.click()
                        time.sleep(0.5)
                        page.locator("a.dropdown-item:has-text('Producto o Servicio')").first.click()
                        page.wait_for_selector("div.modal-dialog", timeout=5000)
                        time.sleep(0.5)
                    except: pass
                
                reportar_paso(task_id, f"Llenando datos del producto {i+1}...", page)
                
                # 1. Tipo
                try: page.locator("select[formcontrolname='tipo']").first.select_option(label=TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien"))
                except: pass
                
                # 2. Cantidad
                try:
                    ci = page.locator("input[formcontrolname='cantidad']").first
                    ci.clear(); ci.fill(str(item.cantidad)); ci.press("Tab")
                except: pass
                
                # 3. Unidad
                try: page.locator("select[formcontrolname='unidad']").first.select_option(label="Unidad")
                except: pass
                
                # 4. Descripción del Producto (CORREGIDO: formcontrolname='producto')
                try:
                    desc_input = page.locator("input[formcontrolname='producto'], input[formcontrolname='descripcion'], input[placeholder='Nombre Producto']").first
                    desc_input.clear(); desc_input.fill(item.descripcion)
                except: pass
                
                # 5. Tipo Venta
                try: page.locator("select[formcontrolname='tipoVenta']").first.select_option(label=item.tipo_venta)
                except: pass
                
                # 6. Precio (CORREGIDO: formcontrolname='precio')
                try:
                    pi = page.locator("input[formcontrolname='precio'], input[formcontrolname='precioUnitario']").first
                    pi.clear(); pi.fill(f"{item.precio:.2f}"); pi.press("Tab")
                    time.sleep(0.5) # Esperar a que se calcule el Total abajo
                except: pass
                
                reportar_paso(task_id, f"Guardando producto {i+1}...", page)
                
                # 7. Clic en "Agregar ítem"
                try: 
                    # Selector infalible basado en tu HTML
                    btn_add = page.locator("button[ngbpopover='Adicionar al documento.'], button.btn-primary:has-text('Agregar ítem'), button.btn-primary:has-text('Agregar Ítem')").first
                    btn_add.click(force=True)
                    time.sleep(1.5)
                except: pass
                
                # 8. Manejar el popup verde de éxito
                try:
                    if i < len(req.items) - 1:
                        page.locator("button:has-text('Seguir adicionando')").first.click(force=True)
                    else:
                        page.locator("button:has-text('Regresar al documento'), button.swal2-confirm:has-text('OK')").first.click(force=True)
                except: pass
                
                time.sleep(1)

                # 9. RED DE SEGURIDAD: Si el modal azul sigue abierto tapando todo, forzamos el cierre
                try:
                    btn_cancelar = page.locator("button[data-dismiss='modal']:has-text('Cancelar')").first
                    if btn_cancelar.is_visible():
                        btn_cancelar.click(force=True)
                        time.sleep(0.5)
                except: pass
            
            # ==========================================================

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
                
                # CORREGIDO: Selector exacto para el botón +
                try: 
                    btn_plus = page.locator("button[tooltip='Agregar forma de pago'], button.btn-block:has(i.fa-plus)").first
                    btn_plus.click(force=True)
                    time.sleep(1)
                except: pass

            reportar_paso(task_id, "Generando Documento Final...", page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)
            
            # CORREGIDO: Selector exacto de Generar Documento
            try: 
                btn_gen = page.locator("input[value='Generar Documento'], button:has-text('Generar Documento')").first
                btn_gen.click(force=True)
                time.sleep(1)
            except: pass
            
            # CORREGIDO: Selector exacto SweetAlert de confirmación
            try: 
                btn_confirm = page.locator("button.swal2-confirm:has-text('Si, crear documento')").first
                btn_confirm.wait_for(state="visible", timeout=5000)
                btn_confirm.click(force=True)
            except: pass
            
            reportar_paso(task_id, "Ingresando Clave Privada...", page)
            try:
                ic = page.get_by_placeholder("Ingrese la clave privada de validación")
                ic.wait_for(state="visible", timeout=12000)
                ic.fill(hw_clave)
                bok = page.locator("button.swal2-confirm:has-text('OK')").last
                bok.wait_for(state="visible", timeout=5000)
                bok.click(force=True)
            except: pass

            reportar_paso(task_id, "Esperando respuesta oficial de Hacienda (UUID)...", page)
            codigo_generacion = ""
            pdf_base64 = ""
            
            def _extract_uuid(text):
                m = re.search(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", text)
                return m.group(0).upper() if m else ""

            try:
                page.wait_for_selector(".swal2-popup, .swal2-content", timeout=20000)
                time.sleep(1.5)
                body_txt = page.inner_text("body")
                if "incorrecta" in body_txt.lower() or "inválida" in body_txt.lower() or "rechazado" in body_txt.lower():
                    raise Exception("Hacienda rechazó la emisión. Clave privada incorrecta o inválida.")
                codigo_generacion = _extract_uuid(body_txt)
            except Exception as e:
                if "Hacienda rechazó" in str(e): raise e

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
                    codigo_generacion = _extract_uuid(pdf_tab.url)
                    if not codigo_generacion:
                        codigo_generacion = _extract_uuid(pdf_tab.title())
                try:
                    pdf_bytes_js = pdf_tab.evaluate("""async () => {
                        const r = await fetch(document.URL);
                        const buf = await r.arrayBuffer(); return Array.from(new Uint8Array(buf));
                    }""")
                    pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode('utf-8')
                except: pass

            for btn_txt in ["OK", "Aceptar", "Cerrar"]:
                try:
                    page.locator(f"button:has-text('{btn_txt}')").last.click(force=True, timeout=1000)
                    time.sleep(0.3)
                except: pass

            if not pdf_base64 and codigo_generacion:
                reportar_paso(task_id, "Recuperando PDF desde Consultas...", page)
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(page.url)
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
                    page.goto(f"{base_url}/consultaDteEmitidos", wait_until="domcontentloaded")
                    time.sleep(1)

                    input_cod = page.locator("input[formcontrolname='codigoGeneracion'], input[placeholder*='0000']").first
                    input_cod.wait_for(state="visible", timeout=10000)
                    input_cod.fill(codigo_generacion)
                    page.locator("button:has-text('Consultar')").first.click()
                    page.wait_for_selector("tbody tr", timeout=15000)
                    time.sleep(1.5)

                    pages_antes = set(id(p) for p in context.pages)
                    page.locator("button[tooltip='Versión legible'], button:has(i.fa-print)").first.click(force=True)
                    time.sleep(3)

                    for pg in context.pages:
                        if id(pg) not in pages_antes or "pdf" in pg.url.lower() or "blob:" in pg.url.lower():
                            try:
                                pdf_bytes_js = pg.evaluate("""async () => {
                                    const r = await fetch(document.URL);
                                    const buf = await r.arrayBuffer();
                                    return Array.from(new Uint8Array(buf));
                                }""")
                                pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode('utf-8')
                            except: pass
                            break
                except: pass

            if not codigo_generacion:
                raise Exception("El portal de Hacienda no devolvió el UUID. Ver monitor para detalles.")

            browser.close()
            TAREAS[task_id] = {
                "status": "completado", "exito": True, "sello_recepcion": "IMPRESO-EN-EL-PDF", 
                "codigo_generacion": codigo_generacion, "pdf_base64": pdf_base64, "json_content": "{}"
            }

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
def verificar_status(task_id: str):
    return TAREAS.get(task_id, {"status": "no_encontrado"})
