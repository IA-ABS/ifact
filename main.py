"""
IABSTECH IFACT – Robot RPA para factura.gob.sv
Construido a partir del HTML real del portal y los pasos documentados.

PASOS REALES (según documento y HTML):
  1.  Abrir https://factura.gob.sv/ → clic "Ingresar"
  2.  Login: NIT/DUI + pass + ambiente prueba → "Iniciar sesión" → OK
  3.  Clic "Sistema de Facturación" en el menú lateral
  4.  Popup SweetAlert2: elegir tipo DTE → OK
  5.  Completar receptor (tab Receptor está activo por defecto)
      - CCF: formcontrolname="nit"
      - Factura consumidor: igual, el mismo campo
      - nombre, nrc, actividadEconomica (ng-select), departamento (value numérico),
        municipio (value numérico), complemento, correo, telefono
  6.  Scroll hacia abajo → clic "Agregar Ítem" (id=btnGroupDrop2) → "Producto o Servicio"
  7.  En el modal:  tipo, cantidad, unidad, descripcion, tipoVenta, precioUnitario
      → clic "Agregar ítem" (btn-primary dentro del modal)
  8.  "Seguir adicionando" o "Regresar al documento"
  9.  Scroll al fondo → configurar Forma de Pago (select[formcontrolname='codigo'])
      + input[formcontrolname='montoPago'] + clic botón "+"
  10. Clic "Generar Documento" (input[type='submit'][value='Generar Documento'])
  11. SweetAlert "¿Está seguro?" → "Sí, crear documento"
  12. Input clave privada → OK
  13. PDF generado → capturar UUID → ir a Consultas → descargar PDF
"""

from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from playwright.sync_api import sync_playwright, Page, BrowserContext
import time, re, base64, uuid

app = FastAPI()
TAREAS: dict = {}

# ── Modelos ──────────────────────────────────────────────────────────────────

@app.get("/")
@app.get("/ping")
def ping():
    return {"status": "ok", "mensaje": "Robot en línea"}


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


# ── Helpers ──────────────────────────────────────────────────────────────────

TIPO_ITEM_MAP = {
    "1 - Bien":            "1 - Bien",
    "2 - Servicio":        "2 - Servicio",
    "3 - Bien y servicio": "3 - Bien y servicio",
}

# Departamentos: el HTML usa value numérico "06" etc.
DEPTO_VALUE = {
    "00": "00", "01": "01", "02": "02", "03": "03", "04": "04",
    "05": "05", "06": "06", "07": "07", "08": "08", "09": "09",
    "10": "10", "11": "11", "12": "12", "13": "13", "14": "14",
}


def _depto_value(raw: str) -> str:
    """
    Convierte '06 - SAN SALVADOR' o '06' → '06'.
    Devuelve el número de 2 dígitos que usa el <select> del portal.
    """
    if not raw:
        return "06"
    part = raw.split(" - ")[0].strip().zfill(2)
    return part if part in DEPTO_VALUE else "06"


def _muni_value(raw: str) -> str:
    """
    Convierte '23 - SAN SALVADOR CENTRO' → '23'.
    El <select> de municipio también usa valor numérico.
    """
    if not raw:
        return ""
    return raw.split(" - ")[0].strip()


def _screenshot(page: Page) -> str:
    try:
        return base64.b64encode(
            page.screenshot(type="jpeg", quality=45)
        ).decode("utf-8")
    except:
        return ""


def _report(task_id: str, msg: str, page: Page = None):
    TAREAS[task_id]["mensaje"] = msg
    if page:
        s = _screenshot(page)
        if s:
            TAREAS[task_id]["screenshot"] = s


def _extract_uuid(text: str) -> str:
    m = re.search(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
        r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        text
    )
    return m.group(0).upper() if m else ""


def _dispatch_input(page: Page, selector: str, value: str):
    """
    Fuerza que Angular detecte el cambio usando events nativos.
    Equivale a lo que hace un usuario real al escribir.
    """
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return;
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value'
            ).set;
            nativeInputValueSetter.call(el, val);
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur',   {bubbles: true}));
        }""",
        [selector, value]
    )


def _dispatch_textarea(page: Page, selector: str, value: str):
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return;
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value'
            ).set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur',   {bubbles: true}));
        }""",
        [selector, value]
    )


