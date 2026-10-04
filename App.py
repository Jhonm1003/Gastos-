import asyncio
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import flet as ft
import requests


URL_API = os.environ.get(
    "URL_API",
    "https://script.google.com/macros/s/AKfycby6n6HIadClwXCj3gZsgFG0HqZJIliiwsu6n83JzDM8SsuxHQLqfmiseFXlP1kk8TY/exec",
)
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "America/Bogota")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "20"))
API_CONNECT_TIMEOUT = float(os.environ.get("API_CONNECT_TIMEOUT", "8"))
API_READ_TIMEOUT = float(os.environ.get("API_READ_TIMEOUT", "35"))
API_REINTENTOS = int(os.environ.get("API_REINTENTOS", "2"))

# Solo las variables solicitadas
COLOR_MAP = {
    "Comida": "orange",
    "Transporte": "teal",
    "Universidad": "green",
    "Otros": "grey",
}

CATEGORIAS = list(COLOR_MAP.keys())


def obtener_zona_horaria():
    try:
        return ZoneInfo(APP_TIMEZONE)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=-5))


TZ = obtener_zona_horaria()


def hoy_local():
    return datetime.now(TZ).date()


def formato_moneda(valor):
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        numero = 0.0
    return f"${numero:,.0f}".replace(",", ".")


def convertir_monto(texto):
    if texto is None:
        raise ValueError("Monto vacío")

    valor = str(texto).strip().replace("$", "").replace(" ", "")
    if not valor:
        raise ValueError("Monto vacío")

    if "," in valor and "." in valor:
        if valor.rfind(",") > valor.rfind("."):
            valor = valor.replace(".", "").replace(",", ".")
        else:
            valor = valor.replace(",", "")
    elif "," in valor:
        partes = valor.split(",")
        if len(partes) > 2 or (len(partes) == 2 and len(partes[1]) == 3):
            valor = "".join(partes)
        else:
            valor = valor.replace(",", ".")
    elif "." in valor:
        partes = valor.split(".")
        if len(partes) > 2 or (len(partes) == 2 and len(partes[1]) == 3):
            valor = "".join(partes)

    numero = float(valor)
    if numero <= 0:
        raise ValueError("El monto debe ser mayor que cero")
    return numero


def monto_seguro(valor):
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def parsear_fecha(valor):
    if valor in (None, ""):
        return None
    texto = str(valor).strip()
    if not texto:
        return None
    try:
        iso = texto.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(TZ)
        return dt.date()
    except ValueError:
        pass

    parte_fecha = texto.split(" ")[0]
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(parte_fecha, formato).date()
        except ValueError:
            continue
    return None


