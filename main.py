from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time, re, base64, uuid, json, traceback

app = FastAPI()
TAREAS: dict = {}

# ── Modelos ───────────────────────────────────────────────────────────────────
class Receptor(BaseModel):
    tipo_doc: str = "NIT"
    numDocumento: str = ""
    nombre: str = ""
    nrc: str = ""
    codActividad: str = ""
    departamento: str = ""
    municipio: str = ""
    direccion: str = ""
    correo: str = ""
    telefono: str = ""

class Item(BaseModel):
    cantidad: float = 1
    descripcion: str = ""
    precio: float = 0
    tipo_item: str = "1 - Bien"
    tipo_venta: str = "Gravado"

class FacturaRequest(BaseModel):
    nit_empresa: str = ""
    clave_hacienda: str = ""
    clave_firma: str = ""
    tipo_dte: str = "Comprobante de Crédito Fiscal"
    receptor: Receptor = Receptor()
    items: list[Item] = []
    formas_pago: list[str] = ["01"]
    condicion: str = "Contado"
    observaciones: str = ""
    doc_rel_cod_generacion: str = ""
    doc_rel_tipo_documento: str = "Comprobante de Crédito Fiscal"
    doc_rel_fecha_desde: str = None

# ── Constantes y Helpers ──────────────────────────────────────────────────────
FP_MAP = {"01": "0: 01", "02": "1: 02", "03": "2: 03", "04": "3: 04", "05": "4: 05", "07": "1: 02", "99": "11: 99"}
TIPO_ITEM_MAP = {"1 - Bien": "1 - Bien", "2 - Servicio": "2 - Servicio", "3 - Bien y servicio": "3 - Bien y servicio"}

def _shot(page) -> str:
    try: return base64.b64encode(page.screenshot(type="jpeg", quality=40)).decode("utf-8")
    except: return ""

def _rep(task_id: str, msg: str, page=None):
    print(f"[RPA] {msg}")
    TAREAS[task_id]["mensaje"] = msg
    if page:
        s = _shot(page)
        if s: TAREAS[task_id]["screenshot"] = s