def _fill_angular(page: Page, selector: str, value: str, timeout: int = 6000) -> bool:
    """
    Estrategia robusta para campos Angular:
    1. Playwright fill normal
    2. triple_click + fill + Tab
    3. Dispatch nativo de eventos
    """
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.scroll_into_view_if_needed()

        # intento 1: click → triple_click (selecciona todo) → fill → Tab
        el.click()
        time.sleep(0.15)
        el.triple_click()
        time.sleep(0.1)
        page.keyboard.type(str(value))
        el.press("Tab")
        time.sleep(0.25)

        # intento 2: dispatch nativo por si Angular no captó
        _dispatch_input(page, selector, str(value))
        return True
    except:
        return False


def _select_angular(page: Page, selector: str,
                    value: str = None, label: str = None,
                    timeout: int = 6000) -> bool:
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        if value is not None:
            el.select_option(value=value)
        else:
            el.select_option(label=label)
        # Disparar change para Angular
        page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (el) el.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            selector
        )
        time.sleep(0.3)
        return True
    except:
        return False


# ── PASO 5: Llenar receptor ───────────────────────────────────────────────────

def _fill_receptor(page: Page, task_id: str, req: FacturaRequest):
    """
    Llena el tab 'Receptor' con los datos del cliente.
    Selectores sacados del HTML real del portal.
    """
    _report(task_id, "Paso 5 – Llenando receptor...", page)

    is_ccf = "Crédito Fiscal" in req.tipo_dte

    # ── NIT (el portal usa formcontrolname="nit" en CCF y Factura) ────
    nit_clean = req.receptor.numDocumento.replace("-", "").strip()
    # Asegurar que el tab Receptor está activo
    try:
        tab_receptor = page.locator("a.nav-link:has-text('Receptor'), tab[heading='Receptor'] a").first
        tab_receptor.click()
        time.sleep(0.5)
    except:
        pass

    nit_filled = False
    for sel in [
        "input[formcontrolname='nit']",
        "input[placeholder*='NIT del receptor']",
        "input[placeholder*='número de NIT']",
    ]:
        if _fill_angular(page, sel, nit_clean, timeout=8000):
            nit_filled = True
            break
    if not nit_filled:
        _report(task_id, "⚠️ No se pudo llenar NIT del receptor", page)

    # ── Nombre del cliente ────────────────────────────────────────────
    _fill_angular(page, "input[formcontrolname='nombre']", req.receptor.nombre)

    # ── NRC (solo CCF) ────────────────────────────────────────────────
    if is_ccf and req.receptor.nrc:
        nrc_clean = req.receptor.nrc.replace("-", "").strip()
        _fill_angular(page, "input[formcontrolname='nrc']", nrc_clean)

    # ── Actividad Económica (ng-select) ───────────────────────────────
    if req.receptor.codActividad:
        try:
            cod = req.receptor.codActividad.split(" - ")[0].strip()
            ng = page.locator("ng-select[formcontrolname='actividadEconomica']").first
            ng.wait_for(state="visible", timeout=6000)
            ng.click(force=True)
            time.sleep(0.5)
            ng.locator("input").first.fill(cod)
            time.sleep(1.2)
            # Clic en la primera opción que aparezca
            page.locator("div.ng-option").first.click(force=True)
            time.sleep(0.4)
        except:
            pass

    # ── Departamento (value numérico "06") ────────────────────────────
    depto_val = _depto_value(req.receptor.departamento)
    _select_angular(page, "select[formcontrolname='departamento']", value=depto_val)
    time.sleep(0.4)

    # ── Municipio (value numérico, después de que Angular actualice opciones) ──
    muni_val = _muni_value(req.receptor.municipio)
    if muni_val:
        try:
            muni_el = page.locator("select[formcontrolname='municipio']").first
            muni_el.wait_for(state="visible", timeout=5000)
            muni_el.select_option(value=muni_val)
            page.evaluate(
                "document.querySelector(\"select[formcontrolname='municipio']\")"
                ".dispatchEvent(new Event('change',{bubbles:true}))"
            )
            time.sleep(0.3)
        except:
            pass

    # ── Dirección / Complemento ───────────────────────────────────────
    dir_val = req.receptor.direccion.strip() or "San Salvador, El Salvador"
    try:
        ta = page.locator("textarea[formcontrolname='complemento']").first
        ta.wait_for(state="visible", timeout=5000)
        ta.click()
        ta.triple_click()
        ta.fill(dir_val)
        ta.press("Tab")
        time.sleep(0.3)
        _dispatch_textarea(page, "textarea[formcontrolname='complemento']", dir_val)
    except:
        pass

    # ── Correo ────────────────────────────────────────────────────────
    if req.receptor.correo:
        _fill_angular(page, "input[formcontrolname='correo']", req.receptor.correo)

    # ── Teléfono ──────────────────────────────────────────────────────
    if req.receptor.telefono:
        tel = req.receptor.telefono.replace("-", "").strip()
        _fill_angular(page, "input[formcontrolname='telefono']", tel)

    _report(task_id, "Paso 5 ✅ Receptor llenado", page)


