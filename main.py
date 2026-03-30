from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time, re, json, base64, uuid

app = FastAPI()

# Diccionario en memoria para guardar el progreso de las facturas
TAREAS = {}

@app.get("/")
@app.get("/ping")
def ping():
    return {"status": "ok", "mensaje": "Robot despierto."}

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

# --- EL CÓDIGO PESADO DE PLAYWRIGHT AHORA SE EJECUTA EN SEGUNDO PLANO ---
def procesar_dte_en_fondo(task_id: str, req: FacturaRequest):
    TAREAS[task_id] = {"status": "procesando"}
    
    hw_user = req.nit_empresa
    hw_pass = req.clave_hacienda
    hw_clave = req.clave_firma if req.clave_firma else req.clave_hacienda
    nit_cli = req.receptor.numDocumento
    nom_cli = req.receptor.nombre
    nrc_cli = req.receptor.nrc
    act_cli = req.receptor.codActividad
    tipo_doc = "NIT"
    depto = req.receptor.departamento
    muni = req.receptor.municipio
    dir_cli = req.receptor.direccion
    mail_cli = req.receptor.correo
    tipo_dte = req.tipo_dte
    es_nota = tipo_dte in ("Nota de Crédito", "Nota de Débito")
    forma_pago = req.formas_pago[0] if req.formas_pago else "01"
    
    monto_pago = sum([i.cantidad * i.precio for i in req.items])
    if tipo_dte == "Comprobante de Crédito Fiscal": monto_pago = monto_pago * 1.13

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
            context = browser.new_context(viewport={"width": 1366, "height": 768}, accept_downloads=True)
            page = context.new_page()
            
            # Bloquear imágenes para ir más rápido
            page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media"] else route.continue_())

            # 1. Login
            page.goto("https://factura.gob.sv/", wait_until="domcontentloaded")
            with context.expect_page() as npi: page.click("text=Ingresar")
            page = npi.value
            page.wait_for_load_state("domcontentloaded")
            try: page.locator("//h5[contains(text(),'Emisores DTE')]/..//button").click()
            except: pass
            
            page.get_by_placeholder("NIT/DUI").fill(hw_user.replace("-", ""))
            page.locator("input[type='password']").fill(hw_pass)
            try:
                sel_ambiente = page.locator("select[formcontrolname='ambiente']").first
                sel_ambiente.wait_for(state="visible", timeout=3000)
                sel_ambiente.select_option(value="/test")
                time.sleep(0.5) 
            except: pass

            page.click("button:has-text('Iniciar sesión')")
            try:
                page.wait_for_selector(".swal2-confirm", timeout=3000)
                page.click(".swal2-confirm")
            except: pass

            # 2. Sistema Facturacion
            try:
                page.locator("text=Sistema de facturación").wait_for(state="visible", timeout=10000)
                page.click("text=Sistema de facturación", force=True)
                page.wait_for_load_state("domcontentloaded")
            except: pass

            # 3. Tipo DTE
            try:
                page.wait_for_selector(".swal2-popup select, select.swal2-select", timeout=10000)
                page.locator(".swal2-popup select, select.swal2-select").first.select_option(label=tipo_dte)
                page.locator("button.swal2-confirm, button:has-text('OK')").first.click()
            except: pass

            # 4. Receptor
            is_ccf_or_nota = ("Crédito Fiscal" in tipo_dte) or es_nota
            if not is_ccf_or_nota:
                try: page.locator("select[formcontrolname='tipoDocumento']").first.select_option(label=tipo_doc)
                except: pass

            nit_clean = nit_cli.replace("-", "")
            try: page.locator("input[placeholder^='Digite el número de']").first.fill(nit_clean)
            except: fill_field(page, "input[formcontrolname='numDocumento']", nit_clean)

            if is_ccf_or_nota:
                if nrc_cli: fill_field(page, "input[formcontrolname='nrc']", nrc_cli.replace("-", ""))
                if act_cli:
                    try:
                        cod_act = act_cli.split(" - ")[0].strip()
                        a = page.locator("ng-select[formcontrolname='actividadEconomica']").first
                        a.click()
                        a.locator("input").fill(cod_act)
                        page.locator(f"div.ng-option:has-text('{cod_act}')").first.click()
                    except: pass

            fill_field(page, "input[formcontrolname='nombre']", nom_cli)
            try: page.locator("select[formcontrolname='departamento']").first.select_option(value=depto.split(" - ")[0].strip())
            except: pass
            try: page.locator("select[formcontrolname='municipio']").first.select_option(label=muni.split(" - ")[1].strip() if " - " in muni else muni)
            except: pass
            fill_field(page, "textarea[formcontrolname='complemento']", dir_cli)
            try: page.locator("input[formcontrolname='correo']").first.fill(mail_cli)
            except: pass

            # 5. Items
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
                    ci.clear()
                    ci.fill(str(item.cantidad))
                    ci.press("Tab")
                except: pass
                select_opt(page, "xpath=//label[contains(text(),'Unidad')]/following-sibling::*//select", label="Unidad")
                fill_field(page, "input[formcontrolname='descripcion']", item.descripcion)
                select_opt(page, "xpath=//label[contains(text(),'Tipo Venta')]/following-sibling::*//select", label=item.tipo_venta)
                try:
                    pi = page.locator("input[formcontrolname='precioUnitario']").first
                    pi.clear()
                    pi.fill(f"{item.precio:.2f}")
                    pi.press("Tab")
                except: pass
                try: page.locator("button.btn-primary:has-text('Agregar ítem'), button.btn-primary:has-text('Agregar Ítem')").last.click()
                except: pass
                if i < len(req.items) - 1:
                    try: page.locator("button:has-text('Seguir adicionando')").click()
                    except: pass
                else:
                    try: page.locator("button:has-text('Regresar al documento')").click()
                    except: pass

            # 6. Pago
            if not es_nota:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.5)
                fp_val = {"01": "0: 01", "02": "3: 04", "03": "4: 05", "07": "1: 02", "99": "11: 99"}.get(forma_pago, "0: 01")
                try: page.locator("select[formcontrolname='codigo']").first.select_option(value=fp_val)
                except: pass
                try:
                    mp = page.locator("input[formcontrolname='montoPago']").first
                    mp.clear()
                    mp.fill(f"{monto_pago:.2f}")
                    mp.press("Tab")
                except: pass
                try: page.locator("button.btn-block:has(i.fa-plus)").first.click(force=True)
                except: pass

            # 7. Generar
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)
            try: page.locator("input[type='submit'][value='Generar Documento'], button:has-text('Generar Documento')").last.click(force=True)
            except: pass
            try: page.locator("button.swal2-confirm:has-text('Si, crear documento')").first.click(force=True)
            except: pass
            try:
                ic = page.get_by_placeholder("Ingrese la clave privada de validación")
                ic.wait_for(state="visible", timeout=12000)
                ic.fill(hw_clave)
                bok = page.locator("button:has-text('OK')").last
                bok.wait_for(state="visible", timeout=5000)
                bok.click(force=True)
            except: pass

            # ==========================================================
            # 8. ATRAPAR EL PDF AUTOMÁTICO Y EL UUID
            # ==========================================================
            codigo_generacion = ""
            pdf_base64 = ""
            
            def _extract_uuid(text):
                m = re.search(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", text)
                return m.group(0).upper() if m else ""

            # Esperamos hasta 20 segundos a que Hacienda genere y abra la nueva pestaña con el PDF
            t0 = time.time()
            pdf_tab = None
            while time.time() - t0 < 20:
                for pg in context.pages:
                    # Buscamos la pestaña que tiene el PDF o el blob
                    if "pdf" in pg.url.lower() or "blob:" in pg.url.lower():
                        pdf_tab = pg
                        break
                if pdf_tab:
                    break
                time.sleep(1)

            if pdf_tab:
                # 1. Sacamos el UUID de la URL del PDF
                codigo_generacion = _extract_uuid(pdf_tab.url)
                if not codigo_generacion:
                    codigo_generacion = _extract_uuid(pdf_tab.title())
                
                # 2. Convertimos ese PDF a Base64 para enviarlo al Frontend
                try:
                    pdf_bytes_js = pdf_tab.evaluate("""async () => {
                        const r = await fetch(document.URL);
                        const buf = await r.arrayBuffer();
                        return Array.from(new Uint8Array(buf));
                    }""")
                    pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode('utf-8')
                except: pass

            # Si por alguna razón la pestaña no se abrió, leemos el error de la pantalla principal
            if not codigo_generacion:
                try:
                    body_txt = page.inner_text("body")
                    if "incorrecta" in body_txt.lower() or "inválida" in body_txt.lower() or "rechazado" in body_txt.lower():
                        raise Exception("Hacienda rechazó la emisión. Clave privada incorrecta o inválida.")
                    codigo_generacion = _extract_uuid(body_txt)
                except Exception as e:
                    if "Hacienda rechazó" in str(e): raise e

            if not codigo_generacion:
                raise Exception("El portal de Hacienda no devolvió el UUID. Verifica tu Clave Privada.")

            browser.close()

            # Devolvemos el éxito inmediatamente. El Sello viene impreso en el PDF.
            TAREAS[task_id] = {
                "status": "completado",
                "exito": True,
                "sello_recepcion": "IMPRESO-EN-EL-PDF", 
                "codigo_generacion": codigo_generacion,
                "pdf_base64": pdf_base64,
                "json_content": "{}" # Dejamos el JSON vacío por ahora como pediste
            }

    except Exception as e:
        TAREAS[task_id] = {"status": "error", "exito": False, "detail": str(e)}

            # ==========================================================
            # 9. Ir a Consultas y Descargar (Lógica idéntica a Streamlit)
            # ==========================================================
            from urllib.parse import urlparse
            parsed = urlparse(page.url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            consulta_url = f"{base_url}/consultaDteEmitidos"

            page.goto(consulta_url, wait_until="domcontentloaded")
            time.sleep(1)

            input_cod = page.locator("input[formcontrolname='codigoGeneracion'], input[placeholder*='0000'], input[placeholder*='AAAA']").first
            input_cod.wait_for(state="visible", timeout=10000)
            input_cod.fill(codigo_generacion)
            
            page.locator("button:has-text('Consultar')").first.click()
            page.wait_for_selector("tbody tr", timeout=15000)
            time.sleep(1.5)

            # --- 9a. Descargar PDF ---
            pdf_base64 = ""
            try:
                pages_antes = set(id(p) for p in context.pages)
                page.locator("button[tooltip='Versión legible'], button:has(i.fa-print)").first.click(force=True)
                time.sleep(3)

                pdf_tab = None
                for pg in context.pages:
                    if id(pg) not in pages_antes:
                        pdf_tab = pg
                        break
                if not pdf_tab:
                    for pg in context.pages:
                        if "data:application/pdf" in pg.url or "blob:" in pg.url:
                            pdf_tab = pg
                            break

                if pdf_tab:
                    pdf_url = pdf_tab.url
                    if pdf_url.startswith("data:application/pdf;base64,"):
                        pdf_base64 = pdf_url.split(",", 1)[1]
                    elif "blob:" in pdf_url:
                        pdf_bytes_js = pdf_tab.evaluate("""async () => {
                            const r = await fetch(document.URL);
                            const buf = await r.arrayBuffer();
                            return Array.from(new Uint8Array(buf));
                        }""")
                        pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode('utf-8')
                    try: pdf_tab.close()
                    except: pass
            except: pass

            # --- 9b. Descargar JSON ---
            json_content = ""
            sello_recepcion = "SELLO-NO-ENCONTRADO"
            try:
                with context.expect_download(timeout=20000) as dl_info:
                    page.locator("button[tooltip='Descargar documento'], button:has(i.fas.fa-arrow-down), button:has(i.fa-arrow-down)").first.click(force=True)
                
                json_path = f"/tmp/{codigo_generacion}.json"
                dl_info.value.save_as(json_path)
                with open(json_path, "r", encoding="utf-8") as f:
                    json_content = f.read()
                
                try: sello_recepcion = json.loads(json_content).get("selloRecibido", "SELLO-NO-ENCONTRADO")
                except: pass
            except: 
                # Fallback de JSON si falla la descarga directa
                try:
                    pages_antes2 = set(id(p) for p in context.pages)
                    page.locator("button[tooltip='Descargar documento'], button:has(i.fas.fa-arrow-down), button:has(i.fa-arrow-down)").first.click(force=True)
                    time.sleep(2)
                    for pg in context.pages:
                        if id(pg) not in pages_antes2:
                            json_txt = pg.inner_text("body")
                            if json_txt.strip().startswith("{"):
                                json_content = json_txt
                                try: sello_recepcion = json.loads(json_content).get("selloRecibido", "SELLO-NO-ENCONTRADO")
                                except: pass
                            try: pg.close()
                            except: pass
                            break
                except: pass

            browser.close()

            TAREAS[task_id] = {
                "status": "completado",
                "exito": True,
                "sello_recepcion": sello_recepcion,
                "codigo_generacion": codigo_generacion,
                "pdf_base64": pdf_base64,
                "json_content": json_content
            }
    except Exception as e:
        TAREAS[task_id] = {"status": "error", "exito": False, "detail": str(e)}

# --- NUEVOS ENDPOINTS PARA CLOUDFLARE ---
@app.post("/facturar")
def facturar_inmediato(req: FacturaRequest, bg_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    # Asignamos el trabajo a un hilo secundario y respondemos en 0.1 segundos a Cloudflare
    bg_tasks.add_task(procesar_dte_en_fondo, task_id, req)
    return {"exito": True, "task_id": task_id, "status": "procesando"}

@app.get("/status/{task_id}")
def verificar_status(task_id: str):
    return TAREAS.get(task_id, {"status": "no_encontrado"})
