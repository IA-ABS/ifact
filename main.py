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

def reportar_paso(task_id, mensaje, page=None):
    b64_img = ""
    if page:
        try:
            img_bytes = page.screenshot(type="jpeg", quality=40)
            b64_img = base64.b64encode(img_bytes).decode("utf-8")
        except:
            pass
    TAREAS[task_id]["mensaje"] = mensaje
    if b64_img:
        TAREAS[task_id]["screenshot"] = b64_img

# ── Helpers portados desde Streamlit (versión probada) ──────────

def fill_field(page, selector, value, timeout=5000):
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.fill(str(value))
        el.press("Tab")
        return True
    except:
        return False

def select_opt(page, selector, label=None, value=None, timeout=5000):
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        if label:
            el.select_option(label=label)
        elif value:
            el.select_option(value=value)
        return True
    except:
        return False

def _extract_uuid(text):
    m = re.search(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        text
    )
    return m.group(0).upper() if m else ""


def procesar_dte_en_fondo(task_id: str, req: FacturaRequest):
    TAREAS[task_id] = {"status": "procesando", "mensaje": "Iniciando navegador...", "screenshot": ""}

    hw_user  = req.nit_empresa
    hw_pass  = req.clave_hacienda
    hw_clave = req.clave_firma if req.clave_firma else req.clave_hacienda
    tipo_dte = req.tipo_dte
    es_nota  = tipo_dte in ("Nota de Crédito", "Nota de Débito")
    forma_pago = req.formas_pago[0] if req.formas_pago else "01"

    monto_pago = sum(i.cantidad * i.precio for i in req.items)
    if "Crédito Fiscal" in tipo_dte:
        monto_pago = monto_pago * 1.13

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                ]
            )
            context = browser.new_context(
                viewport={"width": 1366, "height": 768},
                accept_downloads=True,
                locale="es-SV",
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            # Bloquear recursos innecesarios para acelerar
            page.route("**/*", lambda route: route.abort()
                if route.request.resource_type in ["media"]
                else route.continue_())

            # ── 1. LOGIN ─────────────────────────────────────────────
            reportar_paso(task_id, "Abriendo portal Hacienda...", page)
            page.goto("https://factura.gob.sv/", wait_until="domcontentloaded")

            with context.expect_page() as npi:
                page.click("text=Ingresar")
            page = npi.value
            page.wait_for_load_state("domcontentloaded")

            # Popup de selección de rol
            try:
                page.locator("//h5[contains(text(),'Emisores DTE')]/..//button").click()
                page.wait_for_load_state("domcontentloaded")
            except:
                pass

            # Credenciales
            page.get_by_placeholder("NIT/DUI").fill(hw_user.replace("-", ""))
            page.locator("input[type='password']").fill(hw_pass)

            # Seleccionar ambiente pruebas
            try:
                sel_amb = page.locator("select[formcontrolname='ambiente']").first
                sel_amb.wait_for(state="visible", timeout=5000)
                sel_amb.select_option(value="/test")
                time.sleep(0.5)
            except:
                pass

            page.click("button:has-text('Iniciar sesión')")
            try:
                page.wait_for_selector(".swal2-confirm", timeout=4000)
                page.click(".swal2-confirm")
            except:
                pass

            reportar_paso(task_id, "✅ Login OK — abriendo facturador...", page)

            # ── 2. SISTEMA DE FACTURACIÓN ────────────────────────────
            try:
                page.locator("text=Sistema de facturación").wait_for(state="visible", timeout=15000)
                page.click("text=Sistema de facturación", force=True)
                page.wait_for_load_state("domcontentloaded")
            except:
                pass

            # ── 3. TIPO DE DOCUMENTO ─────────────────────────────────
            reportar_paso(task_id, f"Seleccionando tipo DTE: {tipo_dte}...", page)
            try:
                page.wait_for_selector(".swal2-popup select, select.swal2-select", timeout=12000)
                page.locator(".swal2-popup select, select.swal2-select").first.select_option(label=tipo_dte)
                page.locator("button.swal2-confirm, button:has-text('OK')").first.click()
                page.wait_for_load_state("domcontentloaded")
            except:
                pass

            # ── 4. RECEPTOR ──────────────────────────────────────────
            reportar_paso(task_id, "Llenando datos del receptor...", page)

            is_ccf_or_nota = ("Crédito Fiscal" in tipo_dte) or es_nota
            nit_clean = req.receptor.numDocumento.replace("-", "")

            if not is_ccf_or_nota:
                # Factura: seleccionar tipo de documento primero
                try:
                    td = page.locator("select[formcontrolname='tipoDocumento']").first
                    td.wait_for(state="visible", timeout=5000)
                    td.select_option(label="NIT")
                    time.sleep(0.5)
                except:
                    pass

            # Número de documento
            try:
                ni = page.locator("input[placeholder^='Digite el número de']").first
                ni.wait_for(state="visible", timeout=10000)
                ni.fill(nit_clean)
            except:
                fill_field(page, "input[formcontrolname='numDocumento']", nit_clean)

            if is_ccf_or_nota:
                if req.receptor.nrc:
                    fill_field(page, "input[formcontrolname='nrc']",
                               req.receptor.nrc.replace("-", ""))
                if req.receptor.codActividad:
                    try:
                        cod = req.receptor.codActividad.split(" - ")[0].strip()
                        a = page.locator("ng-select[formcontrolname='actividadEconomica']").first
                        a.wait_for(state="visible", timeout=5000)
                        a.click(force=True)
                        time.sleep(0.4)
                        a.locator("input[type='text']").first.fill(cod)
                        time.sleep(1)
                        page.locator(f"div.ng-option:has-text('{cod}')").first.click(force=True)
                        time.sleep(0.5)
                    except:
                        pass

            # Nombre receptor
            fill_field(page, "input[formcontrolname='nombre']", req.receptor.nombre)

            # Departamento
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

            # Municipio
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

            # Dirección / complemento
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

            # Correo y teléfono
            if req.receptor.correo:
                try:
                    e_inp = page.locator(
                        "input[formcontrolname='correo'], input[formcontrolname='correoReceptor']"
                    ).first
                    e_inp.wait_for(state="visible", timeout=5000)
                    e_inp.fill(req.receptor.correo)
                except:
                    pass
            if req.receptor.telefono:
                try:
                    t_inp = page.locator(
                        "input[formcontrolname='telefono'], input[formcontrolname='telefonoReceptor']"
                    ).first
                    t_inp.wait_for(state="visible", timeout=5000)
                    t_inp.fill(req.receptor.telefono)
                except:
                    pass

            # ── 5. ÍTEMS ─────────────────────────────────────────────
            reportar_paso(task_id, "Agregando ítems...", page)

            for i, item in enumerate(req.items):
                if i == 0:
                    # Abrir modal de ítem — igual que en Streamlit
                    try:
                        btn_add = page.locator(
                            "button:has-text('Agregar Detalle'), "
                            "#btnGroupDrop2, "
                            "button:has-text('Agregar Ítem'), "
                            "button:has-text('Agregar ítem')"
                        ).first
                        btn_add.wait_for(state="visible", timeout=10000)
                        btn_add.click()
                        time.sleep(0.5)

                        opt_ps = page.locator(
                            "a.dropdown-item:has-text('Producto o Servicio'), "
                            "a:has-text('Producto o Servicio')"
                        ).first
                        opt_ps.wait_for(state="visible", timeout=5000)
                        opt_ps.click()

                        page.wait_for_selector(
                            "div.modal-dialog, h5:has-text('Ítem DTE'), div:has-text('Adición detalle')",
                            timeout=8000
                        )
                        time.sleep(0.6)
                    except Exception as e_modal:
                        reportar_paso(task_id, f"⚠️ Abrir modal ítem: {e_modal}", page)

                # Tipo de ítem
                tipo_item_label = TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien")
                select_opt(
                    page,
                    "xpath=//label[contains(text(),'Tipo:')]/following-sibling::*//select",
                    label=tipo_item_label
                )

                # Cantidad — click, clear, fill, TAB (patrón Streamlit)
                try:
                    ci = page.locator("input[formcontrolname='cantidad']").first
                    ci.wait_for(state="visible", timeout=5000)
                    ci.click()
                    ci.clear()
                    ci.fill(str(item.cantidad))
                    ci.press("Tab")
                except:
                    try:
                        ci_fb = page.locator("input[placeholder='Cantidad']").first
                        ci_fb.click(); ci_fb.clear()
                        ci_fb.fill(str(item.cantidad)); ci_fb.press("Tab")
                    except:
                        pass

                # Unidad
                select_opt(
                    page,
                    "xpath=//label[contains(text(),'Unidad')]/following-sibling::*//select",
                    label="Unidad"
                )

                # Descripción
                try:
                    desc_inp = page.locator(
                        "input[placeholder='Nombre Producto'], input[formcontrolname='descripcion']"
                    ).first
                    desc_inp.wait_for(state="visible", timeout=4000)
                    desc_inp.fill(item.descripcion)
                except:
                    fill_field(page, "input[formcontrolname='descripcion']", item.descripcion)

                # Tipo de venta
                select_opt(
                    page,
                    "xpath=//label[contains(text(),'Tipo Venta')]/following-sibling::*//select",
                    label=item.tipo_venta
                )

                # Precio — click, clear, fill 2 decimales, TAB (patrón Streamlit)
                try:
                    pi = page.locator("input[formcontrolname='precioUnitario']").first
                    pi.wait_for(state="visible", timeout=5000)
                    pi.click()
                    pi.clear()
                    pi.fill(f"{item.precio:.2f}")
                    pi.press("Tab")
                    time.sleep(0.5)
                except:
                    try:
                        pi_fb = page.locator(
                            "xpath=//label[contains(text(),'Precio')]/following-sibling::input"
                        ).first
                        pi_fb.click(); pi_fb.clear()
                        pi_fb.fill(f"{item.precio:.2f}"); pi_fb.press("Tab")
                        time.sleep(0.5)
                    except:
                        pass

                # Botón "Agregar ítem" (azul dentro del modal)
                try:
                    page.locator(
                        "button.btn-primary:has-text('Agregar ítem'), "
                        "button.btn-primary:has-text('Agregar Ítem')"
                    ).last.wait_for(state="visible", timeout=5000)
                    page.locator(
                        "button.btn-primary:has-text('Agregar ítem'), "
                        "button.btn-primary:has-text('Agregar Ítem')"
                    ).last.click()
                    time.sleep(1.2)
                except:
                    pass

                # Navegar entre ítems
                try:
                    if i < len(req.items) - 1:
                        page.locator("button:has-text('Seguir adicionando')").wait_for(
                            state="visible", timeout=5000
                        )
                        page.locator("button:has-text('Seguir adicionando')").click()
                        time.sleep(0.8)
                    else:
                        page.locator("button:has-text('Regresar al documento')").wait_for(
                            state="visible", timeout=5000
                        )
                        page.locator("button:has-text('Regresar al documento')").click()
                        page.wait_for_load_state("domcontentloaded")
                        time.sleep(0.8)
                except:
                    pass

            # ── 6. FORMA DE PAGO ─────────────────────────────────────
            if not es_nota:
                reportar_paso(task_id, "Configurando forma de pago...", page)

                # Map código → valor Angular
                fp_map = {"01": "0: 01", "02": "3: 04", "03": "4: 05", "07": "1: 02", "99": "11: 99"}
                fp_val = fp_map.get(forma_pago, "0: 01")

                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.0)

                # PASO 1: Borrar filas auto-agregadas por Hacienda
                try:
                    btns_x = page.locator(
                        "table tbody tr button.btn-outline-primary:has(i.fa-times), "
                        "table tbody tr button:has(i.fa-times)"
                    ).all()
                    for btn_x in btns_x:
                        try:
                            btn_x.click(force=True)
                            time.sleep(0.4)
                        except:
                            pass
                except:
                    pass

                # PASO 2: Seleccionar forma de pago
                try:
                    fp_sel = page.locator("select[formcontrolname='codigo']").first
                    fp_sel.wait_for(state="visible", timeout=8000)
                    fp_sel.scroll_into_view_if_needed()
                    fp_sel.select_option(value=fp_val)
                except Exception as e_fp:
                    reportar_paso(task_id, f"⚠️ Forma pago select: {e_fp}", page)

                # PASO 3: Monto de pago — click, clear, fill, TAB (patrón Streamlit)
                try:
                    mp = page.locator("input[formcontrolname='montoPago']").first
                    mp.wait_for(state="visible", timeout=5000)
                    mp.click()
                    mp.clear()
                    mp.fill(f"{monto_pago:.2f}")
                    mp.press("Tab")
                    time.sleep(0.5)
                except Exception as e_mp:
                    reportar_paso(task_id, f"⚠️ Monto pago: {e_mp}", page)

                # PASO 4: Clic en botón "+" para agregar la forma de pago
                added = False
                for sel_plus in [
                    "button.btn-block:has(i.fa-plus)",
                    "button[tooltip='Agregar forma de pago']",
                    "button.btn-primary:has(i.fa-plus)",
                ]:
                    try:
                        bp = page.locator(sel_plus).first
                        bp.wait_for(state="visible", timeout=4000)
                        bp.scroll_into_view_if_needed()
                        bp.click(force=True)
                        added = True
                        time.sleep(1.0)
                        break
                    except:
                        pass

                if not added:
                    # Fallback JS — igual que Streamlit
                    try:
                        page.evaluate("""
                            const b = Array.from(document.querySelectorAll('button.btn-primary'))
                                       .find(x => x.querySelector('i.fa-plus'));
                            if (b) { b.scrollIntoView(); b.click(); }
                            else {
                                const bb = document.querySelector('button.btn-block');
                                if (bb) { bb.scrollIntoView(); bb.click(); }
                            }
                        """)
                        time.sleep(1.0)
                    except:
                        pass

                reportar_paso(task_id, "✅ Forma de pago configurada", page)

            # Observaciones
            if req.observaciones:
                try:
                    ob = page.locator("textarea[formcontrolname='observacionesDoc']").first
                    ob.wait_for(state="visible", timeout=4000)
                    ob.fill(req.observaciones)
                except:
                    pass

            # ── 7. GENERAR DOCUMENTO ─────────────────────────────────
            reportar_paso(task_id, "Haciendo clic en 'Generar Documento'...", page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.8)

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
                raise Exception("No se encontró el botón 'Generar Documento'")

            time.sleep(1.0)

            # ── 7.2 CONFIRMAR ────────────────────────────────────────
            reportar_paso(task_id, "Confirmando creación del DTE...", page)
            try:
                btn_si = page.locator(
                    "button.swal2-confirm:has-text('Si, crear documento'), "
                    "button.swal2-confirm:has-text('Sí, crear documento'), "
                    ".swal2-confirm"
                )
                btn_si.wait_for(state="visible", timeout=12000)
                btn_si.first.click(force=True)
                time.sleep(1.0)
            except Exception as e_conf:
                # Puede haber errores de validación en pantalla
                try:
                    errs = page.locator(
                        "div[style*='background-color: #f8d7da'], div.alert-danger"
                    ).all_inner_texts()
                    if errs:
                        raise Exception("Datos incompletos: " + " | ".join(
                            [e.replace('×', '').strip() for e in errs]
                        ))
                except Exception as ve:
                    if "Datos incompletos" in str(ve):
                        raise ve
                reportar_paso(task_id, f"⚠️ Confirmar: {e_conf}", page)

            # ── 7.3 CLAVE PRIVADA ────────────────────────────────────
            reportar_paso(task_id, "Ingresando clave privada...", page)
            try:
                ic = page.get_by_placeholder("Ingrese la clave privada de validación")
                ic.wait_for(state="visible", timeout=12000)
                ic.fill(hw_clave)
                bok = page.locator("button:has-text('OK')").last
                bok.wait_for(state="visible", timeout=5000)
                bok.click(force=True)
                reportar_paso(task_id, "✅ Clave enviada — esperando sello oficial...", page)
            except Exception as e_clave:
                reportar_paso(task_id, f"⚠️ Clave privada: {e_clave}", page)

            # ── 8. CAPTURAR UUID ─────────────────────────────────────
            reportar_paso(task_id, "Capturando código de generación (UUID)...", page)
            codigo_generacion = ""
            pdf_base64 = ""

            # Intentar desde el popup SweetAlert2
            try:
                page.wait_for_selector(".swal2-popup, .swal2-content", timeout=20000)
                time.sleep(1.5)
                body_txt = page.inner_text("body")
                if "incorrecta" in body_txt.lower() or "inválida" in body_txt.lower():
                    raise Exception("Clave privada incorrecta.")
                codigo_generacion = _extract_uuid(body_txt)
                if codigo_generacion:
                    reportar_paso(task_id, f"✅ UUID capturado del modal: {codigo_generacion}", page)
            except Exception as e_uuid:
                if "Clave" in str(e_uuid):
                    raise e_uuid

            # Si no se obtuvo desde modal, buscar en pestaña PDF
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
                    # Intentar extraer PDF desde la pestaña blob
                    try:
                        pdf_bytes_js = pdf_tab.evaluate("""async () => {
                            const r = await fetch(document.URL);
                            const buf = await r.arrayBuffer();
                            return Array.from(new Uint8Array(buf));
                        }""")
                        pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode("utf-8")
                    except:
                        pass

            # Cerrar popups pendientes
            for btn_txt in ["OK", "Aceptar", "Cerrar"]:
                try:
                    page.locator(f"button:has-text('{btn_txt}')").last.click(force=True, timeout=1500)
                    time.sleep(0.3)
                except:
                    pass

            # ── 9. IR A CONSULTAS PARA DESCARGAR PDF + JSON ──────────
            if not codigo_generacion:
                raise Exception("El portal de Hacienda no devolvió el UUID. Ver monitor.")

            reportar_paso(task_id, f"📥 Yendo a Consultas: {codigo_generacion}", page)

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
                reportar_paso(task_id, "✅ DTE encontrado en consultas — descargando PDF...", page)

                # 9a. PDF versión legible
                if not pdf_base64:
                    try:
                        pages_antes = set(id(p) for p in context.pages)
                        page.locator(
                            "button[tooltip='Versión legible'], button:has(i.fa-print)"
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
                                    pdf_base64 = base64.b64encode(bytes(pdf_bytes_js2)).decode("utf-8")
                                except:
                                    pass
                            try:
                                pdf_tab2.close()
                            except:
                                pass
                    except Exception as ep:
                        reportar_paso(task_id, f"⚠️ PDF versión legible: {ep}", page)

            except Exception as e_consulta:
                reportar_paso(task_id, f"⚠️ Consultas: {e_consulta}", page)

            browser.close()

            TAREAS[task_id] = {
                "status":             "completado",
                "exito":              True,
                "sello_recepcion":    "IMPRESO-EN-EL-PDF",
                "codigo_generacion":  codigo_generacion,
                "pdf_base64":         pdf_base64,
                "json_content":       "{}",
            }

    except Exception as e:
        try:
            reportar_paso(task_id, f"❌ ERROR CRÍTICO: {str(e)}", page)
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