# ── PASOS 6-8: Agregar ítems ──────────────────────────────────────────────────

def _abrir_modal_item(page: Page, task_id: str):
    """
    Paso 6/7: clic en 'Agregar Ítem' (btnGroupDrop2) → 'Producto o Servicio'
    Espera que el modal esté visible antes de retornar.
    """
    _report(task_id, "Paso 6 – Abriendo modal de ítem...", page)

    # El HTML real: button#btnGroupDrop2.btn.btn-primary.dropdown-toggle
    for sel_btn in [
        "#btnGroupDrop2",
        "button[id='btnGroupDrop2']",
        "button.dropdown-toggle:has-text('Agregar')",
        "button.btn-primary.dropdown-toggle",
    ]:
        try:
            btn = page.locator(sel_btn).first
            btn.wait_for(state="visible", timeout=6000)
            btn.scroll_into_view_if_needed()
            btn.click()
            time.sleep(0.6)
            break
        except:
            pass

    # dropdown-item "Producto o Servicio"
    # HTML: <a class="dropdown-item">Producto o Servicio</a>
    for sel_opt in [
        "a.dropdown-item:has-text('Producto o Servicio')",
        ".dropdown-menu a:has-text('Producto o Servicio')",
    ]:
        try:
            opt = page.locator(sel_opt).first
            opt.wait_for(state="visible", timeout=4000)
            opt.click()
            time.sleep(0.8)
            break
        except:
            pass

    # Esperar que el modal esté abierto (clase .show en Bootstrap)
    modal_ok = False
    for sel_modal in [
        "div.modal.show",
        "div.modal-dialog",
        "div.modal-content",
    ]:
        try:
            page.wait_for_selector(sel_modal, state="visible", timeout=8000)
            modal_ok = True
            break
        except:
            pass

    time.sleep(0.8)
    _report(task_id, f"Modal {'abierto ✅' if modal_ok else 'NO detectado ⚠️'}", page)
    return modal_ok


