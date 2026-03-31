"""
IABSTECH IFACT – Robot RPA factura.gob.sv
Angular 12 (ng-version="12.2.17") usa formularios reactivos.

PROBLEMA RAÍZ DE VERSIONES ANTERIORES:
  Playwright .fill() escribe en el DOM pero NO dispara los eventos internos
  que Angular usa para actualizar el FormControl. Por eso los campos se
  ven vacíos o con el valor original cuando el portal valida.

SOLUCIÓN:
  Acceder directamente al FormControl de Angular via __ngContext__ y llamar
  setValue() + markAsDirty() + markAsTouched(). Esto es equivalente a lo
  que hace el usuario real al escribir.
"""

from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time, re, base64, uuid

app = FastAPI()
TAREAS: dict = {}


# ── Modelos ───────────────────────────────────────────────────────────────────

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


# ── Constantes del portal (del HTML real) ─────────────────────────────────────

# Valores reales de los <option> en el select de Forma de Pago
FP_MAP = {
    "01": "0: 01",   # Billetes y monedas
    "02": "1: 02",   # Tarjeta Débito
    "03": "2: 03",   # Tarjeta Crédito
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

TIPO_ITEM_MAP = {
    "1 - Bien":            "1 - Bien",
    "2 - Servicio":        "2 - Servicio",
    "3 - Bien y servicio": "3 - Bien y servicio",
}


# ── JavaScript central: llenar campos Angular 12 ─────────────────────────────

# Este script JS es el NÚCLEO de la solución.
# Angular 12 almacena el FormControl en __ngContext__ del elemento DOM.
# Al llamar setValue() directamente sobre el FormControl, Angular detecta
# el cambio como si el usuario hubiera escrito el valor.
JS_SET_INPUT = """
(args) => {
    const [selector, value] = args;
    const el = document.querySelector(selector);
    if (!el) return {ok: false, msg: 'no element: ' + selector};

    // ── Método 1: Angular __ngContext__ (Angular 9+) ──────────────
    try {
        const ctxKey = Object.keys(el).find(k => k.startsWith('__ngContext__'));
        if (ctxKey) {
            const ctx = el[ctxKey];
            for (let i = 0; i < ctx.length; i++) {
                const node = ctx[i];
                if (node && typeof node.setValue === 'function' && '_onChange' in node) {
                    node.setValue(value);
                    node.markAsDirty();
                    node.markAsTouched();
                    node._onChange.forEach(fn => { try { fn(value); } catch(e) {} });
                    node._onTouched.forEach(fn => { try { fn(); } catch(e) {} });
                    return {ok: true, msg: 'angular FormControl setValue at ' + i};
                }
            }
        }
    } catch(e) {}

    // ── Método 2: nativeInputValueSetter + eventos DOM ────────────
    try {
        let setter;
        if (el.tagName === 'TEXTAREA') {
            setter = Object.getOwnPropertyDescriptor(
                window.HTMLTextAreaElement.prototype, 'value').set;
        } else {
            setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
        }
        setter.call(el, value);
        ['input','change','blur'].forEach(ev =>
            el.dispatchEvent(new Event(ev, {bubbles: true})));
        return {ok: true, msg: 'native setter + events'};
    } catch(e) {}

    return {ok: false, msg: 'all methods failed'};
}
"""

# Para <select> con Angular
JS_SET_SELECT = """
(args) => {
    const [selector, value] = args;
    const el = document.querySelector(selector);
    if (!el) return {ok: false, msg: 'no element: ' + selector};

    // Establecer valor DOM
    el.value = value;

    // Angular __ngContext__
    try {
        const ctxKey = Object.keys(el).find(k => k.startsWith('__ngContext__'));
        if (ctxKey) {
            const ctx = el[ctxKey];
            for (let i = 0; i < ctx.length; i++) {
                const node = ctx[i];
                if (node && typeof node.setValue === 'function' && '_onChange' in node) {
                    node.setValue(value);
                    node.markAsDirty();
                    node.markAsTouched();
                    node._onChange.forEach(fn => { try { fn(value); } catch(e) {} });
                    return {ok: true, msg: 'angular FormControl setValue select'};
                }
            }
        }
    } catch(e) {}

    // Fallback eventos DOM
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur',   {bubbles: true}));
    return {ok: true, msg: 'DOM events fallback'};
}
"""

# Para ng-select (componente Angular personalizado de actividad económica)
JS_SET_NGSELECT = """
(args) => {
    const [selector, searchText] = args;
    const comp = document.querySelector(selector);
    if (!comp) return {ok: false, msg: 'no ng-select: ' + selector};
    try {
        const ctxKey = Object.keys(comp).find(k => k.startsWith('__ngContext__'));
        if (ctxKey) {
            const ctx = comp[ctxKey];
            for (let i = 0; i < ctx.length; i++) {
                const node = ctx[i];
                if (node && typeof node.open === 'function') {
                    node.open();
                    if (node.searchTerm !== undefined) node.searchTerm = searchText;
                    if (node.filter)   node.filter(searchText);
                    if (node._items && node._items.length > 0) {
                        node.select(node._items[0]);
                        return {ok: true, msg: 'ng-select item selected'};
                    }
                }
            }
        }
    } catch(e) {}
    return {ok: false, msg: 'ng-select not found in context'};
}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _shot(page) -> str:
    try:
        return base64.b64encode(
            page.screenshot(type="jpeg", quality=45)
        ).decode("utf-8")
    except:
        return ""


def _rep(task_id: str, msg: str, page=None):
    print(f"[{task_id[:8]}] {msg}")
    TAREAS[task_id]["mensaje"] = msg
    if page:
        s = _shot(page)
        if s:
            TAREAS[task_id]["screenshot"] = s


def _extract_uuid(text: str) -> str:
    m = re.search(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
        r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", text
    )
    return m.group(0).upper() if m else ""


def _depto_num(raw: str) -> str:
    """'06 - SAN SALVADOR' → '06'"""
    if not raw:
        return "06"
    return raw.split(" - ")[0].strip().zfill(2)


def _muni_num(raw: str) -> str:
    """'23 - SAN SALVADOR CENTRO' → '23'"""
    if not raw:
        return ""
    return raw.split(" - ")[0].strip()


# ── Funciones de llenado para Angular 12 ─────────────────────────────────────

def angular_fill(page, selector: str, value: str, timeout: int = 8000) -> bool:
    """
    Llena un <input> o <textarea> en Angular 12.
    Primero espera que el elemento sea visible, luego usa JS para
    actualizar el FormControl interno y disparar todos los eventos.
    """
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.scroll_into_view_if_needed()

        # Llamada JS que accede al FormControl de Angular
        result = page.evaluate(JS_SET_INPUT, [selector, str(value)])
        time.sleep(0.3)

        # Verificación: leer el valor actual del DOM
        actual = page.locator(selector).first.input_value()
        if str(value) in actual or actual in str(value):
            return True

        # Si la verificación falla, intentar con click + keyboard como último recurso
        el.click()
        time.sleep(0.15)
        page.keyboard.press("Control+a")
        page.keyboard.type(str(value))
        el.press("Tab")
        time.sleep(0.3)
        return True
    except Exception as e:
        print(f"  angular_fill '{selector}' = '{value}': {e}")
        return False


def angular_select(page, selector: str, value: str = None,
                   label: str = None, timeout: int = 8000) -> bool:
    """
    Selecciona una opción en un <select> Angular 12.
    """
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.scroll_into_view_if_needed()

        # Obtener el value real de la opción si se pasó label
        if label and value is None:
            value = page.evaluate(
                """([sel, lbl]) => {
                    const sel_el = document.querySelector(sel);
                    if (!sel_el) return null;
                    const opt = Array.from(sel_el.options)
                        .find(o => o.text.trim() === lbl || o.text.includes(lbl));
                    return opt ? opt.value : null;
                }""",
                [selector, label]
            )

        if value is None:
            return False

        result = page.evaluate(JS_SET_SELECT, [selector, str(value)])
        time.sleep(0.3)
        return True
    except Exception as e:
        print(f"  angular_select '{selector}' = '{value}': {e}")
        return False


def angular_ngselect(page, selector: str, search_text: str,
                     timeout: int = 8000) -> bool:
    """
    Selecciona un valor en ng-select (componente Angular especial).
    Estrategia: intentar vía JS primero, luego interacción manual.
    """
    try:
        comp = page.locator(selector).first
        comp.wait_for(state="visible", timeout=timeout)
        comp.scroll_into_view_if_needed()

        # Intento 1: JS directo al componente
        result = page.evaluate(JS_SET_NGSELECT, [selector, search_text])
        if result.get("ok"):
            time.sleep(0.4)
            return True

        # Intento 2: interacción manual (clic → escribir → elegir primera opción)
        comp.click(force=True)
        time.sleep(0.5)
        input_inside = comp.locator("input[type='text']").first
        input_inside.wait_for(state="visible", timeout=3000)
        input_inside.fill(search_text)
        time.sleep(1.2)

        option = page.locator("div.ng-option:not(.ng-option-disabled)").first
        option.wait_for(state="visible", timeout=4000)
        option.click(force=True)
        time.sleep(0.4)
        return True
    except Exception as e:
        print(f"  angular_ngselect '{selector}': {e}")
        return False


# ── RECEPTOR ──────────────────────────────────────────────────────────────────

def fill_receptor(page, task_id: str, req: FacturaRequest):
    _rep(task_id, "Llenando receptor...", page)
    is_ccf = "Crédito Fiscal" in req.tipo_dte

    nit_clean = req.receptor.numDocumento.replace("-", "").strip()

    # NIT del receptor — formcontrolname="nit" / placeholder="Digite el número de NIT"
    ok_nit = angular_fill(page,
        "input[formcontrolname='nit']", nit_clean)
    if not ok_nit:
        angular_fill(page,
            "input[placeholder*='NIT del receptor']", nit_clean)

    # Nombre del cliente — formcontrolname="nombre"
    angular_fill(page,
        "input[formcontrolname='nombre']", req.receptor.nombre)

    # NRC — formcontrolname="nrc"
    if req.receptor.nrc:
        angular_fill(page,
            "input[formcontrolname='nrc']",
            req.receptor.nrc.replace("-", "").strip())

    # Actividad Económica — ng-select con formcontrolname="actividadEconomica"
    if req.receptor.codActividad:
        cod = req.receptor.codActividad.split(" - ")[0].strip()
        angular_ngselect(page,
            "ng-select[formcontrolname='actividadEconomica']", cod)

    # Departamento — formcontrolname="departamento", value numérico "06"
    depto_val = _depto_num(req.receptor.departamento)
    angular_select(page,
        "select[formcontrolname='departamento']", value=depto_val)
    time.sleep(0.5)  # Angular actualiza las opciones de municipio

    # Municipio — formcontrolname="municipio", value numérico "23"
    muni_val = _muni_num(req.receptor.municipio)
    if muni_val:
        angular_select(page,
            "select[formcontrolname='municipio']", value=muni_val)

    # Dirección / Complemento — formcontrolname="complemento" (textarea)
    dir_val = req.receptor.direccion.strip() or "San Salvador, El Salvador"
    angular_fill(page,
        "textarea[formcontrolname='complemento']", dir_val)

    # Correo — formcontrolname="correo"
    if req.receptor.correo:
        angular_fill(page,
            "input[formcontrolname='correo']", req.receptor.correo)

    # Teléfono — formcontrolname="telefono"
    if req.receptor.telefono:
        tel = req.receptor.telefono.replace("-", "").strip()
        angular_fill(page,
            "input[formcontrolname='telefono']", tel)

    _rep(task_id, "Receptor llenado ✅", page)


# ── ÍTEMS ─────────────────────────────────────────────────────────────────────

def abrir_modal_item(page, task_id: str) -> bool:
    """Clic en btnGroupDrop2 → 'Producto o Servicio'."""
    _rep(task_id, "Abriendo modal de ítem...", page)

    # Scroll para que el botón esté visible
    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
    time.sleep(0.5)

    # Botón "Agregar Ítem" (btnGroupDrop2 del HTML real)
    abierto = False
    for sel in ["#btnGroupDrop2",
                "button[id='btnGroupDrop2']",
                "button.btn-primary.dropdown-toggle"]:
        try:
            btn = page.locator(sel).first
            btn.wait_for(state="visible", timeout=6000)
            btn.scroll_into_view_if_needed()
            btn.click()
            time.sleep(0.6)
            abierto = True
            break
        except:
            pass

    # Dropdown item "Producto o Servicio"
    for sel in ["a.dropdown-item:has-text('Producto o Servicio')",
                ".dropdown-menu a:first-child"]:
        try:
            opt = page.locator(sel).first
            opt.wait_for(state="visible", timeout=4000)
            opt.click()
            time.sleep(0.8)
            break
        except:
            pass

    # Esperar modal
    for sel in ["div.modal.show", "div.modal-dialog", "div.modal-content"]:
        try:
            page.wait_for_selector(sel, state="visible", timeout=8000)
            time.sleep(0.8)
            _rep(task_id, "Modal abierto ✅", page)
            return True
        except:
            pass

    _rep(task_id, "⚠️ Modal no detectado", page)
    return False


def llenar_item(page, task_id: str, item: Item) -> bool:
    """
    Llena los campos del modal de ítem usando Angular FormControl.
    Los formcontrolnames del modal se determinan por inspección del componente
    dinámico — son los mismos en Factura y CCF.
    """
    _rep(task_id, f"Llenando ítem: {item.descripcion[:35]}...", page)

    tipo_label = TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien")
    cant_str   = str(int(item.cantidad)) if item.cantidad == int(item.cantidad) else str(item.cantidad)
    precio_str = f"{item.precio:.2f}"

    # Los selectores deben apuntar DENTRO del modal para no confundirse
    # con los selects del documento principal
    def modal_sel(sel):
        return f"div.modal-dialog {sel}, div.modal.show {sel}"

    # 1. Tipo de ítem
    angular_select(page,
        modal_sel("select[formcontrolname='tipo']"),
        label=tipo_label, timeout=5000)

    # 2. Cantidad — formcontrolname="cantidad"
    ok_cant = angular_fill(page,
        modal_sel("input[formcontrolname='cantidad']"),
        cant_str, timeout=5000)
    if not ok_cant:
        angular_fill(page,
            modal_sel("input[placeholder='Cantidad']"),
            cant_str)

    # 3. Unidad de medida
    angular_select(page,
        modal_sel("select[formcontrolname='unidad']"),
        label="Unidad", timeout=4000)

    # 4. Descripción — formcontrolname="descripcion"
    ok_desc = angular_fill(page,
        modal_sel("input[formcontrolname='descripcion']"),
        item.descripcion, timeout=5000)
    if not ok_desc:
        angular_fill(page,
            modal_sel("input[placeholder*='Nombre']"),
            item.descripcion)

    # 5. Tipo de venta (Gravado / Exento / No Sujeto)
    angular_select(page,
        modal_sel("select[formcontrolname='tipoVenta']"),
        label=item.tipo_venta, timeout=4000)

    # 6. Precio unitario — formcontrolname="precioUnitario"
    ok_precio = angular_fill(page,
        modal_sel("input[formcontrolname='precioUnitario']"),
        precio_str, timeout=5000)
    if not ok_precio:
        angular_fill(page,
            modal_sel("input[formcontrolname='precio']"),
            precio_str)

    time.sleep(0.6)  # Angular recalcula subtotal
    _rep(task_id, f"Ítem llenado (cant:{ok_cant} desc:{ok_desc} precio:{ok_precio})", page)
    return ok_cant and ok_desc and ok_precio


def clic_agregar_item(page, task_id: str) -> bool:
    """Clic en el botón 'Agregar ítem' dentro del modal."""
    _rep(task_id, "Clic 'Agregar ítem'...", page)

    # Botón azul dentro del modal
    for sel in [
        "div.modal button.btn-primary:has-text('Agregar')",
        "div.modal-footer button.btn-primary",
        "div.modal button.btn-primary:last-of-type",
    ]:
        try:
            btn = page.locator(sel).last
            btn.wait_for(state="visible", timeout=5000)
            btn.scroll_into_view_if_needed()
            btn.click(force=True)
            time.sleep(1.5)
            _rep(task_id, "Ítem agregado ✅", page)
            return True
        except:
            pass

    # JS fallback: último btn-primary dentro del modal
    try:
        page.evaluate("""
            const m = document.querySelector('div.modal.show, div.modal-dialog');
            if (m) {
                const btns = Array.from(m.querySelectorAll('button.btn-primary'));
                const b = btns[btns.length - 1];
                if (b) { b.scrollIntoView(); b.click(); }
            }
        """)
        time.sleep(1.5)
        _rep(task_id, "Ítem agregado (JS) ✅", page)
        return True
    except:
        pass

    _rep(task_id, "⚠️ No se pudo agregar ítem", page)
    return False


def navegar_post_item(page, task_id: str, es_ultimo: bool):
    """'Seguir adicionando' o 'Regresar al documento'."""
    time.sleep(0.5)
    if not es_ultimo:
        for sel in ["button:has-text('Seguir adicionando')", ".swal2-confirm"]:
            try:
                b = page.locator(sel).first
                b.wait_for(state="visible", timeout=5000)
                b.click(force=True)
                time.sleep(0.8)
                return
            except:
                pass
    else:
        for sel in ["button:has-text('Regresar al documento')",
                    "button.swal2-confirm", ".swal2-confirm"]:
            try:
                b = page.locator(sel).first
                b.wait_for(state="visible", timeout=5000)
                b.click(force=True)
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.2)
                return
            except:
                pass


# ── FORMA DE PAGO ─────────────────────────────────────────────────────────────

def configurar_forma_pago(page, task_id: str, fp_code: str, monto: float):
    _rep(task_id, "Configurando forma de pago...", page)

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(1.2)
    _rep(task_id, "Vista sección forma de pago", page)

    # Borrar filas auto-generadas
    # HTML: <button tooltip="Borrar forma de pago" class="btn btn-outline-primary btn-add">
    try:
        btns = page.locator(
            "button[tooltip='Borrar forma de pago'], "
            "button.btn-outline-primary.btn-add:has(i.fa-times)"
        ).all()
        for b in reversed(btns):
            try:
                b.click(force=True)
                time.sleep(0.35)
            except:
                pass
    except:
        pass

    # Select forma de pago — formcontrolname="codigo"
    fp_val = FP_MAP.get(fp_code, "0: 01")
    angular_select(page, "select[formcontrolname='codigo']", value=fp_val)

    # Monto — formcontrolname="montoPago", placeholder="Monto Pago"
    monto_str = f"{monto:.2f}"
    ok_monto = angular_fill(page,
        "input[formcontrolname='montoPago']", monto_str)
    if not ok_monto:
        angular_fill(page,
            "input[placeholder='Monto Pago']", monto_str)

    time.sleep(0.4)

    # Botón "+" — tooltip="Agregar forma de pago"
    # HTML: <button tooltip="Agregar forma de pago" class="btn btn-primary btn-block">
    added = False
    for sel in [
        "button[tooltip='Agregar forma de pago']",
        "button.btn-primary.btn-block:has(i.fa-plus)",
        "button.btn-block:has(i.fa-plus)",
        "button.btn-primary:has(i.fa-plus)",
    ]:
        try:
            b = page.locator(sel).first
            b.wait_for(state="visible", timeout=4000)
            b.scroll_into_view_if_needed()
            b.click(force=True)
            added = True
            time.sleep(1.0)
            break
        except:
            pass

    if not added:
        page.evaluate("""
            const b = document.querySelector("button[tooltip='Agregar forma de pago']")
                   || Array.from(document.querySelectorAll('button.btn-primary'))
                      .find(x => x.querySelector('i.fa-plus'));
            if (b) { b.scrollIntoView(); b.click(); }
        """)
        time.sleep(1.0)

    _rep(task_id, "Forma de pago configurada ✅", page)


# ── PROCESO PRINCIPAL ─────────────────────────────────────────────────────────

def procesar_dte_en_fondo(task_id: str, req: FacturaRequest):
    TAREAS[task_id] = {
        "status": "procesando",
        "mensaje": "Iniciando...",
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
            page.route("**/*", lambda route: route.abort()
                if route.request.resource_type in ["media"]
                else route.continue_())

            # ── Paso 1: Abrir portal ─────────────────────────────────
            _rep(task_id, "Abriendo factura.gob.sv...", page)
            page.goto("https://factura.gob.sv/", wait_until="domcontentloaded")

            with context.expect_page() as npi:
                page.click("text=Ingresar")
            page = npi.value
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1.0)

            # ── Paso 2: Login ────────────────────────────────────────
            _rep(task_id, "Login...", page)
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

            # Ambiente pruebas
            try:
                sa = page.locator("select[formcontrolname='ambiente']").first
                sa.wait_for(state="visible", timeout=5000)
                sa.select_option(value="/test")
                time.sleep(0.5)
            except:
                pass

            page.click("button:has-text('Iniciar sesión')")

            # Paso 3: OK post-login
            try:
                page.wait_for_selector(".swal2-confirm", timeout=6000)
                _rep(task_id, "Paso 3 – OK post-login", page)
                page.click(".swal2-confirm")
            except:
                pass

            _rep(task_id, "Login OK ✅", page)
            time.sleep(1.2)

            # ── Paso 4: Sistema de Facturación ───────────────────────
            _rep(task_id, "Abriendo Sistema de Facturación...", page)
            try:
                menu = page.locator(
                    "a[href='/facturadorv3'], "
                    "span:has-text('Sistema de Facturación')"
                ).first
                menu.wait_for(state="visible", timeout=15000)
                menu.click(force=True)
                page.wait_for_load_state("domcontentloaded")
                time.sleep(1.8)
                _rep(task_id, "Sistema de Facturación abierto ✅", page)
            except Exception as e:
                _rep(task_id, f"⚠️ Menú facturación: {e}", page)

            # ── Paso 5: Elegir tipo DTE ──────────────────────────────
            _rep(task_id, f"Eligiendo tipo DTE: {tipo_dte}...", page)
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
                time.sleep(2.0)
                _rep(task_id, f"Tipo DTE seleccionado ✅", page)
            except Exception as e:
                _rep(task_id, f"⚠️ Tipo DTE: {e}", page)

            # ── Paso 6: Receptor ─────────────────────────────────────
            fill_receptor(page, task_id, req)

            # ── Pasos 7-10: Ítems ────────────────────────────────────
            _rep(task_id, f"Agregando {len(req.items)} ítem(s)...", page)

            abrir_modal_item(page, task_id)

            for i, item in enumerate(req.items):
                es_ultimo = (i == len(req.items) - 1)

                if i > 0:
                    # Para ítems 2,3,… el modal ya está abierto por "Seguir adicionando"
                    time.sleep(0.5)

                llenar_item(page, task_id, item)
                clic_agregar_item(page, task_id)
                navegar_post_item(page, task_id, es_ultimo)

            # Screenshot para verificar tabla de ítems
            time.sleep(1.0)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.5)
            _rep(task_id, "Ítems en tabla – verificando...", page)

            # ── Paso 11: Forma de pago ───────────────────────────────
            if not es_nota:
                configurar_forma_pago(page, task_id, fp_code, monto_pago)

            # Observaciones opcionales
            if req.observaciones:
                try:
                    ob = page.locator(
                        "textarea[formcontrolname='observacionesDoc']"
                    ).first
                    ob.wait_for(state="visible", timeout=4000)
                    angular_fill(page,
                        "textarea[formcontrolname='observacionesDoc']",
                        req.observaciones)
                except:
                    pass

            # ── Paso 12: Generar Documento ───────────────────────────
            _rep(task_id, "Generando documento...", page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.8)
            _rep(task_id, "Vista previa antes de generar", page)

            generado = False
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
                    generado = True
                    _rep(task_id, f"Clic Generar Documento ✅", page)
                    break
                except:
                    pass

            if not generado:
                page.evaluate(
                    "const i = document.querySelector(\"input[type='submit']\");"
                    "if(i){i.scrollIntoView(); i.click();}"
                )

            time.sleep(1.2)

            # ── Paso 13: Confirmar ───────────────────────────────────
            _rep(task_id, "Confirmando DTE...", page)
            try:
                btn_si = page.locator(
                    "button.swal2-confirm:has-text('crear documento'), "
                    "button.swal2-confirm:has-text('Sí'), "
                    "button.swal2-confirm"
                )
                btn_si.wait_for(state="visible", timeout=15000)
                _rep(task_id, "Popup confirmación ✅", page)
                btn_si.first.click(force=True)
                time.sleep(1.0)
            except Exception as e_conf:
                try:
                    errs = page.locator(
                        "div[style*='#f8d7da'], div.alert-danger, .swal2-html-container"
                    ).all_inner_texts()
                    msgs = [x.replace("×", "").strip() for x in errs if x.strip()]
                    if msgs:
                        raise Exception("Validación portal: " + " | ".join(msgs))
                except Exception as ve:
                    if "Validación" in str(ve):
                        raise ve
                _rep(task_id, f"⚠️ Confirmar: {e_conf}", page)

            # ── Paso 14: Clave privada ───────────────────────────────
            _rep(task_id, "Ingresando clave privada...", page)
            try:
                ic = page.get_by_placeholder("Ingrese la clave privada de validación")
                ic.wait_for(state="visible", timeout=15000)
                ic.click()
                ic.fill(hw_clave)
                time.sleep(0.4)
                bok = page.locator("button:has-text('OK')").last
                bok.wait_for(state="visible", timeout=5000)
                bok.click(force=True)
                _rep(task_id, "Clave enviada ✅ – esperando sello...", page)
            except Exception as e_clave:
                _rep(task_id, f"⚠️ Clave privada: {e_clave}", page)

            # ── Paso 15: Capturar UUID ───────────────────────────────
            codigo_generacion = ""
            pdf_base64 = ""

            try:
                page.wait_for_selector(".swal2-popup, .swal2-content", timeout=25000)
                time.sleep(2.0)
                body_txt = page.inner_text("body")
                if any(x in body_txt.lower()
                       for x in ["incorrecta", "inválida", "invalida"]):
                    raise Exception("Clave privada incorrecta")
                codigo_generacion = _extract_uuid(body_txt)
                if codigo_generacion:
                    _rep(task_id, f"UUID: {codigo_generacion} ✅", page)
            except Exception as eu:
                if "Clave" in str(eu) or "clave" in str(eu):
                    raise eu

            # Buscar UUID en pestaña PDF si no lo encontramos
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
                        bts = pdf_tab.evaluate("""async () => {
                            const r = await fetch(document.URL);
                            const b = await r.arrayBuffer();
                            return Array.from(new Uint8Array(b));
                        }""")
                        pdf_base64 = base64.b64encode(bytes(bts)).decode("utf-8")
                    except:
                        pass

            # Cerrar popups
            for txt in ["OK", "Aceptar", "Cerrar"]:
                try:
                    page.locator(f"button:has-text('{txt}')").last.click(
                        force=True, timeout=1200
                    )
                    time.sleep(0.3)
                except:
                    pass

            if not codigo_generacion:
                raise Exception(
                    "El portal de Hacienda no devolvió el UUID. Ver monitor."
                )

            # ── Consultas: descargar PDF si no lo tenemos ────────────
            if not pdf_base64:
                try:
                    from urllib.parse import urlparse
                    base_url = (f"{urlparse(page.url).scheme}://"
                                f"{urlparse(page.url).netloc}")
                    page.goto(f"{base_url}/consultaDteEmitidos",
                              wait_until="domcontentloaded")
                    time.sleep(1.0)

                    inp = page.locator(
                        "input[formcontrolname='codigoGeneracion'], "
                        "input[placeholder*='0000']"
                    ).first
                    inp.wait_for(state="visible", timeout=10000)
                    inp.fill(codigo_generacion)

                    page.locator("button:has-text('Consultar')").first.click()
                    page.wait_for_selector("tbody tr", timeout=15000)
                    time.sleep(1.5)

                    pages_antes = set(id(pg) for pg in context.pages)
                    page.locator(
                        "button[tooltip='Versión legible'], button:has(i.fa-print)"
                    ).first.click(force=True)
                    time.sleep(3.5)

                    pdf_tab2 = None
                    for pg in context.pages:
                        if id(pg) not in pages_antes:
                            pdf_tab2 = pg
                            break
                    if not pdf_tab2:
                        for pg in context.pages:
                            if ("data:application/pdf" in pg.url
                                    or "blob:" in pg.url):
                                pdf_tab2 = pg
                                break

                    if pdf_tab2:
                        if pdf_tab2.url.startswith("data:application/pdf;base64,"):
                            pdf_base64 = pdf_tab2.url.split(",", 1)[1]
                        elif "blob:" in pdf_tab2.url:
                            try:
                                bts2 = pdf_tab2.evaluate("""async () => {
                                    const r = await fetch(document.URL);
                                    const b = await r.arrayBuffer();
                                    return Array.from(new Uint8Array(b));
                                }""")
                                pdf_base64 = base64.b64encode(
                                    bytes(bts2)
                                ).decode("utf-8")
                            except:
                                pass
                        try:
                            pdf_tab2.close()
                        except:
                            pass
                except Exception as ec:
                    _rep(task_id, f"⚠️ Consultas PDF: {ec}", page)

            browser.close()

            TAREAS[task_id] = {
                "status":            "completado",
                "exito":             True,
                "sello_recepcion":   "IMPRESO-EN-EL-PDF",
                "codigo_generacion": codigo_generacion,
                "pdf_base64":        pdf_base64,
                "json_content":      "{}",
            }
            _rep(task_id, f"✅ DTE completado: {codigo_generacion}")

    except Exception as e:
        try:
            _rep(task_id, f"❌ ERROR: {str(e)}", page)
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