async def main(page: ft.Page):
    page.title = "Mis Gastos Mensuales"
    page.theme_mode = "dark"
    page.padding = 10
    page.horizontal_alignment = "center"

    http = requests.Session()
    http.headers.update({
        "Accept": "application/json",
        "User-Agent": "MisGastosFlet/3.0",
    })

    cache = {"datos": None, "actualizado": 0.0}
    cache_lock = asyncio.Lock()
    api_write_lock = asyncio.Lock()

    def show_snackbar(mensaje, es_error=False):
        snack = ft.SnackBar(
            content=ft.Text(mensaje, color="white"),
            bgcolor="red" if es_error else "green",
        )
        page.snack_bar = snack
        snack.open = True
        page.update()

    def solicitar_json_sync(metodo, *, params=None, payload=None):
        ultimo_error = None
        for intento in range(API_REINTENTOS + 1):
            try:
                respuesta = http.request(
                    metodo, URL_API, params=params, json=payload,
                    timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT), allow_redirects=True,
                )
                if respuesta.status_code in (401, 403):
                    raise RuntimeError("El Web App de Google no permite acceso.")
                if respuesta.status_code >= 400:
                    raise RuntimeError(f"HTTP {respuesta.status_code}")
                try:
                    return respuesta.json()
                except ValueError as ex:
                    raise RuntimeError("El servidor no devolvió JSON válido") from ex
            except (requests.Timeout, requests.ConnectionError) as ex:
                ultimo_error = ex
                if intento < API_REINTENTOS:
                    time.sleep(0.8 * (intento + 1))
                    continue
                raise RuntimeError("No fue posible conectar.") from ex
            except requests.RequestException as ex:
                ultimo_error = ex
                if intento < API_REINTENTOS:
                    time.sleep(0.8 * (intento + 1))
                    continue
                raise RuntimeError(f"Error: {ex}") from ex
        raise RuntimeError(f"Fallo: {ultimo_error}")

    async def solicitar_json(metodo, *, params=None, payload=None):
        return await asyncio.to_thread(solicitar_json_sync, metodo, params=params, payload=payload)

    async def obtener_datos(force=False):
        ahora = time.monotonic()
        if not force and cache["datos"] is not None and (ahora - cache["actualizado"]) < CACHE_TTL_SECONDS:
            return cache["datos"]

        async with cache_lock:
            ahora = time.monotonic()
            if not force and cache["datos"] is not None and (ahora - cache["actualizado"]) < CACHE_TTL_SECONDS:
                return cache["datos"]

            datos = await solicitar_json("GET")
            if not isinstance(datos, list):
                raise RuntimeError("Error al consultar los gastos.")

            cache["datos"] = datos
            cache["actualizado"] = time.monotonic()
            return datos

    async def enviar_datos_api(payload, mensaje_exito):
        if api_write_lock.locked():
            show_snackbar("Procesando...", es_error=True)
            return False

        async with api_write_lock:
            try:
                resultado = await solicitar_json("POST", payload=payload)
                if not isinstance(resultado, dict) or resultado.get("status") != "success":
                    raise RuntimeError(resultado.get("message", "Operación rechazada"))

                cache["datos"] = None
                cache["actualizado"] = 0.0
                show_snackbar(resultado.get("message", mensaje_exito))
                return True
            except Exception as ex:
                show_snackbar(f"Error al guardar: {ex}", es_error=True)
                return False

    # ==========================================
    # 1. FORMULARIO REGISTRO Y ACCESO RÁPIDO
    # ==========================================
    monto_input = ft.TextField(label="Monto ($)", keyboard_type="number", prefix=ft.Text("$ "), border_radius=12)
    categoria_dropdown = ft.Dropdown(label="Categoría", border_radius=12, options=[ft.dropdown.Option(cat) for cat in CATEGORIAS])
    descripcion_input = ft.TextField(label="Descripción", border_radius=12)
    btn_guardar = ft.ElevatedButton("Guardar Gasto", icon="save", style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=12), padding=18), width=350)

    async def enviar_gasto(e):
        if not monto_input.value or not categoria_dropdown.value:
            show_snackbar("Ingresa el monto y la categoría", es_error=True)
            return
        try:
            monto = convertir_monto(monto_input.value)
        except ValueError:
            show_snackbar("Monto inválido", es_error=True)
            return

        payload = {
            "action": "agregar",
            "monto": monto,
            "categoria": categoria_dropdown.value,
            "descripcion": descripcion_input.value or "",
            "aplica4x1000": False,
            "valor4x1000": 0.0,
        }

        btn_guardar.disabled = True
        page.update()
        guardado = await enviar_datos_api(payload, f"¡Gasto guardado!")
        if guardado:
            monto_input.value = ""
            categoria_dropdown.value = None
            descripcion_input.value = ""
        btn_guardar.disabled = False
        page.update()

    btn_guardar.on_click = enviar_gasto

    # Accesos rápidos para transporte
    async def registrar_transporte_rapido(monto):
        payload = {
            "action": "agregar", "monto": float(monto), "categoria": "Transporte",
            "descripcion": "Transporte diario", "aplica4x1000": False, "valor4x1000": 0.0,
        }
        await enviar_datos_api(payload, f"✓ Transporte de {formato_moneda(monto)} registrado")

    def crear_tarjeta_transporte(monto):
        async def click_transporte(e):
            await registrar_transporte_rapido(monto)
        return ft.Container(
            content=ft.Column([ft.Icon("directions_bus", size=24, color="teal"), ft.Text(formato_moneda(monto), size=14, weight="bold")], alignment="center", horizontal_alignment="center"),
            bgcolor="#303030", border_radius=12, padding=10, ink=True, on_click=click_transporte, width=110, height=75,
        )

    vista_formulario = ft.Container(
        content=ft.Column(
            controls=[
                ft.Text("Nuevo Gasto", size=22, weight="bold"),
                monto_input, categoria_dropdown, descripcion_input, ft.Container(height=5), btn_guardar,
                ft.Divider(height=20, color="transparent"),
                ft.Text("Transporte Rápido", size=16, weight="bold"),
                ft.Row(
                    controls=[crear_tarjeta_transporte(3000), crear_tarjeta_transporte(4000), crear_tarjeta_transporte(5000)],
                    alignment="center", wrap=True, spacing=10
                ),
            ],
            spacing=12, horizontal_alignment="center"
        ),
        padding=20, visible=True,
    )

    # ==========================================
    # 2. HISTORIAL / REPORTES
    # ==========================================
    lista_gastos = ft.Column(spacing=8)
    
    def crear_control_gasto(item):
        cat = item.get("categoria", "Otros")
        monto = monto_seguro(item.get("monto"))
        desc = item.get("descripcion", "")
        color_cat = COLOR_MAP.get(cat, "grey")

        return ft.Container(
            content=ft.Row(
                controls=[
                    ft.Container(width=4, height=35, bgcolor=color_cat, border_radius=2),
                    ft.Column(controls=[ft.Text(cat, weight="bold", size=14), ft.Text(desc if desc else "Sin descripción", size=11, color="grey")], spacing=2, expand=True),
                    ft.Text(formato_moneda(monto), weight="bold", size=14),
                ],
                alignment="spaceBetween",
            ),
            padding=12, bgcolor="#303030", border_radius=12,
        )

    def crear_grupo_fecha(fecha, items_dia):
        total_dia = sum(monto_seguro(i.get("monto")) for i in items_dia)
        col_movimientos = ft.Column(controls=[], visible=False, spacing=8)
        icono_toggle = ft.Icon("keyboard_arrow_down", color="grey")
        estado = {"renderizado": False}

        def toggle_vis(e):
            if not estado["renderizado"]:
                col_movimientos.controls.extend(crear_control_gasto(item) for item in items_dia)
                estado["renderizado"] = True
            col_movimientos.visible = not col_movimientos.visible
            icono_toggle.name = "keyboard_arrow_up" if col_movimientos.visible else "keyboard_arrow_down"
            page.update()

        header = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Text(str(fecha), weight="bold", size=15),
                    ft.Row(controls=[ft.Text(formato_moneda(total_dia), weight="bold", size=14, color="grey"), icono_toggle], spacing=5),
                ],
                alignment="spaceBetween",
            ),
            ink=True, on_click=toggle_vis, padding=ft.padding.symmetric(horizontal=10, vertical=15), border_radius=8,
        )
        return ft.Container(content=ft.Column(controls=[header, col_movimientos], spacing=0), bgcolor="#1e1e1e", border_radius=10, margin=ft.margin.only(bottom=8))

    def renderizar_historial(datos):
        lista_gastos.controls.clear()
        if not datos:
            return

        gastos_por_fecha = defaultdict(list)
        for item in datos:
            fecha = parsear_fecha(item.get("fecha"))
            fecha_key = fecha.isoformat() if fecha else str(item.get("fecha", "Sin fecha")).split("T")[0]
            gastos_por_fecha[fecha_key].append(item)

        def clave_orden(fecha_texto):
            try: return (1, date.fromisoformat(fecha_texto))
            except ValueError: return (0, date.min)

        for fecha_key in sorted(gastos_por_fecha.keys(), key=clave_orden, reverse=True):
            lista_gastos.controls.append(crear_grupo_fecha(fecha_key, gastos_por_fecha[fecha_key]))

    async def cargar_historial(e=None, force=False):
        try:
            datos = await obtener_datos(force=force)
            renderizar_historial(datos)
        except Exception as ex:
            show_snackbar(f"Error: {ex}", es_error=True)
        page.update()

    vista_historial = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(controls=[ft.Text("Historial General", size=22, weight="bold"), ft.IconButton("refresh", on_click=lambda e: cargar_historial(force=True))], alignment="spaceBetween"),
                ft.Container(height=10),
                lista_gastos,
            ]
        ),
        padding=20, visible=False,
    )

    # ==========================================
    # 3. PRESUPUESTO MENSUAL
    # ==========================================
    presupuesto_input = ft.TextField(label="Monto base del mes ($)", keyboard_type="number", prefix=ft.Text("$ "), border_radius=12, expand=True)
    btn_presupuesto = ft.ElevatedButton("Guardar", icon="savings")

    presupuesto_text = ft.Text("$0", size=18, weight="bold")
    gastado_mes_text = ft.Text("$0", size=18, weight="bold", color="orange")
    disponible_mes_text = ft.Text("$0", size=32, weight="bold", color="green")
    
    resumen_barras_mes = ft.Column(spacing=12)

    def calcular_gastos_mes(datos):
        hoy = hoy_local()
        mes_actual = hoy.month
        año_actual = hoy.year

        gastos_mes = []
        totales_cat = {cat: 0.0 for cat in CATEGORIAS}

        if not datos:
            return gastos_mes, totales_cat

        for item in datos:
            fecha_item = parsear_fecha(item.get("fecha"))
            if not fecha_item:
                continue

            if fecha_item.month == mes_actual and fecha_item.year == año_actual:
                gastos_mes.append(item)
                cat = item.get("categoria", "Otros")
                if cat not in CATEGORIAS:
                    cat = "Otros"
                totales_cat[cat] += monto_seguro(item.get("monto"))

        return gastos_mes, totales_cat

    async def actualizar_panel_mensual(datos=None):
        if datos is None:
            datos = cache.get("datos") or []

        presupuesto_str = await page.client_storage.get_async("presupuesto_mensual")
        presupuesto = float(presupuesto_str) if presupuesto_str else 0.0

        gastos_mes, totales_cat = calcular_gastos_mes(datos)
        gastado_total = sum(totales_cat.values())
        restante = presupuesto - gastado_total

        if presupuesto > 0:
            presupuesto_input.value = str(int(presupuesto))
        
        presupuesto_text.value = formato_moneda(presupuesto)
        gastado_mes_text.value = formato_moneda(gastado_total)
        disponible_mes_text.value = formato_moneda(restante)
        disponible_mes_text.color = "red" if restante < 0 else "green"

        # Dibujar barras del mes
        resumen_barras_mes.controls.clear()
        ancho_maximo = 300
        if gastado_total > 0:
            for cat, total in sorted(totales_cat.items(), key=lambda x: -x[1]):
                if total == 0: continue
                porcentaje = (total / gastado_total) * 100
                ancho_relativo = max(2, (porcentaje / 100) * ancho_maximo)
                color_cat = COLOR_MAP.get(cat, "grey")

                resumen_barras_mes.controls.append(
                    ft.Column(
                        controls=[
                            ft.Row(
                                controls=[ft.Text(cat, size=13, weight="bold", expand=True), ft.Text(f"{formato_moneda(total)} ({porcentaje:.1f}%)", size=12, color="grey")],
                                alignment="spaceBetween",
                            ),
                            ft.Container(
                                content=ft.Stack(
                                    controls=[
                                        ft.Container(width=ancho_maximo, height=8, bgcolor="#424242", border_radius=4),
                                        ft.Container(width=ancho_relativo, height=8, bgcolor=color_cat, border_radius=4),
                                    ]
                                ),
                                width=ancho_maximo, height=8,
                            ),
                        ],
                        spacing=4,
                    )
                )
        page.update()

    async def guardar_presupuesto_mensual(e):
        try:
            valor = convertir_monto(presupuesto_input.value)
        except ValueError:
            show_snackbar("Presupuesto inválido", es_error=True)
            return

        await page.client_storage.set_async("presupuesto_mensual", str(valor))
        show_snackbar("Monto mensual guardado correctamente")
        await actualizar_panel_mensual(cache.get("datos") or [])

    btn_presupuesto.on_click = guardar_presupuesto_mensual

    vista_mensual = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(controls=[ft.Column(controls=[ft.Text("Mi Mes", size=22, weight="bold"), ft.Text(f"Estado del mes de {hoy_local().strftime('%B')}", size=12, color="grey")], spacing=2, expand=True), ft.IconButton("refresh", on_click=lambda e: cargar_mensual(force=True))], alignment="spaceBetween"),
                ft.Card(
                    content=ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Row(controls=[presupuesto_input, btn_presupuesto], vertical_alignment="center"),
                                ft.Divider(height=10),
                                ft.Row(controls=[ft.Column([ft.Text("Monto Base", size=11, color="grey"), presupuesto_text], expand=True), ft.Column([ft.Text("Gastado", size=11, color="grey"), gastado_mes_text], expand=True)]),
                                ft.Container(height=10),
                                ft.Container(content=ft.Column(controls=[ft.Text("Dinero disponible (Restante)", size=12, color="grey"), disponible_mes_text], spacing=2, horizontal_alignment="center"), padding=15, bgcolor="#252525", border_radius=12, width=400, alignment=ft.alignment.center),
                                ft.Divider(height=20),
                                ft.Text("Desglose del Mes", size=14, weight="bold"),
                                resumen_barras_mes
                            ],
                            spacing=12,
                        ),
                        padding=18,
                    )
                ),
            ],
            spacing=12,
        ),
        padding=20, visible=False,
    )

    async def cargar_mensual(force=False):
        try:
            datos = await obtener_datos(force=force)
            await actualizar_panel_mensual(datos)
        except Exception as ex:
            show_snackbar(f"Error: {ex}", es_error=True)

    # ==========================================
    # 4. NAVEGACIÓN INFERIOR
    # ==========================================
    async def mostrar_vista(idx):
        vista_formulario.visible = idx == 0
        vista_historial.visible = idx == 1
        vista_mensual.visible = idx == 2
        page.update()

        if idx == 1:
            await cargar_historial()
        elif idx == 2:
            await cargar_mensual()

    async def cambiar_pestana(e):
        indice = e.control.selected_index
        if indice is not None:
            await mostrar_vista(indice)

    page.navigation_bar = ft.NavigationBar(
        selected_index=0,
        on_change=cambiar_pestana,
        bgcolor="#1e1e1e",
        destinations=[
            ft.NavigationBarDestination(icon="add_card", label="Registrar"),
            ft.NavigationBarDestination(icon="list_alt", label="Historial"),
            ft.NavigationBarDestination(icon="account_balance_wallet", label="Mensual"),
        ],
    )

    body = ft.Container(
        content=ft.Column(controls=[vista_formulario, vista_historial, vista_mensual], scroll="auto", horizontal_alignment="center"),
        expand=True, width=480,
    )

    page.add(body)
    await actualizar_panel_mensual([])

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 7860))
    ft.app(target=main, host="0.0.0.0", port=puerto)