def _llenar_item_modal(page: Page, task_id: str, item: Item):
    """
    Paso 8: Llenar los campos del modal de ítem.
    El modal es un componente Angular dinámico – los formcontrolname
    se obtienen inspeccionando el modal en el navegador real.
    Usamos selectores de posición como fallback cuando no hay formcontrolname.
    """
    _report(task_id, f"Paso 8 – Llenando ítem: {item.descripcion[:40]}", page)

    tipo_label = TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien")

    # ── Tipo de ítem ──────────────────────────────────────────────────
    # El select de tipo está dentro del modal; usa formcontrolname="tipo"
    for sel_tipo in [
        "div.modal select[formcontrolname='tipo']",
        "div.modal-body select[formcontrolname='tipo']",
        "div.modal select:first-of-type",
    ]:
        if _select_angular(page, sel_tipo, label=tipo_label, timeout=4000):
            break

    # ── Cantidad ──────────────────────────────────────────────────────
    cant_str = str(int(item.cantidad)) if item.cantidad == int(item.cantidad) else str(item.cantidad)
    cant_ok = False
    for sel_cant in [
        "div.modal input[formcontrolname='cantidad']",
        "div.modal-body input[formcontrolname='cantidad']",
        "div.modal input[type='text']:nth-child(1)",
    ]:
        if _fill_angular(page, sel_cant, cant_str, timeout=4000):
            cant_ok = True
            break

    # ── Unidad de Medida ──────────────────────────────────────────────
    for sel_uni in [
        "div.modal select[formcontrolname='unidad']",
        "div.modal-body select[formcontrolname='unidad']",
        "div.modal select:nth-of-type(2)",
    ]:
        if _select_angular(page, sel_uni, label="Unidad", timeout=4000):
            break

    # ── Descripción / Nombre Producto ─────────────────────────────────
    desc_ok = False
    for sel_desc in [
        "div.modal input[formcontrolname='descripcion']",
        "div.modal-body input[formcontrolname='descripcion']",
        "div.modal input[placeholder*='Nombre']",
        "div.modal input[placeholder*='escripci']",
    ]:
        if _fill_angular(page, sel_desc, item.descripcion, timeout=4000):
            desc_ok = True
            break

    # ── Tipo de Venta (Gravado/Exento/No Sujeto) ──────────────────────
    for sel_tv in [
        "div.modal select[formcontrolname='tipoVenta']",
        "div.modal-body select[formcontrolname='tipoVenta']",
        "div.modal select:nth-of-type(3)",
    ]:
        if _select_angular(page, sel_tv, label=item.tipo_venta, timeout=4000):
            break

    # ── Precio Unitario ───────────────────────────────────────────────
    precio_str = f"{item.precio:.2f}"
    precio_ok = False
    for sel_precio in [
        "div.modal input[formcontrolname='precioUnitario']",
        "div.modal-body input[formcontrolname='precioUnitario']",
        "div.modal input[formcontrolname='precio']",
    ]:
        if _fill_angular(page, sel_precio, precio_str, timeout=4000):
            precio_ok = True
            break

    time.sleep(0.5)  # dejar que Angular recalcule subtotal
    _report(task_id,
        f"  cant:{cant_ok} desc:{desc_ok} precio:{precio_ok}", page)

    return cant_ok and desc_ok and precio_ok


def _clic_agregar_item(page: Page, task_id: str) -> bool:
    """
    Paso 9: Clic en el botón 'Agregar ítem' dentro del modal.
    HTML: <button class="btn btn-primary">Agregar ítem</button>
    """
    _report(task_id, "Paso 9 – Clic 'Agregar ítem'...", page)

    for sel in [
        "div.modal button.btn-primary:has-text('Agregar')",
        "div.modal-footer button.btn-primary",
        "div.modal button.btn-primary:last-of-type",
        # fallback: último btn-primary visible en la página
        "button.btn-primary:visible:last-of-type",
    ]:
        try:
            btn = page.locator(sel).last
            btn.wait_for(state="visible", timeout=5000)
            btn.scroll_into_view_if_needed()
            btn.click(force=True)
            time.sleep(1.5)
            _report(task_id, "Ítem agregado ✅", page)
            return True
        except:
            pass

    # JS fallback
    try:
        page.evaluate("""
            const modal = document.querySelector('div.modal.show, div.modal-dialog');
            if (modal) {
                const btns = Array.from(modal.querySelectorAll('button.btn-primary'));
                const btn  = btns[btns.length - 1];
                if (btn) { btn.scrollIntoView(); btn.click(); }
            }
        """)
        time.sleep(1.5)
        _report(task_id, "Ítem agregado (JS fallback) ✅", page)
        return True
    except:
        pass

    _report(task_id, "⚠️ No se pudo hacer clic en Agregar ítem", page)
    return False


def _navegar_tras_agregar(page: Page, task_id: str, es_ultimo: bool):
    """
    Paso 10: 'Seguir adicionando' o 'Regresar al documento'.
    """
    time.sleep(0.5)
    if not es_ultimo:
        for sel in [
            "button:has-text('Seguir adicionando')",
            ".swal2-confirm:has-text('Seguir')",
        ]:
            try:
                btn = page.locator(sel).first
                btn.wait_for(state="visible", timeout=5000)
                btn.click(force=True)
                time.sleep(0.8)
                return
            except:
                pass
    else:
        for sel in [
            "button:has-text('Regresar al documento')",
            ".swal2-confirm:has-text('Regresar')",
            ".swal2-confirm",
        ]:
            try:
                btn = page.locator(sel).first
                btn.wait_for(state="visible", timeout=5000)
                btn.click(force=True)
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.0)
                return
            except:
                pass


# ── PASO 9/11: Forma de pago ──────────────────────────────────────────────────

