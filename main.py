from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time, re, json, base64, uuid

app = FastAPI()
TAREAS = {}

@app.get("/")
@app.get("/ping")
def ping(): return {"status": "ok", "mensaje": "Robot en línea"}

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

TIPO_ITEM_MAP = {
    "1 - Bien":            "1 - Bien",
    "2 - Servicio":        "2 - Servicio",
    "3 - Bien y servicio": "3 - Bien y servicio",
}

def screenshot_b64(page):
    try:
        return base64.b64encode(page.screenshot(type="jpeg", quality=50)).decode("utf-8")
    except:
        return ""

def reportar(task_id, mensaje, page=None):
    TAREAS[task_id]["mensaje"] = mensaje
    if page:
        s = screenshot_b64(page)
        if s:
            TAREAS[task_id]["screenshot"] = s

def _extract_uuid(text):
    m = re.search(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        text
    )
    return m.group(0).upper() if m else ""

def safe_fill(page, selector, value, timeout=6000):
    """Click -> clear -> fill -> Tab. Robusto para Angular."""
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.scroll_into_view_if_needed()
        el.click()
        time.sleep(0.2)
        el.clear()
        el.fill(str(value))
        el.press("Tab")
        time.sleep(0.2)
        return True
    except:
        return False

def safe_select(page, selector, label=None, value=None, timeout=5000):
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        if label:
            el.select_option(label=label)
        elif value:
            el.select_option(value=value)
        time.sleep(0.3)
        return True
    except:
        return False


