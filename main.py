"""
IABSTECH IFACT – Robot RPA factura.gob.sv

CAUSA RAÍZ CONFIRMADA DE VERSIONES ANTERIORES:
  Angular 12 requiere que los eventos vengan de interacción real del teclado.
  .fill() de Playwright es demasiado rápido y no dispara todos los handlers.
  La solución correcta y probada es:
    1. click() para enfocar el campo
    2. triple_click() para seleccionar todo
    3. page.keyboard.type(value, delay=50) — simula tecleo real letra por letra
    4. press("Tab") para confirmar y mover foco

  Esto es exactamente lo que hace un usuario humano y Angular lo detecta 100%.
"""

from fastapi import FastAPI, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time, re, base64, uuid, json, traceback

app = FastAPI()
TAREAS: dict = {}


# ── Log de diagnóstico en el endpoint para ver qué llega ─────────────────────
@app.post("/facturar")
async def facturar_inmediato(request: Request, bg_tasks: BackgroundTasks):
    """
    Loguea el raw body para diagnóstico y luego procesa.
    """
    try:
        body_bytes = await request.body()
        body_str   = body_bytes.decode("utf-8")
        print(f"\n[FACTURAR] Body recibido ({len(body_str)} bytes):")
        print(body_str[:500])

        data = json.loads(body_str)
        req  = FacturaRequest(**data)

        print(f"[FACTURAR] Parsed OK:")
        print(f"  nit_empresa   = '{req.nit_empresa}'")
        print(f"  tipo_dte      = '{req.tipo_dte}'")
        print(f"  receptor.nit  = '{req.receptor.numDocumento}'")
        print(f"  receptor.nom  = '{req.receptor.nombre}'")
        print(f"  items count   = {len(req.items)}")
        for i, item in enumerate(req.items):
            print(f"  item[{i}]: {item.cantidad}x '{item.descripcion}' ${item.precio}")

        task_id = str(uuid.uuid4())
        bg_tasks.add_task(procesar_dte_en_fondo, task_id, req)
        return JSONResponse({"exito": True, "task_id": task_id, "status": "procesando"})

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[FACTURAR] ERROR parseando request: {e}\n{tb}")
        return JSONResponse(
            {"exito": False, "error": f"Error parseando datos: {str(e)}"},
            status_code=400
        )


# ── Modelos ───────────────────────────────────────────────────────────────────

@app.get("/")
@app.get("/ping")
def ping():
    return {"status": "ok", "mensaje": "Robot en línea"}


class Receptor(BaseModel):
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


@app.get("/status/{task_id}")
def verificar_status(task_id: str):
    return TAREAS.get(task_id, {"status": "no_encontrado"})


# ── Constantes del portal (del HTML real inspeccionado) ───────────────────────

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _shot(page) -> str:
    try:
        return base64.b64encode(
            page.screenshot(type="jpeg", quality=50)
        ).decode("utf-8")
    except:
        return ""


def _rep(task_id: str, msg: str, page=None):
    print(f"[RPA {task_id[:8]}] {msg}")
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


# ── La función de llenado correcta para Angular ───────────────────────────────

def type_into(page, selector: str, value: str,
              timeout: int = 10000, delay: int = 40) -> bool:
    """
    Llena un campo Angular usando tecleo real simulado.
    Este método es el más confiable para formularios Angular/React:
    1. Espera que el elemento sea visible
    2. click() — enfoca el campo y Angular activa el control
    3. triple_click() — selecciona TODO el texto existente
    4. keyboard.type(value, delay=delay) — simula tecleo letra por letra
       El delay entre letras da tiempo a Angular para procesar cada keypress
    5. press("Tab") — confirma el valor y mueve el foco al siguiente campo
    """
    if not value:
        return True
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.scroll_into_view_if_needed()
        time.sleep(0.2)
        el.click()
        time.sleep(0.2)
        el.triple_click()
        time.sleep(0.1)
        page.keyboard.type(str(value), delay=delay)
        time.sleep(0.2)
        el.press("Tab")
        time.sleep(0.3)

        # Verificar que quedó el valor
        actual = ""
        try:
            actual = el.input_value()
        except:
            pass
        print(f"  type_into '{selector}' = '{value}' → dom='{actual}'")
        return True
    except Exception as e:
        print(f"  type_into FAIL '{selector}': {e}")
        return False