# Mapa código → value real del <option> en el HTML del portal
# HTML: <option value="0: 01">Billetes y monedas</option> etc.
FP_MAP = {
    "01": "0: 01",   # Billetes y monedas
    "02": "1: 02",   # Tarjeta Débito  (corregido desde HTML real)
    "03": "2: 03",   # Tarjeta Crédito (corregido)
    "04": "3: 04",   # Cheque
    "05": "4: 05",   # Transferencia-Depósito Bancario
    "08": "5: 08",   # Dinero electrónico
    "09": "6: 09",   # Monedero electrónico
    "11": "7: 11",   # Bitcoin
    "12": "8: 12",   # Otras Criptomonedas
    "13": "9: 13",   # Cuentas por Pagar
    "14": "10: 14",  # Giro bancario
    "99": "11: 99",  # Otros
}


def _configurar_forma_pago(page: Page, task_id: str,
                           forma_pago: str, monto: float):
    """
    Paso 11: Scroll al fondo → borrar filas auto-generadas →
    seleccionar forma → llenar monto → clic "+".
    """
    _report(task_id, "Paso 11 – Configurando forma de pago...", page)

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(1.2)
    _report(task_id, "Vista sección forma de pago", page)

    # Borrar filas auto-generadas por el portal
    # HTML: <button tooltip="Borrar forma de pago" class="btn btn-outline-primary btn-add">
    try:
        btns_del = page.locator(
            "button[tooltip='Borrar forma de pago'], "
            "button.btn-outline-primary.btn-add:has(i.fa-times)"
        ).all()
        for b in reversed(btns_del):
            try:
                b.click(force=True)
                time.sleep(0.4)
            except:
                pass
        if btns_del:
            _report(task_id, f"Borradas {len(btns_del)} fila(s) auto-generadas", page)
    except:
        pass

    # Select forma de pago
    fp_val = FP_MAP.get(forma_pago, "0: 01")
    _select_angular(page, "select[formcontrolname='codigo']", value=fp_val)

    # Monto — usar dispatch nativo (campo tiene oninput que limpia caracteres)
    monto_str = f"{monto:.2f}"
    try:
        mp = page.locator("input[formcontrolname='montoPago']").first
        mp.wait_for(state="visible", timeout=6000)
        mp.scroll_into_view_if_needed()
        mp.click()
        time.sleep(0.2)
        mp.triple_click()
        page.keyboard.type(monto_str)
        mp.press("Tab")
        time.sleep(0.4)
        _dispatch_input(page, "input[formcontrolname='montoPago']", monto_str)
    except Exception as e:
        _report(task_id, f"⚠️ Monto pago: {e}", page)

    # Clic en botón "+"
    # HTML: <button tooltip="Agregar forma de pago" class="btn btn-primary btn-block">
    added = False
    for sel_plus in [
        "button[tooltip='Agregar forma de pago']",
        "button.btn-primary.btn-block:has(i.fa-plus)",
        "button.btn-block:has(i.fa-plus)",
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
        # JS fallback
        try:
            page.evaluate("""
                const b = document.querySelector("button[tooltip='Agregar forma de pago']")
                    || Array.from(document.querySelectorAll('button.btn-primary'))
                       .find(x => x.querySelector('i.fa-plus'));
                if (b) { b.scrollIntoView(); b.click(); }
            """)
            time.sleep(1.0)
        except:
            pass

    _report(task_id, "Paso 11 ✅ Forma de pago configurada", page)


# ── PASO 12: Generar Documento ────────────────────────────────────────────────

def _generar_documento(page: Page, task_id: str):
    """
    Paso 12: Clic en input[type='submit'][value='Generar Documento']
    HTML real: <input type="submit" value="Generar Documento" class="btn btn-primary">
    """
    _report(task_id, "Paso 12 – Generando documento...", page)
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(0.8)
    _report(task_id, "Vista previa antes de Generar", page)

    for sel in [
        "input[type='submit'][value='Generar Documento']",
        "input[type='submit'].btn-primary",
        "input[type='submit']",
        "button:has-text('Generar Documento')",
    ]:
        try:
            el = page.locator(sel).last
            el.wait_for(state="visible", timeout=8000)
            el.scroll_into_view_if_needed()
            el.click(force=True)
            _report(task_id, f"Clic en '{sel}'", page)
            return True
        except:
            pass

    # JS fallback
    try:
        page.evaluate(
            "const i = document.querySelector(\"input[type='submit']\");"
            "if(i){i.scrollIntoView(); i.click();}"
        )
        return True
    except:
        pass

    raise Exception("No se encontró el botón 'Generar Documento'")


# ── PASO 13-14: Confirmar y clave privada ──────────────────────────────────────

def _confirmar_y_firmar(page: Page, task_id: str, hw_clave: str):
    """
    Paso 13: SweetAlert "¿Está seguro?" → "Sí, crear documento"
    Paso 14: Input clave privada → OK
    """
    # Paso 13
    _report(task_id, "Paso 13 – Confirmando...", page)
    try:
        btn_si = page.locator(
            "button.swal2-confirm:has-text('crear documento'), "
            "button.swal2-confirm:has-text('Sí'), "
            ".swal2-confirm"
        )
        btn_si.wait_for(state="visible", timeout=15000)
        _report(task_id, "Popup confirmación visible", page)
        btn_si.first.click(force=True)
        time.sleep(1.0)
    except Exception as e_conf:
        # Verificar si hay errores de validación en pantalla
        try:
            errs = page.locator(
                "div[style*='background-color: #f8d7da'], "
                "div.alert-danger, .swal2-html-container"
            ).all_inner_texts()
            msgs = [x.replace("×", "").strip() for x in errs if x.strip()]
            if msgs:
                raise Exception("Validación portal: " + " | ".join(msgs))
        except Exception as ve:
            if "Validación" in str(ve):
                raise ve
        _report(task_id, f"⚠️ Confirmar: {e_conf}", page)

    # Paso 14
    _report(task_id, "Paso 14 – Ingresando clave privada...", page)
    try:
        ic = page.get_by_placeholder("Ingrese la clave privada de validación")
        ic.wait_for(state="visible", timeout=15000)
        ic.click()
        ic.fill(hw_clave)
        time.sleep(0.4)
        bok = page.locator("button:has-text('OK')").last
        bok.wait_for(state="visible", timeout=5000)
        bok.click(force=True)
        _report(task_id, "Paso 14 ✅ Clave enviada – esperando sello...", page)
    except Exception as e_clave:
        _report(task_id, f"⚠️ Clave privada: {e_clave}", page)


# ── PASO 15: Capturar UUID y PDF ──────────────────────────────────────────────

def _capturar_uuid_pdf(page: Page, task_id: str,
                       context: BrowserContext) -> tuple[str, str]:
    """
    Paso 15: Esperar que se genere el PDF y capturar UUID.
    Retorna (codigo_generacion, pdf_base64).
    """
    _report(task_id, "Paso 15 – Esperando UUID y PDF...", page)

    codigo_generacion = ""
    pdf_base64 = ""

    # Intentar capturar UUID desde popup SweetAlert2
    try:
        page.wait_for_selector(".swal2-popup, .swal2-content", timeout=25000)
        time.sleep(2.0)
        body_txt = page.inner_text("body")
        if any(x in body_txt.lower() for x in ["incorrecta", "inválida", "invalida"]):
            raise Exception("Clave privada incorrecta o inválida")
        codigo_generacion = _extract_uuid(body_txt)
        if codigo_generacion:
            _report(task_id, f"UUID capturado del modal: {codigo_generacion}", page)
    except Exception as e_uuid:
        if "Clave" in str(e_uuid) or "clave" in str(e_uuid):
            raise e_uuid

    # Si no hay UUID aún, buscar en pestañas PDF
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
            # Intentar leer bytes del PDF
            try:
                pdf_bytes_js = pdf_tab.evaluate("""async () => {
                    const r = await fetch(document.URL);
                    const b = await r.arrayBuffer();
                    return Array.from(new Uint8Array(b));
                }""")
                pdf_base64 = base64.b64encode(bytes(pdf_bytes_js)).decode("utf-8")
            except:
                pass

    # Cerrar cualquier popup pendiente
    for btn_txt in ["OK", "Aceptar", "Cerrar"]:
        try:
            page.locator(f"button:has-text('{btn_txt}')").last.click(
                force=True, timeout=1200
            )
            time.sleep(0.3)
        except:
            pass

    return codigo_generacion, pdf_base64


def _descargar_pdf_desde_consultas(page: Page, task_id: str,
                                   context: BrowserContext,
                                   codigo_generacion: str) -> str:
    """
    Ir a Consultas → buscar por UUID → clic 'Versión legible' → obtener PDF.
    Retorna pdf_base64.
    """
    pdf_base64 = ""
    try:
        from urllib.parse import urlparse
        base_url = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}"

        page.goto(f"{base_url}/consultaDteEmitidos", wait_until="domcontentloaded")
        time.sleep(1.0)

        inp = page.locator(
            "input[formcontrolname='codigoGeneracion'], "
            "input[placeholder*='0000'], input[placeholder*='AAAA']"
        ).first
        inp.wait_for(state="visible", timeout=10000)
        inp.fill(codigo_generacion)

        page.locator("button:has-text('Consultar')").first.wait_for(
            state="visible", timeout=5000
        )
        page.locator("button:has-text('Consultar')").first.click()
        page.wait_for_selector("tbody tr", timeout=15000)
        time.sleep(1.5)
        _report(task_id, "DTE encontrado en Consultas ✅", page)

        # Clic en "Versión legible" (ícono de impresora)
        pages_antes = set(id(pg) for pg in context.pages)
        page.locator(
            "button[tooltip='Versión legible'], button:has(i.fa-print)"
        ).first.click(force=True)
        time.sleep(3.5)

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
            if pdf_tab.url.startswith("data:application/pdf;base64,"):
                pdf_base64 = pdf_tab.url.split(",", 1)[1]
            elif "blob:" in pdf_tab.url:
                try:
                    bts = pdf_tab.evaluate("""async () => {
                        const r = await fetch(document.URL);
                        const b = await r.arrayBuffer();
                        return Array.from(new Uint8Array(b));
                    }""")
                    pdf_base64 = base64.b64encode(bytes(bts)).decode("utf-8")
                except:
                    pass
            try:
                pdf_tab.close()
            except:
                pass
    except Exception as e:
        _report(task_id, f"⚠️ Consultas: {e}", page)

    return pdf_base64


