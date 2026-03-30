from fastapi import FastAPI
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time
import re
import json
import base64

app = FastAPI()

@app.get("/")
@app.get("/ping")
def ping():
    return {"status": "ok", "mensaje": "El robot está despierto y listo."}

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
    "1 - Bien": "1 - Bien",
    "2 - Servicio": "2 - Servicio",
    "3 - Bien y servicio": "3 - Bien y servicio",
}

def fill_field(page, selector, value, timeout=5000):
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.fill(str(value))
        return True
    except Exception:
        return False

def select_opt(page, selector, label=None, value=None, index=None, timeout=5000):
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        if label: el.select_option(label=label)
        elif value: el.select_option(value=value)
        elif index is not None: el.select_option(index=index)
        return True
    except Exception:
        return False

@app.post("/facturar")
def automatizar_hacienda(req: FacturaRequest):
    hw_user  = req.nit_empresa
    hw_pass  = req.clave_hacienda
    hw_clave = req.clave_firma if req.clave_firma else req.clave_hacienda

    nit_cli  = req.receptor.numDocumento
    nom_cli  = req.receptor.nombre
    nrc_cli  = req.receptor.nrc
    act_cli  = req.receptor.codActividad
    tipo_doc = "NIT"
    depto    = req.receptor.departamento
    muni     = req.receptor.municipio
    dir_cli  = req.receptor.direccion
    mail_cli = req.receptor.correo
    tel_cli  = req.receptor.telefono
    
    tipo_dte = req.tipo_dte
    es_nota = tipo_dte in ("Nota de Crédito", "Nota de Débito")
    forma_pago = req.formas_pago[0] if req.formas_pago else "01"
    
    monto_pago = sum([i.cantidad * i.precio for i in req.items])
    if tipo_dte == "Comprobante de Crédito Fiscal":
        monto_pago = monto_pago * 1.13

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--disable-blink-features=AutomationControlled'])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                accept_downloads=True
            )
            page = context.new_page()

            # 1. Login
            page.goto("https://factura.gob.sv/", wait_until="domcontentloaded")
            with context.expect_page() as npi:
                page.click("text=Ingresar")
            page = npi.value
            page.wait_for_load_state("domcontentloaded")
            try:
                page.locator("//h5[contains(text(),'Emisores DTE')]/..//button").click()
                page.wait_for_load_state("domcontentloaded")
            except: pass
            
            page.get_by_placeholder("NIT/DUI").fill(hw_user.replace("-", ""))
            page.locator("input[type='password']").fill(hw_pass)
            
            try:
                sel_ambiente = page.locator("select[formcontrolname='ambiente']").first
                sel_ambiente.wait_for(state="visible", timeout=5000)
                sel_ambiente.select_option(value="/test")
                time.sleep(0.5) 
            except: pass

            page.click("button:has-text('Iniciar sesión')")
            try:
                page.wait_for_selector(".swal2-confirm", timeout=4000)
                page.click(".swal2-confirm")
            except: pass

            # 2. Sistema de Facturación
            try:
                page.locator("text=Sistema de facturación").wait_for(state="visible", timeout=12000)
                page.click("text=Sistema de facturación", force=True)
                page.wait_for_load_state("domcontentloaded")
            except: pass

            # 3. Tipo de documento
            try:
                page.wait_for_selector(".swal2-popup select, select.swal2-select", timeout=12000)
                page.locator(".swal2-popup select, select.swal2-select").first.select_option(label=tipo_dte)
                page.locator("button.swal2-confirm, button:has-text('OK')").first.click()
                page.wait_for_load_state("domcontentloaded")
            except: pass

            # 4. Receptor
            is_ccf_or_nota = ("Crédito Fiscal" in tipo_dte) or es_nota
            if not is_ccf_or_nota:
                try:
                    td = page.locator("select[formcontrolname='tipoDocumento']").first
                    td.wait_for(state="visible", timeout=5000)
                    td.select_option(label=tipo_doc)
                    time.sleep(0.5) 
                except: pass

            nit_clean = nit_cli.replace("-", "")
            try:
                ni = page.locator("input[placeholder^='Digite el número de']").first
                ni.wait_for(state="visible", timeout=10000)
                ni.fill(nit_clean)
            except:
                try: fill_field(page, "input[formcontrolname='numDocumento']", nit_clean)
                except: fill_field(page, "input[formcontrolname='nit']", nit_clean)

            if is_ccf_or_nota:
                if nrc_cli: fill_field(page, "input[formcontrolname='nrc']", nrc_cli.replace("-", ""))
                if act_cli:
                    try:
                        cod_act = act_cli.split(" - ")[0].strip()
                        a = page.locator("ng-select[formcontrolname='actividadEconomica']").first
                        a.wait_for(state="visible", timeout=5000)
                        a.click()
                        a.locator("input").fill(cod_act)
                        page.wait_for_selector(f"div.ng-option:has-text('{cod_act}')", timeout=5000)
                        page.locator(f"div.ng-option:has-text('{cod_act}')").first.click()
                    except: pass

            fill_field(page, "input[formcontrolname='nombre']", nom_cli)

            depto_code = depto.split(" - ")[0].strip() if " - " in depto else depto
            try:
                d = page.locator("select[formcontrolname='departamento']").first
                d.wait_for(state="visible", timeout=5000)
                try: d.select_option(value=depto_code)
                except: d.select_option(label=depto)
            except: pass

            muni_label = muni.split(" - ")[1].strip() if " - " in muni else muni
            try:
                m = page.locator("select[formcontrolname='municipio']").first
                m.wait_for(state="visible", timeout=5000)
                try: m.select_option(label=muni_label)
                except: m.select_option(index=1)
            except: pass

            fill_field(page, "textarea[formcontrolname='complemento']", dir_cli)
            try:
                e = page.locator("input[formcontrolname='correo'], input[formcontrolname='correoReceptor']").first
                e.wait_for(state="visible", timeout=5000)
                e.fill(mail_cli)
            except: pass

            # 5. Ítems
            for i, item in enumerate(req.items):
                if i == 0:
                    try:
                        btn_add_item = page.locator("button:has-text('Agregar Detalle'), #btnGroupDrop2, button:has-text('Agregar Ítem'), button:has-text('Agregar ítem')").first
                        btn_add_item.wait_for(state="visible", timeout=10000)
                        btn_add_item.click()
                        time.sleep(0.5)
                        opt_prod_serv = page.locator("a.dropdown-item:has-text('Producto o Servicio'), a:has-text('Producto o Servicio')").first
                        opt_prod_serv.wait_for(state="visible", timeout=5000)
                        opt_prod_serv.click()
                        page.wait_for_selector("div.modal-dialog, h5:has-text('Ítem DTE'), div:has-text('Adición detalle')", timeout=8000)
                        time.sleep(0.5)
                    except: pass

                tipo_item_label = TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien")
                select_opt(page, "xpath=//label[contains(text(),'Tipo:')]/following-sibling::*//select", label=tipo_item_label)

                try:
                    ci = page.locator("input[formcontrolname='cantidad']").first
                    ci.wait_for(state="visible", timeout=5000)
                    ci.click()
                    ci.clear()
                    ci.fill(str(item.cantidad))
                    ci.press("Tab")
                except: pass

                select_opt(page, "xpath=//label[contains(text(),'Unidad')]/following-sibling::*//select", label="Unidad")

                try:
                    ni2 = page.locator("input[placeholder='Nombre Producto'], input[formcontrolname='descripcion']").first
                    ni2.wait_for(state="visible", timeout=4000)
                    ni2.fill(item.descripcion)
                except:
                    try: fill_field(page, "input[formcontrolname='descripcion']", item.descripcion)
                    except: pass

                select_opt(page, "xpath=//label[contains(text(),'Tipo Venta')]/following-sibling::*//select", label=item.tipo_venta)

                try:
                    pi = page.locator("input[formcontrolname='precioUnitario']").first
                    pi.wait_for(state="visible", timeout=5000)
                    pi.click()
                    pi.clear()
                    pi.fill(f"{item.precio:.2f}")
                    pi.press("Tab")
                    time.sleep(0.3)
                except: pass

                try:
                    page.locator("button.btn-primary:has-text('Agregar ítem'), button.btn-primary:has-text('Agregar Ítem')").last.wait_for(state="visible", timeout=5000)
                    page.locator("button.btn-primary:has-text('Agregar ítem'), button.btn-primary:has-text('Agregar Ítem')").last.click()
                except: pass

                try:
                    if i < len(req.items) - 1:
                        page.locator("button:has-text('Seguir adicionando')").wait_for(state="visible", timeout=5000)
                        page.locator("button:has-text('Seguir adicionando')").click()
                    else:
                        page.locator("button:has-text('Regresar al documento')").wait_for(state="visible", timeout=5000)
                        page.locator("button:has-text('Regresar al documento')").click()
                        page.wait_for_load_state("domcontentloaded")
                except: pass

            # 6. Forma de pago
            if not es_nota:
                fp_map = {"01": "0: 01", "02": "3: 04", "03": "4: 05", "07": "1: 02", "99": "11: 99"}
                fp_val = fp_map.get(forma_pago, "0: 01")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.8)

                try:
                    fp = page.locator("select[formcontrolname='codigo']").first
                    fp.wait_for(state="visible", timeout=8000)
                    fp.scroll_into_view_if_needed()
                    fp.select_option(value=fp_val)
                except: pass

                try:
                    mp = page.locator("input[formcontrolname='montoPago']").first
                    mp.wait_for(state="visible", timeout=5000)
                    mp.click()
                    mp.clear()
                    mp.fill(f"{monto_pago:.2f}")
                    mp.press("Tab")
                    time.sleep(0.3)
                except: pass

                try:
                    bp = page.locator("button.btn-block:has(i.fa-plus), button[tooltip='Agregar forma de pago'], button.btn-primary:has(i.fa-plus)").first
                    bp.wait_for(state="visible", timeout=6000)
                    bp.scroll_into_view_if_needed()
                    bp.click(force=True)
                    time.sleep(1)
                except:
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
                        time.sleep(1)
                    except: pass
            else:
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.5)
                    cond_sel = page.locator("select[formcontrolname='condicionOperacion'], xpath=//label[contains(text(),'Condición')]/following-sibling::*//select").first
                    cond_sel.wait_for(state="visible", timeout=6000)
                    cond_sel.select_option(label="Contado")
                except: pass

            # 7. Generar Documento
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)

            generar_ok = False
            for sel_str in ["input[type='submit'][value='Generar Documento']", "input[type='submit']", "button:has-text('Generar Documento')"]:
                try:
                    el = page.locator(sel_str).last
                    el.wait_for(state="visible", timeout=8000)
                    el.scroll_into_view_if_needed()
                    el.click(force=True)
                    generar_ok = True
                    break
                except: pass

            if not generar_ok:
                try:
                    page.evaluate("const i=document.querySelector(\"input[type='submit']\"); if(i){i.scrollIntoView();i.click();}")
                except: pass

            # 7.2 Confirmar
            try:
                bs = page.locator("button.swal2-confirm:has-text('Si, crear documento'), .swal2-confirm")
                bs.wait_for(state="visible", timeout=12000)
                bs.first.click(force=True)
            except: pass

            # 7.3 Clave privada
            try:
                ic = page.get_by_placeholder("Ingrese la clave privada de validación")
                ic.wait_for(state="visible", timeout=12000)
                ic.fill(hw_clave)
                bok = page.locator("button:has-text('OK')").last
                bok.wait_for(state="visible", timeout=5000)
                bok.click(force=True)
            except: pass

            # =================================================================
            # 8. EXTRACCIÓN REAL DEL UUID (RESTAURADO DE TU STREAMLIT)
            # =================================================================
            codigo_generacion = ""
            
            def _extract_uuid(text):
                m = re.search(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", text)
                return m.group(0).upper() if m else ""

            # Intento 1: Buscar en el modal de éxito
            try:
                time.sleep(3) # Dar tiempo a que el servidor de MH responda
                page.wait_for_selector(".swal2-popup, .swal2-content", timeout=15000)
                body_text = page.inner_text("body")
                codigo_generacion = _extract_uuid(body_text)
            except: pass

            # Intento 2: Buscar en las pestañas nuevas (PDF blob) EXACTAMENTE como en Streamlit
            if not codigo_generacion:
                try:
                    t0 = time.time()
                    pdf_page = None
                    while time.time() - t0 < 15:
                        for pg in context.pages:
                            if "data:application/pdf" in pg.url or "blob:" in pg.url:
                                pdf_page = pg
                                break
                        if pdf_page:
                            break
                        time.sleep(0.5)
                    
                    if pdf_page:
                        codigo_generacion = _extract_uuid(pdf_page.url)
                        if not codigo_generacion:
                            codigo_generacion = _extract_uuid(pdf_page.title())
                except: pass

            if not codigo_generacion:
                raise Exception("No se pudo capturar el Código de Generación (UUID) tras firmar. El portal no lo mostró a tiempo.")

            # Cerrar modales
            for btn_txt in ["OK", "Aceptar", "Cerrar"]:
                try: page.locator(f"button:has-text('{btn_txt}')").last.click(force=True, timeout=2000)
                except: pass

            # 9. Ir a Consultas para descargar los archivos reales
            page.goto("https://factura.gob.sv/consultaDteEmitidos", wait_until="domcontentloaded")
            time.sleep(1)

            input_cod = page.locator("input[formcontrolname='codigoGeneracion'], input[placeholder*='0000']").first
            input_cod.wait_for(state="visible", timeout=10000)
            input_cod.fill(codigo_generacion)
            
            page.locator("button:has-text('Consultar')").first.click()
            page.wait_for_selector("tbody tr", timeout=15000)
            time.sleep(1.5)

            # 9a. Descargar JSON y extraer Sello
            json_content = ""
            sello_recepcion = "SELLO-NO-ENCONTRADO"
            try:
                with context.expect_download(timeout=20000) as dl_info:
                    page.locator("button[tooltip='Descargar documento'], button:has(i.fa-arrow-down)").first.click(force=True)
                dl = dl_info.value
                json_path = f"/tmp/{codigo_generacion}.json"
                dl.save_as(json_path)
                with open(json_path, "r", encoding="utf-8") as f:
                    json_content = f.read()
                
                j_data = json.loads(json_content)
                sello_recepcion = j_data.get("selloRecibido", sello_recepcion)
            except Exception as e:
                raise Exception(f"Error descargando JSON: {str(e)}")

            # 9b. Descargar PDF en Base64
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
                    pdf_bytes_js = pdf_tab.evaluate("""async () => {
                        const r = await fetch(document.URL);
                        const buf = await r.arrayBuffer();
                        return Array.from(new Uint8Array(buf));
                    }""")
                    pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode('utf-8')
                    pdf_tab.close()
            except Exception as e:
                raise Exception(f"Error descargando PDF: {str(e)}")

            browser.close()

            return {
                "exito": True,
                "sello_recepcion": sello_recepcion,
                "codigo_generacion": codigo_generacion,
                "pdf_base64": pdf_base64,
                "json_content": json_content
            }

    except Exception as e:
        titulo = "Desconocido"
        try: titulo = page.title()
        except: pass
        return {
            "exito": False, 
            "detail": f"Error: {str(e)} | Se atascó en la página: '{titulo}'"
        }