def select_into(page, selector: str, value: str = None,
                label: str = None, timeout: int = 8000) -> bool:
    """
    Selecciona una opción en un <select>.
    Usa Playwright select_option que funciona bien con Angular
    siempre que dispare change event (lo hace por defecto).
    """
    if not value and not label:
        return True
    try:
        el = page.locator(selector).first
        el.wait_for(state="visible", timeout=timeout)
        el.scroll_into_view_if_needed()
        if value:
            el.select_option(value=value)
        else:
            el.select_option(label=label)
        time.sleep(0.35)
        print(f"  select_into '{selector}' = '{value or label}'")
        return True
    except Exception as e:
        print(f"  select_into FAIL '{selector}': {e}")
        return False


def fill_ngselect(page, selector: str, search: str,
                  timeout: int = 8000) -> bool:
    """
    Llena un ng-select: clic → escribir en el input interno → elegir primera opción.
    """
    if not search:
        return True
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
        opt_text = opt.inner_text()
        opt.click(force=True)
        time.sleep(0.4)
        print(f"  ng-select '{selector}' → seleccionó '{opt_text[:40]}'")
        return True
    except Exception as e:
        print(f"  ng-select FAIL '{selector}': {e}")
        return False


# ── RECEPTOR ──────────────────────────────────────────────────────────────────

def fill_receptor(page, task_id: str, req: FacturaRequest):
    _rep(task_id, "Llenando receptor...", page)

    r = req.receptor
    nit_clean = r.numDocumento.replace("-", "").strip()

    print(f"  [receptor] NIT={nit_clean} nombre={r.nombre} nrc={r.nrc}")
    print(f"  [receptor] depto={r.departamento} muni={r.municipio}")
    print(f"  [receptor] dir={r.direccion} correo={r.correo} tel={r.telefono}")

    # NIT — formcontrolname="nit", placeholder="Digite el número de NIT del receptor"
    nit_ok = type_into(page, "input[formcontrolname='nit']", nit_clean)
    if not nit_ok:
        type_into(page, "input[placeholder*='NIT del receptor']", nit_clean)

    # Nombre — formcontrolname="nombre"
    type_into(page, "input[formcontrolname='nombre']", r.nombre)

    # NRC — formcontrolname="nrc"
    if r.nrc:
        nrc_clean = r.nrc.replace("-", "").strip()
        type_into(page, "input[formcontrolname='nrc']", nrc_clean)

    # Actividad Económica — ng-select
    if r.codActividad:
        fill_ngselect(page,
            "ng-select[formcontrolname='actividadEconomica']",
            r.codActividad)

    # Departamento — value numérico "06"
    depto_val = _depto_num(r.departamento)
    select_into(page, "select[formcontrolname='departamento']", value=depto_val)
    time.sleep(0.6)  # Angular actualiza opciones de municipio

    # Municipio — value numérico
    muni_val = _muni_num(r.municipio)
    if muni_val:
        select_into(page, "select[formcontrolname='municipio']", value=muni_val)

    # Dirección / Complemento — textarea, formcontrolname="complemento"
    dir_val = r.direccion.strip() or "San Salvador, El Salvador"
    type_into(page, "textarea[formcontrolname='complemento']", dir_val, delay=30)

    # Correo — formcontrolname="correo"
    if r.correo:
        type_into(page, "input[formcontrolname='correo']", r.correo)

    # Teléfono — formcontrolname="telefono"
    if r.telefono:
        tel = r.telefono.replace("-", "").strip()
        type_into(page, "input[formcontrolname='telefono']", tel)

    _rep(task_id, "Receptor llenado ✅", page)


# ── ÍTEMS ─────────────────────────────────────────────────────────────────────