def _extract_uuid(text: str) -> str:
    m = re.search(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", text)
    return m.group(0).upper() if m else ""

def type_into(page, selector: str, value: str, timeout: int = 10000) -> bool:
    if not value: return True
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.scroll_into_view_if_needed()
        el.click()
        el.fill("")
        time.sleep(0.1)
        el.type(str(value), delay=50)
        time.sleep(0.2)
        el.press("Tab")
        try: page.evaluate(f"(sel) => {{ const e = document.querySelector(sel); if(e) {{ e.dispatchEvent(new Event('input', {{bubbles: true}})); e.dispatchEvent(new Event('change', {{bubbles: true}})); }} }}", selector)
        except: pass
        return True
    except Exception as e:
        print(f"  [ERROR] type_into '{selector}': {e}")
        return False

def select_into(page, selector: str, value: str = None, label: str = None, timeout: int = 8000) -> bool:
    if not value and not label: return True
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.scroll_into_view_if_needed()
        if value: el.select_option(value=value)
        else: el.select_option(label=label)
        time.sleep(0.4)
        return True
    except Exception as e:
        print(f"  [ERROR] select_into '{selector}': {e}")
        return False

def fill_ngselect(page, selector: str, search: str, timeout: int = 8000) -> bool:
    if not search: return True
    cod = search.split(" - ")[0].strip()
    try:
        comp = page.locator(selector).first
        comp.wait_for(state="visible", timeout=timeout)
        comp.scroll_into_view_if_needed()
        comp.click(force=True)
        time.sleep(0.5)
        inp = comp.locator("input[type='text']").first
        inp.wait_for(state="visible", timeout=3000)
        inp.type(cod, delay=50)
        time.sleep(1.5)
        opt = page.locator("div.ng-option:not(.ng-option-disabled)").first
        opt.wait_for(state="visible", timeout=5000)
        opt.click(force=True)
        time.sleep(0.4)
        return True
    except Exception as e:
        print(f"  [ERROR] ng-select '{selector}': {e}")
        return False

# ── ENDPOINTS ─────────────────────────────────────────────────────────────────
@app.get("/ping")
def ping(): return {"status": "ok"}

@app.get("/status/{task_id}")
def verificar_status(task_id: str): return TAREAS.get(task_id, {"status": "no_encontrado"})

@app.post("/facturar")
async def facturar_inmediato(request: Request, bg_tasks: BackgroundTasks):
    try:
        body_bytes = await request.body()
        data = json.loads(body_bytes.decode("utf-8"))
        req  = FacturaRequest(**data)
        task_id = str(uuid.uuid4())
        bg_tasks.add_task(procesar_dte_en_fondo, task_id, req)
        return JSONResponse({"exito": True, "task_id": task_id, "status": "procesando"})
    except Exception as e:
        return JSONResponse({"exito": False, "error": f"Error parseando datos: {str(e)}"}, status_code=400)

# ── PROCESO PRINCIPAL ─────────────────────────────────────────────────────────
def procesar_dte_en_fondo(task_id: str, req: FacturaRequest):
    TAREAS[task_id] = {"status": "procesando", "mensaje": "Iniciando navegador...", "screenshot": ""}

    hw_user  = req.nit_empresa
    hw_pass  = req.clave_hacienda
    hw_clave = req.clave_firma if req.clave_firma else req.clave_hacienda
    tipo_dte = req.tipo_dte
    es_nota  = tipo_dte in ("Nota de Crédito", "Nota de Débito")
    fp_code  = req.formas_pago[0] if req.formas_pago else "01"

    monto_pago = sum(i.cantidad * i.precio for i in req.items)
    if "Crédito Fiscal" in tipo_dte: monto_pago *= 1.13

    page = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(viewport={"width": 1366, "height": 768}, accept_downloads=True, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            page = context.new_page()

            # ── 1. LOGIN DIRECTO AL PORTAL INTERNO (Evita el problema de pestañas y timeouts) ──
            _rep(task_id, "Entrando directo al portal de Hacienda...", page)
            
            # Aumentamos el timeout a 60 segundos y vamos directo a admin.factura.gob.sv
            page.goto("https://admin.factura.gob.sv/", timeout=60000)
            time.sleep(1.5)

            # Si aparece la tarjeta de "Emisores DTE" (A veces la muestra, a veces no)
            _rep(task_id, "Buscando botón Emisores DTE...", page)
            try:
                btn_emisores = page.locator("button:has-text('Iniciar Sesión'), button:has-text('Emisores DTE')").first
                if btn_emisores.is_visible(timeout=5000):
                    btn_emisores.click()
                    time.sleep(1)
            except: pass

            _rep(task_id, "Escribiendo credenciales...", page)
            nit_input = page.locator("input[placeholder*='NIT'], input[placeholder*='DUI'], input[formcontrolname='usuario']").first
            nit_input.wait_for(state="visible", timeout=15000)
            nit_input.fill(hw_user.replace("-", ""))

            pass_input = page.locator("input[type='password'], input[formcontrolname='clave']").first
            pass_input.fill(hw_pass)

            try:
                sa = page.locator("select[formcontrolname='ambiente']").first
                sa.wait_for(state="visible", timeout=3000)
                sa.select_option(value="/test")
                time.sleep(0.5)
            except: pass

            page.locator("button:has-text('Iniciar sesión'), button[type='submit']").first.click()

            try:
                page.wait_for_selector(".swal2-confirm", timeout=5000)
                page.click(".swal2-confirm")
            except: pass

            _rep(task_id, "Login exitoso ✅", page)
            time.sleep(2)

            # ── 2. SISTEMA DE FACTURACIÓN ──
            _rep(task_id, "Abriendo Facturación...", page)
            try:
                menu = page.locator("a[href='/facturadorv3'], span:has-text('Sistema de Facturación')").first
                menu.wait_for(state="visible", timeout=15000)
                menu.click(force=True)
                page.wait_for_load_state("networkidle")
                time.sleep(2)
            except Exception as e:
                _rep(task_id, f"Error abriendo menú: {e}", page)

            # ── 3. TIPO DTE ──
            _rep(task_id, f"Seleccionando: {tipo_dte}", page)
            try:
                page.wait_for_selector(".swal2-popup select, select.swal2-select", timeout=10000)
                page.locator(".swal2-popup select, select.swal2-select").first.select_option(label=tipo_dte)
                page.locator("button.swal2-confirm, button:has-text('OK')").first.click()
                time.sleep(2)
            except: pass

            # ── 4. RECEPTOR ──
            _rep(task_id, "Llenando datos del cliente...", page)
            r = req.receptor
            nit_clean = r.numDocumento.replace("-", "").strip()

            if not ("Crédito Fiscal" in tipo_dte or es_nota):
                select_into(page, "select[formcontrolname='tipoDocumento']", label=r.tipo_doc)

            ok_nit = type_into(page, "input[formcontrolname='nit']", nit_clean, timeout=5000)
            if not ok_nit: type_into(page, "input[placeholder*='NIT'], input[formcontrolname='numDocumento']", nit_clean)

            type_into(page, "input[formcontrolname='nombre']", r.nombre)

            if "Crédito Fiscal" in tipo_dte or es_nota:
                if r.nrc: type_into(page, "input[formcontrolname='nrc']", r.nrc.replace("-", ""))
                if r.codActividad: fill_ngselect(page, "ng-select[formcontrolname='actividadEconomica']", r.codActividad)

            select_into(page, "select[formcontrolname='departamento']", value=r.departamento.split(" - ")[0].strip().zfill(2) if r.departamento else "06")
            time.sleep(0.5)
            select_into(page, "select[formcontrolname='municipio']", value=r.municipio.split(" - ")[0].strip() if r.municipio else "")

            type_into(page, "textarea[formcontrolname='complemento']", r.direccion or "San Salvador")
            if r.correo: type_into(page, "input[formcontrolname='correo']", r.correo)
            if r.telefono: type_into(page, "input[formcontrolname='telefono']", r.telefono.replace("-", ""))

            # ── 4.5 DOCUMENTO RELACIONADO (NC/ND) ──
            if es_nota and req.doc_rel_cod_generacion:
                _rep(task_id, "Agregando documento relacionado...", page)
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.8)
                    page.locator("#btnGroupDrop1, button.dropdown-toggle:has-text('Agregar Doc')").first.click()
                    time.sleep(0.6)
                    page.locator("a.dropdown-item:has-text('Electrónico')").first.click()
                    time.sleep(1.2)
                    
                    sel_modal = page.locator("div.modal-body select, div.modal select").filter(has_text="Comprobante").first
                    sel_modal.wait_for(state="visible", timeout=8000)
                    sel_modal.select_option(label=req.doc_rel_tipo_documento)
                    
                    btn_consultar = page.locator("div.modal-body button:has-text('Consultar')").first
                    btn_consultar.click()
                    page.wait_for_selector("div.modal-body tbody tr", timeout=15000)
                    time.sleep(1.5)
                    
                    filas = page.locator("div.modal-body tbody tr").all()
                    cod_buscar = req.doc_rel_cod_generacion.strip().upper().replace("-", "")
                    for fila in filas:
                        if cod_buscar in fila.inner_text().upper().replace("-", ""):
                            fila.locator("input[type='button'][value='+'], button:has-text('+')").first.click(force=True)
                            break
                    
                    page.wait_for_selector("div.modal.show", state="hidden", timeout=10000)
                except Exception as e:
                    _rep(task_id, f"Error en doc relacionado: {e}", page)

            # ── 5. ÍTEMS ──
            _rep(task_id, f"Agregando {len(req.items)} producto(s)...", page)
            for i, item in enumerate(req.items):
                if i == 0:
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                        btn = page.locator("#btnGroupDrop2, button:has-text('Agregar Detalle'), button:has-text('Agregar Ítem')").first
                        btn.wait_for(state="visible", timeout=8000)
                        btn.click()
                        time.sleep(0.5)
                        page.locator("a.dropdown-item:has-text('Producto o Servicio'), a:has-text('Producto o Servicio')").first.click()
                        page.wait_for_selector("div.modal.show, div.modal-dialog", timeout=8000)
                        time.sleep(1)
                    except: pass
                else:
                    time.sleep(1)

                m = "div.modal-dialog "
                select_into(page, f"{m}select[formcontrolname='tipo']", label=TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien"))
                
                cant_str = str(int(item.cantidad)) if item.cantidad == int(item.cantidad) else str(item.cantidad)
                type_into(page, f"{m}input[formcontrolname='cantidad']", cant_str)
                
                select_into(page, f"{m}select[formcontrolname='unidad']", label="Unidad")
                type_into(page, f"{m}input[formcontrolname='descripcion']", item.descripcion)
                select_into(page, f"{m}select[formcontrolname='tipoVenta']", label=item.tipo_venta)
                type_into(page, f"{m}input[formcontrolname='precioUnitario'], {m}input[formcontrolname='precio']", f"{item.precio:.2f}")

                time.sleep(0.5)
                try:
                    page.locator("div.modal button.btn-primary:last-of-type").last.click(force=True)
                    time.sleep(1.5)
                except: pass

                if i < len(req.items) - 1:
                    try: page.locator("button:has-text('Seguir adicionando'), .swal2-confirm").first.click(force=True)
                    except: pass
                else:
                    try: page.locator("button:has-text('Regresar al documento'), .swal2-confirm").first.click(force=True)
                    except: pass
                    time.sleep(1.5)

            # ── 6. FORMA DE PAGO ──
            if not es_nota:
                _rep(task_id, "Configurando pago...", page)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1)
                
                try:
                    btns = page.locator("button[tooltip='Borrar forma de pago']").all()
                    for b in reversed(btns): b.click(force=True)
                except: pass

                select_into(page, "select[formcontrolname='codigo']", value=FP_MAP.get(fp_code, "0: 01"))
                type_into(page, "input[formcontrolname='montoPago']", f"{monto_pago:.2f}")
                
                try:
                    page.locator("button[tooltip='Agregar forma de pago'], button.btn-primary:has(i.fa-plus)").first.click(force=True)
                    time.sleep(1)
                except: pass
            else:
                select_into(page, "select[formcontrolname='condicionOperacion']", label=req.condicion)

            # ── 7. GENERAR Y FIRMAR ──
            _rep(task_id, "Generando documento...", page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)

            try:
                page.locator("input[type='submit'][value='Generar Documento'], button:has-text('Generar Documento')").last.click(force=True)
                time.sleep(1.5)
                page.locator("button.swal2-confirm:has-text('crear documento'), button.swal2-confirm").first.click(force=True)
                time.sleep(1.5)
            except: pass

            _rep(task_id, "Firmando con clave privada...", page)
            try:
                ic = page.get_by_placeholder("Ingrese la clave privada de validación")
                ic.wait_for(state="visible", timeout=10000)
                ic.fill(hw_clave)
                page.locator("button:has-text('OK')").last.click(force=True)
            except: pass

            # ── 8. EXTRAER UUID ──
            _rep(task_id, "Esperando respuesta de Hacienda...", page)
            codigo_generacion = ""
            try:
                page.wait_for_selector(".swal2-popup", timeout=20000)
                time.sleep(2)
                codigo_generacion = _extract_uuid(page.inner_text("body"))
            except: pass

            if not codigo_generacion:
                t0 = time.time()
                while time.time() - t0 < 10:
                    for pg in context.pages:
                        if "pdf" in pg.url or "blob" in pg.url:
                            codigo_generacion = _extract_uuid(pg.url) or _extract_uuid(pg.title())
                            break
                    if codigo_generacion: break
                    time.sleep(1)

            if not codigo_generacion:
                raise Exception("Hacienda no devolvió el UUID. Revisa las credenciales o el monitor.")

            for txt in ["OK", "Aceptar", "Cerrar"]:
                try: page.locator(f"button:has-text('{txt}')").last.click(force=True, timeout=1000)
                except: pass

            # ── 9. DESCARGAR PDF Y JSON ──
            _rep(task_id, f"¡Aprobado! Descargando archivos (UUID: {codigo_generacion})...", page)
            pdf_base64 = ""
            sello_recepcion = "SELLO-NO-ENCONTRADO"
            try:
                from urllib.parse import urlparse
                base_url = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}"
                page.goto(f"{base_url}/consultaDteEmitidos", wait_until="domcontentloaded")
                time.sleep(1)

                page.locator("input[formcontrolname='codigoGeneracion']").first.fill(codigo_generacion)
                page.locator("button:has-text('Consultar')").first.click()
                page.wait_for_selector("tbody tr", timeout=10000)
                time.sleep(1)

                pages_antes = set(id(pg) for pg in context.pages)
                page.locator("button[tooltip='Versión legible'], button:has(i.fa-print)").first.click(force=True)
                time.sleep(3)

                for pg in context.pages:
                    if id(pg) not in pages_antes or "pdf" in pg.url or "blob" in pg.url:
                        bts = pg.evaluate("""async () => { const r = await fetch(document.URL); const b = await r.arrayBuffer(); return Array.from(new Uint8Array(b)); }""")
                        pdf_base64 = base64.b64encode(bytes(bts)).decode("utf-8")
                        pg.close()
                        break
            except Exception as e:
                print(f"Error descargando PDF: {e}")

            browser.close()

            TAREAS[task_id] = {
                "status": "completado",
                "exito": True,
                "sello_recepcion": sello_recepcion,
                "codigo_generacion": codigo_generacion,
                "pdf_base64": pdf_base64,
                "json_content": "{}"
            }
            print(f"[RPA] ✅ COMPLETADO: {codigo_generacion}")

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[RPA] ❌ ERROR: {e}\n{tb}")
        try: _rep(task_id, f"❌ ERROR: {str(e)}", page)
        except: pass
        TAREAS[task_id] = {
            "status": "error", "exito": False, "detail": str(e),
            "screenshot": TAREAS.get(task_id, {}).get("screenshot", "")
        }