def agregar_item_modal(page, task_id, item, es_primero, es_ultimo):
    """Agrega UN item en el modal del portal. Retorna True si exitoso."""

    # ── A: Abrir modal (solo primer item) ───────────────────────
    if es_primero:
        reportar(task_id, "Abriendo modal de items...", page)
        for sel_btn in [
            "button:has-text('Agregar Detalle')",
            "#btnGroupDrop2",
            "button:has-text('Agregar item')",
            "button:has-text('Agregar Item')",
        ]:
            try:
                btn = page.locator(sel_btn).first
                btn.wait_for(state="visible", timeout=5000)
                btn.scroll_into_view_if_needed()
                btn.click()
                time.sleep(0.6)
                break
            except:
                pass

        for sel_opt in [
            "a.dropdown-item:has-text('Producto o Servicio')",
            "a:has-text('Producto o Servicio')",
        ]:
            try:
                opt = page.locator(sel_opt).first
                opt.wait_for(state="visible", timeout=4000)
                opt.click()
                time.sleep(0.6)
                break
            except:
                pass

        # Esperar modal
        for sel_modal in [
            "div.modal.show",
            "div.modal-dialog",
            "label:has-text('Cantidad')",
        ]:
            try:
                page.wait_for_selector(sel_modal, state="visible", timeout=8000)
                break
            except:
                pass
        time.sleep(0.8)
        reportar(task_id, "Modal de item abierto", page)

    # ── B: Tipo de item ──────────────────────────────────────────
    tipo_label = TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien")
    for sel_tipo in [
        "select[formcontrolname='tipo']",
        "div.modal select:first-of-type",
    ]:
        if safe_select(page, sel_tipo, label=tipo_label, timeout=4000):
            break

    # ── C: Cantidad ──────────────────────────────────────────────
    cant_ok = False
    for sel_cant in [
        "input[formcontrolname='cantidad']",
        "input[placeholder='Cantidad']",
        "input[placeholder*='antidad']",
    ]:
        try:
            el = page.locator(sel_cant).first
            el.wait_for(state="visible", timeout=4000)
            el.scroll_into_view_if_needed()
            el.click()
            time.sleep(0.2)
            el.triple_click()
            el.fill(str(item.cantidad))
            el.press("Tab")
            time.sleep(0.3)
            cant_ok = True
            break
        except:
            pass

    # ── D: Unidad ────────────────────────────────────────────────
    for sel_uni in [
        "select[formcontrolname='unidad']",
    ]:
        if safe_select(page, sel_uni, label="Unidad", timeout=4000):
            break

    # ── E: Descripcion ───────────────────────────────────────────
    desc_ok = False
    for sel_desc in [
        "input[formcontrolname='descripcion']",
        "input[placeholder='Nombre Producto']",
        "input[placeholder*='escripcion']",
        "input[placeholder*='roducto']",
    ]:
        try:
            el = page.locator(sel_desc).first
            el.wait_for(state="visible", timeout=4000)
            el.scroll_into_view_if_needed()
            el.click()
            time.sleep(0.2)
            el.clear()
            el.fill(item.descripcion)
            time.sleep(0.2)
            desc_ok = True
            break
        except:
            pass

    # ── F: Tipo de venta ─────────────────────────────────────────
    for sel_tv in [
        "select[formcontrolname='tipoVenta']",
    ]:
        if safe_select(page, sel_tv, label=item.tipo_venta, timeout=4000):
            break

    # ── G: Precio unitario ───────────────────────────────────────
    precio_ok = False
    for sel_precio in [
        "input[formcontrolname='precioUnitario']",
        "input[formcontrolname='precio']",
        "input[placeholder*='recio']",
    ]:
        try:
            el = page.locator(sel_precio).first
            el.wait_for(state="visible", timeout=4000)
            el.scroll_into_view_if_needed()
            el.click()
            time.sleep(0.3)
            el.triple_click()
            el.fill(f"{item.precio:.2f}")
            el.press("Tab")
            time.sleep(0.6)  # Angular recalcula subtotal
            precio_ok = True
            break
        except:
            pass

    reportar(task_id,
        f"Item '{item.descripcion[:30]}' — cant:{cant_ok} desc:{desc_ok} precio:{precio_ok}",
        page)

    # ── H: Clic en boton "Agregar item" ─────────────────────────
    agregado = False
    for sel_add in [
        "button.btn-primary:has-text('Agregar item')",
        "button.btn-primary:has-text('Agregar Item')",
        "button[ngbpopover='Adicionar al documento.']",
        "div.modal-footer button.btn-primary",
        "div.modal button.btn-primary:last-child",
    ]:
        try:
            btn = page.locator(sel_add).last
            btn.wait_for(state="visible", timeout=5000)
            btn.scroll_into_view_if_needed()
            btn.click(force=True)
            agregado = True
            time.sleep(1.5)
            break
        except:
            pass

    if not agregado:
        # JS fallback
        try:
            page.evaluate("""
                const modal = document.querySelector('div.modal.show, div.modal-dialog');
                if (modal) {
                    const btns = modal.querySelectorAll('button.btn-primary');
                    const btn = btns[btns.length - 1];
                    if (btn) btn.click();
                }
            """)
            time.sleep(1.5)
            agregado = True
        except:
            pass

    reportar(task_id,
        f"Item {'agregado OK' if agregado else 'ERROR - no se agrego'}", page)

    # ── I: Navegar (Seguir / Regresar) ───────────────────────────
    time.sleep(0.5)
    if not es_ultimo:
        for sel_sig in ["button:has-text('Seguir adicionando')", ".swal2-confirm"]:
            try:
                btn = page.locator(sel_sig).first
                btn.wait_for(state="visible", timeout=5000)
                btn.click(force=True)
                time.sleep(0.8)
                break
            except:
                pass
    else:
        for sel_reg in [
            "button:has-text('Regresar al documento')",
            "button.swal2-confirm",
            ".swal2-confirm",
        ]:
            try:
                btn = page.locator(sel_reg).first
                btn.wait_for(state="visible", timeout=5000)
                btn.click(force=True)
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.0)
                break
            except:
                pass

    return agregado