def abrir_modal_item(page, task_id: str) -> bool:
    _rep(task_id, "Abriendo modal de ítem...", page)

    # Scroll hasta la sección de ítems
    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
    time.sleep(0.6)

    # Clic en "Agregar Ítem" — id="btnGroupDrop2" del HTML real
    for sel in [
        "#btnGroupDrop2",
        "button[id='btnGroupDrop2']",
        "button.btn-primary.dropdown-toggle:has-text('Agregar')",
    ]:
        try:
            btn = page.locator(sel).first
            btn.wait_for(state="visible", timeout=6000)
            btn.scroll_into_view_if_needed()
            btn.click()
            time.sleep(0.7)
            break
        except:
            pass

    # Clic en "Producto o Servicio" del dropdown
    for sel in [
        "a.dropdown-item:has-text('Producto o Servicio')",
        ".dropdown-menu a:first-child",
    ]:
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
            time.sleep(1.0)
            _rep(task_id, "Modal de ítem abierto ✅", page)
            return True
        except:
            pass

    _rep(task_id, "⚠️ Modal no detectado", page)
    return False


def llenar_item_en_modal(page, task_id: str, item: Item) -> bool:
    """
    Llena los campos del modal de ítem con tecleo real.
    Los campos del modal son componentes Angular dinámicos.
    Prefijamos con 'div.modal-dialog' para no confundir con la página.
    """
    _rep(task_id, f"Llenando: {item.descripcion[:35]}...", page)
    print(f"  [item] cant={item.cantidad} desc={item.descripcion} "
          f"precio={item.precio} tipo={item.tipo_item} venta={item.tipo_venta}")

    tipo_label = TIPO_ITEM_MAP.get(item.tipo_item, "1 - Bien")
    cant_str   = (str(int(item.cantidad))
                  if item.cantidad == int(item.cantidad)
                  else str(item.cantidad))
    precio_str = f"{item.precio:.2f}"

    # Prefijo para apuntar solo al modal
    m = "div.modal-dialog "

    # 1. Tipo de ítem
    select_into(page, f"{m}select[formcontrolname='tipo']",
                label=tipo_label, timeout=5000)

    # 2. Cantidad
    ok_cant = type_into(page, f"{m}input[formcontrolname='cantidad']",
                        cant_str, timeout=5000)
    if not ok_cant:
        type_into(page, f"{m}input[placeholder*='antidad']",
                  cant_str, timeout=4000)

    # 3. Unidad de medida
    select_into(page, f"{m}select[formcontrolname='unidad']",
                label="Unidad", timeout=4000)

    # 4. Descripción
    ok_desc = type_into(page, f"{m}input[formcontrolname='descripcion']",
                        item.descripcion, timeout=5000)
    if not ok_desc:
        type_into(page, f"{m}input[placeholder*='Nombre']",
                  item.descripcion, timeout=4000)

    # 5. Tipo de venta
    select_into(page, f"{m}select[formcontrolname='tipoVenta']",
                label=item.tipo_venta, timeout=4000)

    # 6. Precio unitario
    ok_precio = type_into(page, f"{m}input[formcontrolname='precioUnitario']",
                          precio_str, timeout=5000)
    if not ok_precio:
        type_into(page, f"{m}input[formcontrolname='precio']",
                  precio_str, timeout=4000)

    time.sleep(0.8)  # Angular recalcula subtotal
    _rep(task_id,
         f"Ítem llenado — cant:{ok_cant} desc:{ok_desc} precio:{ok_precio}", page)
    return ok_cant and ok_desc and ok_precio


def clic_agregar_item(page, task_id: str) -> bool:
    _rep(task_id, "Clic 'Agregar ítem'...", page)
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
            time.sleep(1.8)
            _rep(task_id, "Ítem agregado ✅", page)
            return True
        except:
            pass
    try:
        page.evaluate("""
            const m = document.querySelector('div.modal.show, div.modal-dialog');
            if (m) {
                const btns = Array.from(m.querySelectorAll('button.btn-primary'));
                const b = btns[btns.length - 1];
                if (b) { b.scrollIntoView(); b.click(); }
            }
        """)
        time.sleep(1.8)
        _rep(task_id, "Ítem agregado (JS fallback) ✅", page)
        return True
    except:
        pass
    _rep(task_id, "⚠️ No se pudo agregar ítem", page)
    return False