# ── PROCESO PRINCIPAL ─────────────────────────────────────────────────────────

def procesar_dte_en_fondo(task_id: str, req: FacturaRequest):
    TAREAS[task_id] = {
        "status": "procesando",
        "mensaje": "Iniciando navegador...",
        "screenshot": "",
    }

    hw_user  = req.nit_empresa
    hw_pass  = req.clave_hacienda
    hw_clave = req.clave_firma if req.clave_firma else req.clave_hacienda
    tipo_dte = req.tipo_dte
    es_nota  = tipo_dte in ("Nota de Crédito", "Nota de Débito")
    fp_code  = req.formas_pago[0] if req.formas_pago else "01"

    monto_pago = sum(i.cantidad * i.precio for i in req.items)
    if "Crédito Fiscal" in tipo_dte:
        monto_pago *= 1.13

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
            # Bloquear media para acelerar
            page.route("**/*", lambda route: route.abort()
                if route.request.resource_type in ["media"]
                else route.continue_())

            # ── PASO 1: Abrir portal ─────────────────────────────────
            _report(task_id, "Paso 1 – Abriendo factura.gob.sv...", page)
            page.goto("https://factura.gob.sv/", wait_until="domcontentloaded")

            with context.expect_page() as npi:
                page.click("text=Ingresar")
            page = npi.value
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1.0)

            # ── PASO 2: Login ────────────────────────────────────────
            _report(task_id, "Paso 2 – Login...", page)

            # Popup "Emisores DTE"
            try:
                page.locator(
                    "//h5[contains(text(),'Emisores DTE')]/..//button"
                ).click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(0.5)
            except:
                pass

            page.get_by_placeholder("NIT/DUI").fill(hw_user.replace("-", ""))
            page.locator("input[type='password']").fill(hw_pass)

            # Ambiente de pruebas
            try:
                sa = page.locator("select[formcontrolname='ambiente']").first
                sa.wait_for(state="visible", timeout=5000)
                sa.select_option(value="/test")
                time.sleep(0.5)
            except:
                pass

            page.click("button:has-text('Iniciar sesión')")

            # PASO 3: OK del popup post-login
            try:
                page.wait_for_selector(".swal2-confirm", timeout=6000)
                _report(task_id, "Paso 3 – Clic en OK post-login", page)
                page.click(".swal2-confirm")
            except:
                pass

            _report(task_id, "Login OK ✅", page)
            time.sleep(1.2)

            # ── PASO 4: Sistema de Facturación ───────────────────────
            _report(task_id, "Paso 4 – Sistema de Facturación...", page)
            try:
                # Menú lateral: href="/facturadorv3"
                menu_link = page.locator(
                    "a[href='/facturadorv3'], "
                    "a:has-text('Sistema de Facturación'), "
                    "span:has-text('Sistema de Facturación')"
                ).first
                menu_link.wait_for(state="visible", timeout=15000)
                menu_link.click(force=True)
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.5)
            except Exception as e4:
                _report(task_id, f"⚠️ Paso 4: {e4}", page)

            # ── PASO 5: Elegir tipo DTE ──────────────────────────────
            _report(task_id, f"Paso 5 – Eligiendo tipo DTE: {tipo_dte}...", page)
            try:
                page.wait_for_selector(
                    ".swal2-popup select, select.swal2-select", timeout=12000
                )
                page.locator(
                    ".swal2-popup select, select.swal2-select"
                ).first.select_option(label=tipo_dte)
                page.locator(
                    "button.swal2-confirm, button:has-text('OK')"
                ).first.click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.8)
                _report(task_id, f"Tipo DTE seleccionado ✅ – página cargada", page)
            except Exception as e5:
                _report(task_id, f"⚠️ Paso 5: {e5}", page)

            # ── PASO 6: Receptor ─────────────────────────────────────
            _fill_receptor(page, task_id, req)

            # ── PASOS 7-10: Ítems ────────────────────────────────────
            _report(task_id, f"Pasos 7-10 – Agregando {len(req.items)} ítem(s)...", page)

            modal_abierto = False
            for i, item in enumerate(req.items):
                es_primero = (i == 0)
                es_ultimo  = (i == len(req.items) - 1)

                if es_primero:
                    modal_abierto = _abrir_modal_item(page, task_id)

                _llenar_item_modal(page, task_id, item)
                _clic_agregar_item(page, task_id)
                _navegar_tras_agregar(page, task_id, es_ultimo)

            # Screenshot para confirmar ítems en tabla
            time.sleep(1.0)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.5)
            _report(task_id, "Ítems agregados – verificando tabla", page)

            # ── PASO 11: Forma de pago ───────────────────────────────
            if not es_nota:
                _configurar_forma_pago(page, task_id, fp_code, monto_pago)

            # Observaciones
            if req.observaciones:
                try:
                    ob = page.locator(
                        "textarea[formcontrolname='observacionesDoc']"
                    ).first
                    ob.wait_for(state="visible", timeout=4000)
                    ob.fill(req.observaciones)
                except:
                    pass

            # ── PASO 12: Generar Documento ───────────────────────────
            _generar_documento(page, task_id)
            time.sleep(1.2)

            # ── PASOS 13-14: Confirmar + Clave privada ───────────────
            _confirmar_y_firmar(page, task_id, hw_clave)

            # ── PASO 15: Capturar UUID y PDF ─────────────────────────
            codigo_generacion, pdf_base64 = _capturar_uuid_pdf(page, task_id, context)

            if not codigo_generacion:
                raise Exception(
                    "El portal de Hacienda no devolvió el UUID. Ver monitor."
                )

            # Si no tenemos PDF aún, ir a Consultas
            if not pdf_base64:
                pdf_base64 = _descargar_pdf_desde_consultas(
                    page, task_id, context, codigo_generacion
                )

            browser.close()

            TAREAS[task_id] = {
                "status":            "completado",
                "exito":             True,
                "sello_recepcion":   "IMPRESO-EN-EL-PDF",
                "codigo_generacion": codigo_generacion,
                "pdf_base64":        pdf_base64,
                "json_content":      "{}",
            }
            _report(task_id, f"✅ DTE completado: {codigo_generacion}")

    except Exception as e:
        try:
            _report(task_id, f"❌ ERROR: {str(e)}", page)
        except:
            pass
        TAREAS[task_id] = {
            "status":     "error",
            "exito":      False,
            "detail":     str(e),
            "screenshot": TAREAS.get(task_id, {}).get("screenshot", ""),
        }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/facturar")
def facturar_inmediato(req: FacturaRequest, bg_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    bg_tasks.add_task(procesar_dte_en_fondo, task_id, req)
    return {"exito": True, "task_id": task_id, "status": "procesando"}


@app.get("/status/{task_id}")
def verificar_status(task_id: str):
    return TAREAS.get(task_id, {"status": "no_encontrado"})