def procesar_dte_en_fondo(task_id: str, req: FacturaRequest):
    TAREAS[task_id] = {"status": "procesando", "mensaje": "Iniciando navegador...", "screenshot": ""}

    hw_user    = req.nit_empresa
    hw_pass    = req.clave_hacienda
    hw_clave   = req.clave_firma if req.clave_firma else req.clave_hacienda
    tipo_dte   = req.tipo_dte
    es_nota    = tipo_dte in ("Nota de Credito", "Nota de Debito")
    forma_pago = req.formas_pago[0] if req.formas_pago else "01"

    monto_pago = sum(i.cantidad * i.precio for i in req.items)
    if "Credito Fiscal" in tipo_dte:
        monto_pago = monto_pago * 1.13

    page = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--window-size=1366,768",
                ]
            )
            context = browser.new_context(
                viewport={"width": 1366, "height": 768},
                accept_downloads=True,
                locale="es-SV",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            page.route("**/*", lambda route: route.abort()
                if route.request.resource_type in ["media"]
                else route.continue_())

            # ── 1. LOGIN ─────────────────────────────────────────
            reportar(task_id, "Abriendo portal Hacienda...", page)
            page.goto("https://factura.gob.sv/", wait_until="domcontentloaded")

            with context.expect_page() as npi:
                page.click("text=Ingresar")
            page = npi.value
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1.0)

            try:
                page.locator("//h5[contains(text(),'Emisores DTE')]/..//button").click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(0.5)
            except:
                pass

            page.get_by_placeholder("NIT/DUI").fill(hw_user.replace("-", ""))
            page.locator("input[type='password']").fill(hw_pass)

            try:
                sel_amb = page.locator("select[formcontrolname='ambiente']").first
                sel_amb.wait_for(state="visible", timeout=5000)
                sel_amb.select_option(value="/test")
                time.sleep(0.5)
            except:
                pass

            page.click("button:has-text('Iniciar sesion')")
            try:
                page.wait_for_selector(".swal2-confirm", timeout=4000)
                page.click(".swal2-confirm")
            except:
                pass

            reportar(task_id, "Login OK - entrando al sistema", page)
            time.sleep(1.0)

            # ── 2. SISTEMA DE FACTURACION ─────────────────────────
            try:
                page.locator("text=Sistema de facturacion").wait_for(state="visible", timeout=15000)
                page.click("text=Sistema de facturacion", force=True)
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.5)
            except:
                pass

            # ── 3. TIPO DE DOCUMENTO ──────────────────────────────
            reportar(task_id, f"Seleccionando: {tipo_dte}", page)
            try:
                page.wait_for_selector(
                    ".swal2-popup select, select.swal2-select", timeout=12000
                )
                page.locator(".swal2-popup select, select.swal2-select").first.select_option(
                    label=tipo_dte
                )
                page.locator("button.swal2-confirm, button:has-text('OK')").first.click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.5)
            except:
                pass

            reportar(task_id, "Pagina del documento cargada", page)

            # ── 4. RECEPTOR ───────────────────────────────────────
            reportar(task_id, "Llenando receptor...", page)
            is_ccf = ("Credito Fiscal" in tipo_dte) or es_nota
            nit_clean = req.receptor.numDocumento.replace("-", "")

            if not is_ccf:
                try:
                    td = page.locator("select[formcontrolname='tipoDocumento']").first
                    td.wait_for(state="visible", timeout=5000)
                    td.select_option(label="NIT")
                    time.sleep(0.5)
                except:
                    pass

            try:
                ni = page.locator("input[placeholder^='Digite el numero de']").first
                ni.wait_for(state="visible", timeout=10000)
                ni.fill(nit_clean)
            except:
                safe_fill(page, "input[formcontrolname='numDocumento']", nit_clean)

            if is_ccf:
                if req.receptor.nrc:
                    safe_fill(page, "input[formcontrolname='nrc']",
                              req.receptor.nrc.replace("-", ""))
                if req.receptor.codActividad:
                    try:
                        cod = req.receptor.codActividad.split(" - ")[0].strip()
                        a = page.locator("ng-select[formcontrolname='actividadEconomica']").first
                        a.wait_for(state="visible", timeout=5000)
                        a.click(force=True)
                        time.sleep(0.4)
                        a.locator("input[type='text']").first.fill(cod)
                        time.sleep(1.0)
                        page.locator(f"div.ng-option:has-text('{cod}')").first.click(force=True)
                        time.sleep(0.5)
                    except:
                        pass

            safe_fill(page, "input[formcontrolname='nombre']", req.receptor.nombre)

            depto = req.receptor.departamento
            depto_code = depto.split(" - ")[0].strip() if " - " in depto else depto
            try:
                d = page.locator("select[formcontrolname='departamento']").first
                d.wait_for(state="visible", timeout=5000)
                try:
                    d.select_option(value=depto_code)
                except:
                    d.select_option(label=depto)
            except:
                pass

            muni = req.receptor.municipio
            muni_label = muni.split(" - ")[1].strip() if " - " in muni else muni
            try:
                m = page.locator("select[formcontrolname='municipio']").first
                m.wait_for(state="visible", timeout=5000)
                try:
                    m.select_option(label=muni_label)
                except:
                    m.select_option(index=1)
            except:
                pass

            dir_val = req.receptor.direccion.strip() or "San Salvador, El Salvador"
            try:
                comp = page.locator("textarea[formcontrolname='complemento']").first
                comp.wait_for(state="visible", timeout=5000)
                comp.click()
                comp.clear()
                comp.fill(dir_val)
                comp.press("Tab")
            except:
                pass

            if req.receptor.correo:
                try:
                    e_inp = page.locator(
                        "input[formcontrolname='correo'], "
                        "input[formcontrolname='correoReceptor']"
                    ).first
                    e_inp.wait_for(state="visible", timeout=4000)
                    e_inp.fill(req.receptor.correo)
                except:
                    pass

            if req.receptor.telefono:
                try:
                    t_inp = page.locator(
                        "input[formcontrolname='telefono'], "
                        "input[formcontrolname='telefonoReceptor']"
                    ).first
                    t_inp.wait_for(state="visible", timeout=4000)
                    t_inp.fill(req.receptor.telefono)
                except:
                    pass

            reportar(task_id, "Receptor llenado OK", page)

            # ── 5. ITEMS ──────────────────────────────────────────
            reportar(task_id, f"Agregando {len(req.items)} item(s)...", page)

            for i, item in enumerate(req.items):
                agregar_item_modal(
                    page, task_id, item,
                    es_primero=(i == 0),
                    es_ultimo=(i == len(req.items) - 1)
                )

            # Pausa y screenshot para confirmar items en tabla
            time.sleep(1.0)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.5)
            reportar(task_id, "Items agregados - verificando tabla del documento", page)

            # ── 6. FORMA DE PAGO ──────────────────────────────────
            if not es_nota:
                reportar(task_id, "Configurando forma de pago...", page)
                fp_map = {
                    "01": "0: 01",
                    "02": "3: 04",
                    "03": "4: 05",
                    "07": "1: 02",
                    "99": "11: 99",
                }
                fp_val = fp_map.get(forma_pago, "0: 01")

                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.0)
                reportar(task_id, "Vista seccion forma de pago", page)

                # Borrar filas auto-generadas
                try:
                    btns_x = page.locator(
                        "table tbody tr button:has(i.fa-times)"
                    ).all()
                    for btn_x in reversed(btns_x):
                        try:
                            btn_x.click(force=True)
                            time.sleep(0.4)
                        except:
                            pass
                except:
                    pass

                # Select forma de pago
                for sel_fp in ["select[formcontrolname='codigo']"]:
                    try:
                        fp_el = page.locator(sel_fp).first
                        fp_el.wait_for(state="visible", timeout=6000)
                        fp_el.scroll_into_view_if_needed()
                        fp_el.select_option(value=fp_val)
                        time.sleep(0.4)
                        break
                    except:
                        pass

                # Monto
                for sel_mp in ["input[formcontrolname='montoPago']"]:
                    try:
                        mp = page.locator(sel_mp).first
                        mp.wait_for(state="visible", timeout=5000)
                        mp.scroll_into_view_if_needed()
                        mp.click()
                        time.sleep(0.2)
                        mp.triple_click()
                        mp.fill(f"{monto_pago:.2f}")
                        mp.press("Tab")
                        time.sleep(0.5)
                        break
                    except:
                        pass

                # Boton "+"
                added_fp = False
                for sel_plus in [
                    "button.btn-block:has(i.fa-plus)",
                    "button[tooltip='Agregar forma de pago']",
                    "button.btn-primary:has(i.fa-plus)",
                    "button:has(i.fa-plus)",
                ]:
                    try:
                        bp = page.locator(sel_plus).first
                        bp.wait_for(state="visible", timeout=4000)
                        bp.scroll_into_view_if_needed()
                        bp.click(force=True)
                        added_fp = True
                        time.sleep(1.0)
                        break
                    except:
                        pass

                if not added_fp:
                    try:
                        page.evaluate("""
                            const b = Array.from(document.querySelectorAll('button'))
                                .find(x => x.querySelector('i.fa-plus') && x.textContent.trim() === '');
                            if (b) { b.scrollIntoView(); b.click(); }
                        """)
                        time.sleep(1.0)
                    except:
                        pass

                reportar(task_id, "Forma de pago configurada", page)

            if req.observaciones:
                try:
                    ob = page.locator("textarea[formcontrolname='observacionesDoc']").first
                    ob.wait_for(state="visible", timeout=4000)
                    ob.fill(req.observaciones)
                except:
                    pass

            # ── 7. GENERAR DOCUMENTO ──────────────────────────────
            reportar(task_id, "Preparando Generar Documento...", page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.8)
            reportar(task_id, "Vista previa antes de generar", page)

            generar_ok = False
            for sel_gen in [
                "input[type='submit'][value='Generar Documento']",
                "input[type='submit']",
                "button:has-text('Generar Documento')",
            ]:
                try:
                    el = page.locator(sel_gen).last
                    el.wait_for(state="visible", timeout=8000)
                    el.scroll_into_view_if_needed()
                    el.click(force=True)
                    generar_ok = True
                    break
                except:
                    pass

            if not generar_ok:
                try:
                    page.evaluate(
                        "const i=document.querySelector(\"input[type='submit']\");"
                        "if(i){i.scrollIntoView();i.click();}"
                    )
                    generar_ok = True
                except:
                    pass

            if not generar_ok:
                raise Exception("No se encontro el boton 'Generar Documento'")

            time.sleep(1.0)
            reportar(task_id, "Generar Documento enviado", page)

            # ── 7.2 CONFIRMAR ─────────────────────────────────────
            try:
                btn_si = page.locator(
                    "button.swal2-confirm:has-text('Si, crear documento'), "
                    "button.swal2-confirm:has-text('Si'), "
                    ".swal2-confirm"
                )
                btn_si.wait_for(state="visible", timeout=12000)
                reportar(task_id, "Confirmando DTE...", page)
                btn_si.first.click(force=True)
                time.sleep(1.0)
            except Exception as e_conf:
                try:
                    errs = page.locator(
                        "div[style*='background-color: #f8d7da'], "
                        "div.alert-danger, .swal2-html-container"
                    ).all_inner_texts()
                    msgs = [e.replace('x', '').strip() for e in errs if e.strip()]
                    if msgs:
                        reportar(task_id, f"Error validacion: {' | '.join(msgs)}", page)
                        raise Exception("Validacion Hacienda: " + " | ".join(msgs))
                except Exception as ve:
                    if "Validacion" in str(ve):
                        raise ve
                reportar(task_id, f"Confirmar popup: {e_conf}", page)

            # ── 7.3 CLAVE PRIVADA ─────────────────────────────────
            reportar(task_id, "Ingresando clave privada...", page)
            try:
                ic = page.get_by_placeholder("Ingrese la clave privada de validacion")
                ic.wait_for(state="visible", timeout=12000)
                ic.fill(hw_clave)
                bok = page.locator("button:has-text('OK')").last
                bok.wait_for(state="visible", timeout=5000)
                bok.click(force=True)
                reportar(task_id, "Clave enviada - esperando sello...", page)
            except Exception as e_clave:
                reportar(task_id, f"Clave privada: {e_clave}", page)

            # ── 8. CAPTURAR UUID ──────────────────────────────────
            codigo_generacion = ""
            pdf_base64 = ""

            try:
                page.wait_for_selector(".swal2-popup, .swal2-content", timeout=25000)
                time.sleep(2.0)
                body_txt = page.inner_text("body")
                if "incorrecta" in body_txt.lower() or "invalida" in body_txt.lower():
                    raise Exception("Clave privada incorrecta.")
                codigo_generacion = _extract_uuid(body_txt)
                reportar(task_id,
                    f"{'UUID: ' + codigo_generacion if codigo_generacion else 'UUID no encontrado en modal'}",
                    page)
            except Exception as e_uuid:
                if "Clave" in str(e_uuid):
                    raise e_uuid

            if not codigo_generacion:
                t0 = time.time()
                pdf_tab = None
                while time.time() - t0 < 15:
                    for pg in context.pages:
                        if "data:application/pdf" in pg.url or "blob:" in pg.url:
                            pdf_tab = pg
                            break
                    if pdf_tab:
                        break
                    time.sleep(0.8)

                if pdf_tab:
                    codigo_generacion = _extract_uuid(pdf_tab.url)
                    if not codigo_generacion:
                        try:
                            codigo_generacion = _extract_uuid(pdf_tab.title())
                        except:
                            pass
                    try:
                        pdf_bytes_js = pdf_tab.evaluate("""async () => {
                            const r = await fetch(document.URL);
                            const buf = await r.arrayBuffer();
                            return Array.from(new Uint8Array(buf));
                        }""")
                        pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode("utf-8")
                    except:
                        pass

            for btn_txt in ["OK", "Aceptar", "Cerrar"]:
                try:
                    page.locator(f"button:has-text('{btn_txt}')").last.click(
                        force=True, timeout=1500
                    )
                    time.sleep(0.3)
                except:
                    pass

            if not codigo_generacion:
                raise Exception("El portal de Hacienda no devolvio el UUID. Ver monitor.")

            # ── 9. CONSULTAS + PDF ────────────────────────────────
            reportar(task_id, f"Consultando UUID: {codigo_generacion}", page)
            try:
                from urllib.parse import urlparse
                parsed   = urlparse(page.url)
                base_url = f"{parsed.scheme}://{parsed.netloc}"

                page.goto(f"{base_url}/consultaDteEmitidos", wait_until="domcontentloaded")
                time.sleep(1.0)

                inp_cod = page.locator(
                    "input[formcontrolname='codigoGeneracion'], "
                    "input[placeholder*='0000'], input[placeholder*='AAAA']"
                ).first
                inp_cod.wait_for(state="visible", timeout=10000)
                inp_cod.fill(codigo_generacion)

                page.locator("button:has-text('Consultar')").first.wait_for(
                    state="visible", timeout=5000
                )
                page.locator("button:has-text('Consultar')").first.click()
                page.wait_for_selector("tbody tr", timeout=15000)
                time.sleep(1.5)

                if not pdf_base64:
                    pages_antes = set(id(pg) for pg in context.pages)
                    page.locator(
                        "button[tooltip='Version legible'], button:has(i.fa-print)"
                    ).first.click(force=True)
                    time.sleep(3)

                    pdf_tab2 = None
                    for pg in context.pages:
                        if id(pg) not in pages_antes:
                            pdf_tab2 = pg
                            break
                    if not pdf_tab2:
                        for pg in context.pages:
                            if "data:application/pdf" in pg.url or "blob:" in pg.url:
                                pdf_tab2 = pg
                                break

                    if pdf_tab2:
                        pdf_url = pdf_tab2.url
                        if pdf_url.startswith("data:application/pdf;base64,"):
                            pdf_base64 = pdf_url.split(",", 1)[1]
                        elif "blob:" in pdf_url:
                            try:
                                pdf_bytes_js2 = pdf_tab2.evaluate("""async () => {
                                    const r = await fetch(document.URL);
                                    const buf = await r.arrayBuffer();
                                    return Array.from(new Uint8Array(buf));
                                }""")
                                pdf_base64 = base64.b64encode(
                                    bytes(pdf_bytes_js2)
                                ).decode("utf-8")
                            except:
                                pass
                        try:
                            pdf_tab2.close()
                        except:
                            pass

            except Exception as e_consulta:
                reportar(task_id, f"Consultas: {e_consulta}", page)

            browser.close()

            TAREAS[task_id] = {
                "status":            "completado",
                "exito":             True,
                "sello_recepcion":   "IMPRESO-EN-EL-PDF",
                "codigo_generacion": codigo_generacion,
                "pdf_base64":        pdf_base64,
                "json_content":      "{}",
            }

    except Exception as e:
        try:
            reportar(task_id, f"ERROR CRITICO: {str(e)}", page)
        except:
            pass
        TAREAS[task_id] = {
            "status":     "error",
            "exito":      False,
            "detail":     str(e),
            "screenshot": TAREAS.get(task_id, {}).get("screenshot", ""),
        }


@app.post("/facturar")
def facturar_inmediato(req: FacturaRequest, bg_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    bg_tasks.add_task(procesar_dte_en_fondo, task_id, req)
    return {"exito": True, "task_id": task_id, "status": "procesando"}


@app.get("/status/{task_id}")
def verificar_status(task_id: str):
    return TAREAS.get(task_id, {"status": "no_encontrado"})