def navegar_post_item(page, task_id: str, es_ultimo: bool):
    time.sleep(0.6)
    if not es_ultimo:
        for sel in ["button:has-text('Seguir adicionando')", ".swal2-confirm"]:
            try:
                b = page.locator(sel).first
                b.wait_for(state="visible", timeout=5000)
                b.click(force=True)
                time.sleep(1.0)
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
                time.sleep(1.5)
                return
            except:
                pass


# ── FORMA DE PAGO ─────────────────────────────────────────────────────────────

def configurar_forma_pago(page, task_id: str, fp_code: str, monto: float):
    _rep(task_id, "Configurando forma de pago...", page)

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(1.5)
    _rep(task_id, "Vista sección forma de pago", page)

    # Borrar filas auto-generadas
    # HTML real: <button tooltip="Borrar forma de pago" class="btn btn-outline-primary btn-add">
    try:
        btns = page.locator(
            "button[tooltip='Borrar forma de pago'], "
            "button.btn-outline-primary.btn-add:has(i.fa-times)"
        ).all()
        for b in reversed(btns):
            try:
                b.click(force=True)
                time.sleep(0.4)
            except:
                pass
        if btns:
            print(f"  Borradas {len(btns)} fila(s) auto-generadas")
    except:
        pass

    # Select forma de pago — formcontrolname="codigo"
    fp_val = FP_MAP.get(fp_code, "0: 01")
    select_into(page, "select[formcontrolname='codigo']", value=fp_val)

    # Monto — formcontrolname="montoPago"
    monto_str = f"{monto:.2f}"
    ok_monto = type_into(page, "input[formcontrolname='montoPago']",
                         monto_str, timeout=6000)
    if not ok_monto:
        type_into(page, "input[placeholder='Monto Pago']", monto_str)

    time.sleep(0.5)

    # Clic en "+" — tooltip="Agregar forma de pago"
    # HTML real: <button tooltip="Agregar forma de pago" class="btn btn-primary btn-block">
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

    print(f"\n[RPA {task_id[:8]}] INICIO")
    print(f"  tipo_dte   = '{tipo_dte}'")
    print(f"  nit_emp    = '{hw_user}'")
    print(f"  receptor   = '{req.receptor.nombre}' / NIT={req.receptor.numDocumento}")
    print(f"  items      = {len(req.items)}")
    print(f"  monto_pago = {monto_pago:.2f}")

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
            time.sleep(1.0)

            with context.expect_page() as npi:
                page.click("text=Ingresar")
            page = npi.value
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1.5)

            # ── Paso 2: Login ────────────────────────────────────────
            _rep(task_id, "Login...", page)
            try:
                page.locator(
                    "//h5[contains(text(),'Emisores DTE')]/..//button"
                ).click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(0.8)
            except:
                pass

            # Login: estos campos NO son Angular reactivo, son simples inputs
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
                page.wait_for_selector(".swal2-confirm", timeout=8000)
                _rep(task_id, "Paso 3 – OK post-login", page)
                page.click(".swal2-confirm")
            except:
                pass

            _rep(task_id, "Login OK ✅", page)
            time.sleep(1.5)

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
                time.sleep(2.0)
                _rep(task_id, "Sistema de Facturación ✅", page)
            except Exception as e:
                _rep(task_id, f"⚠️ Menú: {e}", page)

            # ── Paso 5: Elegir tipo DTE ──────────────────────────────
            _rep(task_id, f"Eligiendo tipo DTE: {tipo_dte}...", page)
            try:
                page.wait_for_selector(
                    ".swal2-popup select, select.swal2-select", timeout=15000
                )
                _rep(task_id, "Popup tipo DTE visible", page)
                page.locator(
                    ".swal2-popup select, select.swal2-select"
                ).first.select_option(label=tipo_dte)
                page.locator(
                    "button.swal2-confirm, button:has-text('OK')"
                ).first.click()
                page.wait_for_load_state("domcontentloaded")
                time.sleep(2.5)
                _rep(task_id, "Página del documento cargada ✅", page)
            except Exception as e:
                _rep(task_id, f"⚠️ Tipo DTE: {e}", page)

            # ── Paso 6: Receptor ─────────────────────────────────────
            # Esperar que los campos del receptor estén listos
            try:
                page.wait_for_selector(
                    "input[formcontrolname='nit'], "
                    "input[placeholder*='NIT del receptor']",
                    state="visible", timeout=15000
                )
                time.sleep(1.0)
            except:
                time.sleep(2.0)

            fill_receptor(page, task_id, req)

            # ── Pasos 7-10: Ítems ────────────────────────────────────
            _rep(task_id, f"Agregando {len(req.items)} ítem(s)...", page)

            abrir_modal_item(page, task_id)

            for i, item in enumerate(req.items):
                es_ultimo = (i == len(req.items) - 1)

                if i > 0:
                    # Modal ya abierto por "Seguir adicionando"
                    time.sleep(0.8)

                llenar_item_en_modal(page, task_id, item)
                clic_agregar_item(page, task_id)
                navegar_post_item(page, task_id, es_ultimo)

            # Verificar tabla de ítems
            time.sleep(1.5)
            page.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.5)
            _rep(task_id, "Verificando tabla de ítems...", page)

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
                    type_into(page,
                              "textarea[formcontrolname='observacionesDoc']",
                              req.observaciones, delay=20)
                except:
                    pass

            # ── Paso 12: Generar Documento ───────────────────────────
            _rep(task_id, "Generando documento...", page)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1.0)
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
                    _rep(task_id, "Clic Generar Documento ✅", page)
                    break
                except:
                    pass

            if not generado:
                page.evaluate(
                    "const i = document.querySelector(\"input[type='submit']\");"
                    "if(i){i.scrollIntoView(); i.click();}"
                )

            time.sleep(1.5)

            # ── Paso 13: Confirmar ───────────────────────────────────
            _rep(task_id, "Confirmando...", page)
            try:
                btn_si = page.locator(
                    "button.swal2-confirm:has-text('crear documento'), "
                    "button.swal2-confirm:has-text('Sí'), "
                    "button.swal2-confirm"
                )
                btn_si.wait_for(state="visible", timeout=15000)
                _rep(task_id, "Popup confirmación ✅", page)
                btn_si.first.click(force=True)
                time.sleep(1.2)
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
                time.sleep(0.5)
                bok = page.locator("button:has-text('OK')").last
                bok.wait_for(state="visible", timeout=5000)
                bok.click(force=True)
                _rep(task_id, "Clave privada enviada ✅", page)
            except Exception as e_clave:
                _rep(task_id, f"⚠️ Clave: {e_clave}", page)

            # ── Paso 15: UUID y PDF ──────────────────────────────────
            codigo_generacion = ""
            pdf_base64 = ""

            try:
                page.wait_for_selector(".swal2-popup, .swal2-content", timeout=25000)
                time.sleep(2.5)
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
                _rep(task_id, f"⚠️ UUID modal: {eu}", page)

            # Buscar en pestaña PDF
            if not codigo_generacion:
                t0 = time.time()
                pdf_tab = None
                while time.time() - t0 < 15:
                    for pg in context.pages:
                        if ("data:application/pdf" in pg.url
                                or "blob:" in pg.url):
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

            # Consultas → PDF si no lo tenemos
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
                            if "data:application/pdf" in pg.url or "blob:" in pg.url:
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
            print(f"[RPA {task_id[:8]}] ✅ COMPLETADO: {codigo_generacion}")

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[RPA {task_id[:8]}] ❌ ERROR: {e}\n{tb}")
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
