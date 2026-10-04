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

COLOR_MAP = {
    "Compras": "blue",
    "Gastos Personales": "orange",
    "Salidas": "pink",
    "Transporte": "teal",
    "Universidad": "green",
    "Otros": "grey",
}

CATEGORIAS = list(COLOR_MAP.keys())
DIAS_CORTOS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


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


def formato_moneda_corto(valor):
    valor = float(valor or 0)
    if abs(valor) >= 1_000_000:
        return f"${valor / 1_000_000:.1f}M".replace(".0M", "M")
    if abs(valor) >= 1_000:
        return f"${valor / 1_000:.0f}k"
    return formato_moneda(valor)


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


def rango_semana(fecha=None):
    fecha = fecha or hoy_local()
    inicio = fecha - timedelta(days=fecha.weekday())
    fin = inicio + timedelta(days=6)
    return inicio, fin


def calcular_gastos_transporte(datos):
    """Calcula lo gastado en transporte hoy y en el mes actual."""
    hoy = hoy_local()
    mes_actual = hoy.month
    año_actual = hoy.year

    gastado_hoy = 0.0
    gastado_mes = 0.0

    if not datos:
        return gastado_hoy, gastado_mes

    for item in datos:
        cat = item.get("categoria", "")
        if cat != "Transporte":
            continue

        fecha_item = parsear_fecha(item.get("fecha"))
        if not fecha_item:
            continue

        monto = monto_seguro(item.get("monto"))

        if fecha_item.month == mes_actual and fecha_item.year == año_actual:
            gastado_mes += monto
            if fecha_item == hoy:
                gastado_hoy += monto

    return gastado_hoy, gastado_mes


async def main(page: ft.Page):
    page.title = "Mis Gastos"
    page.theme_mode = "dark"
    page.padding = 10
    page.horizontal_alignment = "center"

    http = requests.Session()
    http.headers.update({
        "Accept": "application/json",
        "User-Agent": "MisGastosFlet/2.0",
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
                    metodo,
                    URL_API,
                    params=params,
                    json=payload,
                    timeout=(API_CONNECT_TIMEOUT, API_READ_TIMEOUT),
                    allow_redirects=True,
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
        return await asyncio.to_thread(
            solicitar_json_sync,
            metodo,
            params=params,
            payload=payload,
        )

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

    async def obtener_presupuesto_api():
        resultado = await solicitar_json("GET", params={"action": "presupuesto_actual"})
        if not isinstance(resultado, dict):
            raise RuntimeError("Error al consultar el presupuesto.")
        return resultado

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
    # 1. FORMULARIO REGISTRO
    # ==========================================
    monto_input = ft.TextField(label="Monto ($)", keyboard_type="number", prefix=ft.Text("$ "), border_radius=12)
    categoria_dropdown = ft.Dropdown(label="Categoría", border_radius=12, options=[ft.dropdown.Option(cat) for cat in CATEGORIAS])
    descripcion_input = ft.TextField(label="Descripción", border_radius=12)
    chk_4x1000 = ft.Checkbox(label="Aplica impuesto 4x1000", value=False)
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

        impuesto = round(monto * 4 / 1000, 2) if chk_4x1000.value else 0
        payload = {
            "action": "agregar",
            "monto": monto,
            "categoria": categoria_dropdown.value,
            "descripcion": descripcion_input.value or "",
            "aplica4x1000": bool(chk_4x1000.value),
            "valor4x1000": impuesto,
        }

        btn_guardar.disabled = True
        page.update()
        guardado = await enviar_datos_api(payload, f"¡Gasto guardado! Impuesto: {formato_moneda(impuesto)}")
        if guardado:
            monto_input.value = ""
            categoria_dropdown.value = None
            descripcion_input.value = ""
            chk_4x1000.value = False
        btn_guardar.disabled = False
        page.update()

    btn_guardar.on_click = enviar_gasto

    vista_formulario = ft.Container(
        content=ft.Column(
            controls=[
                ft.Text("Nuevo Gasto", size=22, weight="bold"),
                ft.Text("Registra tus consumos diarios", size=13, color="grey"),
                ft.Divider(height=10, color="transparent"),
                monto_input, categoria_dropdown, descripcion_input, chk_4x1000, ft.Container(height=5), btn_guardar,
            ],
            spacing=12,
        ),
        padding=20,
        visible=True,
    )

    # ==========================================
    # 2. VISTA TRANSPORTE (MEJORADA)
    # ==========================================
    presupuesto_transporte_input = ft.TextField(
        label="Monto mensual ($)", keyboard_type="number", prefix=ft.Text("$ "), border_radius=12, expand=True
    )
    presupuesto_t_text = ft.Text("$0", size=16, weight="bold")
    gastado_hoy_t_text = ft.Text("$0", size=16, weight="bold", color="orange")
    gastado_mes_t_text = ft.Text("$0", size=16, weight="bold", color="red")
    restante_mes_t_text = ft.Text("$0", size=24, weight="bold", color="green")
    
    monto_transporte_custom = ft.TextField(
        label="Otro monto", keyboard_type="number", prefix=ft.Text("$ "), border_radius=12, expand=True
    )

    async def actualizar_panel_transporte(datos=None):
        if datos is None:
            datos = cache.get("datos") or []

        presupuesto_str = await page.client_storage.get_async("presupuesto_transporte")
        presupuesto = float(presupuesto_str) if presupuesto_str else 0.0

        gastado_hoy, gastado_mes = calcular_gastos_transporte(datos)
        restante = presupuesto - gastado_mes

        presupuesto_transporte_input.value = str(int(presupuesto)) if presupuesto > 0 else ""
        presupuesto_t_text.value = formato_moneda(presupuesto)
        gastado_hoy_t_text.value = formato_moneda(gastado_hoy)
        gastado_mes_t_text.value = formato_moneda(gastado_mes)
        restante_mes_t_text.value = formato_moneda(restante)
        restante_mes_t_text.color = "red" if restante < 0 else "green"
        page.update()

    async def guardar_presupuesto_transporte(e):
        try:
            valor = convertir_monto(presupuesto_transporte_input.value)
        except ValueError:
            show_snackbar("Monto inválido", es_error=True)
            return

        await page.client_storage.set_async("presupuesto_transporte", str(valor))
        show_snackbar("Presupuesto mensual de transporte guardado")
        await actualizar_panel_transporte(cache.get("datos") or [])

    async def registrar_transporte(monto):
        payload = {
            "action": "agregar", "monto": float(monto), "categoria": "Transporte",
            "descripcion": "Transporte diario", "aplica4x1000": False, "valor4x1000": 0.0,
        }
        guardado = await enviar_datos_api(payload, f"✓ Transporte de {formato_moneda(monto)} registrado")
        if guardado:
            try:
                datos = await obtener_datos(force=True)
                await actualizar_panel_transporte(datos)
            except Exception:
                pass

    async def registrar_transporte_custom(e):
        if not monto_transporte_custom.value:
            show_snackbar("Ingresa un monto", es_error=True)
            return
        try:
            monto = convertir_monto(monto_transporte_custom.value)
        except ValueError:
            show_snackbar("Monto inválido", es_error=True)
            return
        
        await registrar_transporte(monto)
        monto_transporte_custom.value = ""
        page.update()

    def crear_tarjeta_transporte(monto):
        async def click_transporte(e):
            await registrar_transporte(monto)
        return ft.Container(
            content=ft.Column(
                controls=[ft.Icon("directions_bus", size=28, color="teal"), ft.Text(formato_moneda(monto), size=18, weight="bold")],
                alignment="center", horizontal_alignment="center",
            ),
            bgcolor="#303030", border_radius=16, padding=15, ink=True, on_click=click_transporte, width=145, height=100,
        )

    vista_transporte = ft.Container(
        content=ft.Column(
            controls=[
                ft.Text("Mi Transporte", size=22, weight="bold"),
                ft.Card(
                    content=ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Row(
                                    controls=[presupuesto_transporte_input, ft.ElevatedButton("Guardar", icon="save", on_click=guardar_presupuesto_transporte)],
                                    vertical_alignment="center",
                                ),
                                ft.Divider(height=8),
                                ft.Row(
                                    controls=[
                                        ft.Column([ft.Text("Mensual", size=10, color="grey"), presupuesto_t_text], expand=True),
                                        ft.Column([ft.Text("Gastado Mes", size=10, color="grey"), gastado_mes_t_text], expand=True),
                                        ft.Column([ft.Text("Gastado Hoy", size=10, color="grey"), gastado_hoy_t_text], expand=True),
                                    ]
                                ),
                                ft.Container(height=4),
                                ft.Container(
                                    content=ft.Column(
                                        controls=[ft.Text("Saldo Restante (Mes)", size=12, color="grey"), restante_mes_t_text]
                                    ),
                                    padding=10, bgcolor="#252525", border_radius=12,
                                )
                            ]
                        ),
                        padding=15
                    )
                ),
                ft.Container(height=10),
                ft.Text("Agregar Gasto Manual", size=16, weight="bold"),
                ft.Row(
                    controls=[monto_transporte_custom, ft.ElevatedButton("Agregar", icon="add", on_click=registrar_transporte_custom)],
                    vertical_alignment="center"
                ),
                ft.Container(height=10),
                ft.Text("Acceso Rápido", size=16, weight="bold"),
                ft.Row(
                    controls=[
                        crear_tarjeta_transporte(3000), crear_tarjeta_transporte(4000),
                        crear_tarjeta_transporte(5000), crear_tarjeta_transporte(6000),
                    ],
                    alignment="center", wrap=True, spacing=12, run_spacing=12,
                ),
            ],
            horizontal_alignment="center",
        ),
        padding=20,
        visible=False,
    )

    # ==========================================
    # 3. REPORTES Y PRESUPUESTO SEMANAL
    # ==========================================
    resumen_barras = ft.Column(spacing=12)
    lista_gastos = ft.Column(spacing=8)
    total_text = ft.Text("$0", size=26, weight="bold", color="green")

    presupuesto_input = ft.TextField(label="Presupuesto semanal ($)", keyboard_type="number", prefix=ft.Text("$ "), border_radius=12, expand=True)
    btn_presupuesto = ft.ElevatedButton("Guardar", icon="savings")

    presupuesto_text = ft.Text("$0", size=18, weight="bold")
    gastado_semana_text = ft.Text("$0", size=18, weight="bold")
    disponible_semana_text = ft.Text("$0", size=18, weight="bold")
    disponible_dia_text = ft.Text("$0", size=28, weight="bold", color="green")
    disponible_dia_subtext = ft.Text("Configura tu presupuesto semanal", size=11, color="grey")
    progreso_semana = ft.ProgressBar(value=0, height=8)
    grafica_semana = ft.Row(alignment="spaceBetween", vertical_alignment="end")
    semana_rango_text = ft.Text("", size=11, color="grey")

    estado_presupuesto = {"valor": 0.0}

    def filtrar_semana(datos):
        hoy = hoy_local()
        inicio, fin = rango_semana(hoy)
        gastos = []
        por_dia = {inicio + timedelta(days=i): 0.0 for i in range(7)}

        for item in datos:
            fecha_item = parsear_fecha(item.get("fecha"))
            if fecha_item is None or not (inicio <= fecha_item <= fin):
                continue
            monto = monto_seguro(item.get("monto"))
            gastos.append(item)
            por_dia[fecha_item] += monto
        return inicio, fin, gastos, por_dia

    def construir_grafica_semanal(por_dia, presupuesto):
        grafica_semana.controls.clear()
        valores = list(por_dia.values())
        referencia_diaria = presupuesto / 7 if presupuesto > 0 else 0
        maximo = max(valores + [referencia_diaria, 1])
        alto_max = 105

        for indice, (fecha_dia, monto) in enumerate(por_dia.items()):
            alto = 3 if monto <= 0 else max(8, (monto / maximo) * alto_max)
            es_hoy = fecha_dia == hoy_local()

            grafica_semana.controls.append(
                ft.Column(
                    controls=[
                        ft.Text(formato_moneda_corto(monto), size=9, color="white" if es_hoy else "grey"),
                        ft.Container(
                            content=ft.Column(
                                controls=[
                                    ft.Container(expand=True),
                                    ft.Container(width=24, height=alto, bgcolor="blue" if not es_hoy else "green", border_radius=6),
                                ],
                                spacing=0,
                            ),
                            height=alto_max + 5,
                        ),
                        ft.Text(DIAS_CORTOS[indice], size=10, weight="bold" if es_hoy else None),
                    ],
                    spacing=3, horizontal_alignment="center",
                )
            )

    def actualizar_panel_semana(datos):
        presupuesto = estado_presupuesto["valor"]
        inicio, fin, gastos_semana, por_dia = filtrar_semana(datos)
        gastado = sum(monto_seguro(item.get("monto")) for item in gastos_semana)
        restante = presupuesto - gastado
        hoy = hoy_local()
        dias_restantes = max(1, (fin - hoy).days + 1)
        disponible_por_dia = max(restante, 0) / dias_restantes if presupuesto > 0 else 0

        presupuesto_text.value = formato_moneda(presupuesto)
        gastado_semana_text.value = formato_moneda(gastado)
        disponible_semana_text.value = formato_moneda(restante)
        disponible_semana_text.color = "red" if restante < 0 else "green"
        disponible_dia_text.value = formato_moneda(disponible_por_dia)
        disponible_dia_text.color = "red" if restante < 0 else "green"

        if presupuesto <= 0:
            disponible_dia_subtext.value = "Configura tu presupuesto semanal"
            progreso_semana.value = 0
        elif restante < 0:
            disponible_dia_subtext.value = f"Te pasaste {formato_moneda(abs(restante))} del presupuesto"
            progreso_semana.value = 1
        else:
            disponible_dia_subtext.value = (f"Promedio disponible para {dias_restantes} día" + ("" if dias_restantes == 1 else "s") + " (incluyendo hoy)")
            progreso_semana.value = min(gastado / presupuesto, 1)

        semana_rango_text.value = f"Semana: {inicio.strftime('%d/%m')} - {fin.strftime('%d/%m/%Y')}"
        construir_grafica_semanal(por_dia, presupuesto)

    async def guardar_presupuesto(e):
        try:
            valor = convertir_monto(presupuesto_input.value)
        except ValueError:
            show_snackbar("Presupuesto inválido", es_error=True)
            return

        if api_write_lock.locked():
            show_snackbar("Procesando...", es_error=True)
            return

        async with api_write_lock:
            btn_presupuesto.disabled = True
            page.update()
            try:
                resultado = await solicitar_json("POST", payload={"action": "guardar_presupuesto", "presupuesto": valor})
                if not isinstance(resultado, dict) or resultado.get("status") != "success":
                    raise RuntimeError(resultado.get("message", "No se pudo guardar"))

                estado_presupuesto["valor"] = valor
                datos = cache["datos"] or await obtener_datos()
                actualizar_panel_semana(datos)
                show_snackbar(resultado.get("message", "Presupuesto semanal guardado"))
            except Exception as ex:
                show_snackbar(f"Error: {ex}", es_error=True)
            finally:
                btn_presupuesto.disabled = False
                page.update()

    btn_presupuesto.on_click = guardar_presupuesto

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
        resumen_barras.controls.clear()

        if not datos:
            total_text.value = "$0"
            actualizar_panel_semana([])
            return

        totales = defaultdict(float)
        gran_total = 0.0
        gastos_por_fecha = defaultdict(list)

        for item in datos:
            cat = item.get("categoria", "Otros") or "Otros"
            monto = monto_seguro(item.get("monto"))
            fecha = parsear_fecha(item.get("fecha"))
            fecha_key = fecha.isoformat() if fecha else str(item.get("fecha", "Sin fecha")).split("T")[0]

            totales[cat] += monto
            gran_total += monto
            gastos_por_fecha[fecha_key].append(item)

        ancho_maximo = 300
        if gran_total > 0:
            for cat, total in sorted(totales.items(), key=lambda x: -x[1]):
                porcentaje = (total / gran_total) * 100
                ancho_relativo = max(2, (porcentaje / 100) * ancho_maximo)
                color_cat = COLOR_MAP.get(cat, "grey")

                resumen_barras.controls.append(
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

        def clave_orden(fecha_texto):
            try:
                return (1, date.fromisoformat(fecha_texto))
            except ValueError:
                return (0, date.min)

        for fecha_key in sorted(gastos_por_fecha.keys(), key=clave_orden, reverse=True):
            lista_gastos.controls.append(crear_grupo_fecha(fecha_key, gastos_por_fecha[fecha_key]))

        total_text.value = formato_moneda(gran_total)
        actualizar_panel_semana(datos)

    async def cargar_historial(e=None, force=False):
        total_text.value = "Cargando..."
        page.update()
        try:
            datos = await obtener_datos(force=force)
            renderizar_historial(datos)
        except Exception as ex:
            total_text.value = "Error"
            show_snackbar(f"Error: {ex}", es_error=True)
        page.update()

    async def refrescar_historial(e):
        await cargar_historial(force=True)

    tarjeta_presupuesto = ft.Card(
        content=ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(controls=[ft.Column(controls=[ft.Text("Presupuesto semanal", size=17, weight="bold"), semana_rango_text], spacing=2, expand=True), ft.Icon("account_balance_wallet", color="green")]),
                    ft.Row(controls=[presupuesto_input, btn_presupuesto], vertical_alignment="center"),
                    ft.Divider(height=8),
                    ft.Row(controls=[ft.Column([ft.Text("Presupuesto", size=10, color="grey"), presupuesto_text], expand=True), ft.Column([ft.Text("Gastado", size=10, color="grey"), gastado_semana_text], expand=True), ft.Column([ft.Text("Disponible", size=10, color="grey"), disponible_semana_text], expand=True)], spacing=8),
                    progreso_semana, ft.Container(height=4),
                    ft.Container(content=ft.Column(controls=[ft.Text("Puedes gastar por día", size=11, color="grey"), disponible_dia_text, disponible_dia_subtext], spacing=2), padding=14, bgcolor="#252525", border_radius=12),
                    ft.Text("Gasto de esta semana", size=13, weight="bold"), grafica_semana,
                ],
                spacing=12,
            ),
            padding=18,
        )
    )

    vista_historial = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(controls=[ft.Text("Reportes", size=22, weight="bold"), ft.IconButton("refresh", on_click=refrescar_historial)], alignment="spaceBetween"),
                ft.Card(content=ft.Container(content=ft.Column(controls=[ft.Text("Gasto Total", size=12, color="grey"), total_text, ft.Divider(height=10), resumen_barras]), padding=20)),
                ft.Container(height=10), ft.Text("Historial por Día", size=16, weight="bold", color="white"), lista_gastos,
            ]
        ),
        padding=20, visible=False,
    )

    async def cargar_presupuesto(e=None, force=False):
        disponible_dia_subtext.value = "Cargando presupuesto..."
        page.update()
        try:
            datos = await obtener_datos(force=force)
        except Exception as ex:
            disponible_dia_subtext.value = "No se pudieron cargar los gastos"
            show_snackbar(f"Error: {ex}", es_error=True)
            page.update()
            return

        try:
            info_presupuesto = await obtener_presupuesto_api()
            presupuesto = monto_seguro(info_presupuesto.get("presupuesto", 0))
            estado_presupuesto["valor"] = presupuesto

            if presupuesto > 0:
                presupuesto_input.value = str(int(presupuesto))
            else:
                presupuesto_input.value = ""
            actualizar_panel_semana(datos)
        except Exception as ex:
            actualizar_panel_semana(datos)
            disponible_dia_subtext.value = "No se pudo leer el presupuesto"
        page.update()

    async def refrescar_presupuesto(e):
        await cargar_presupuesto(force=True)

    vista_presupuesto = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(controls=[ft.Column(controls=[ft.Text("Presupuesto", size=22, weight="bold"), ft.Text("Controla cuánto puedes gastar esta semana", size=12, color="grey")], spacing=2, expand=True), ft.IconButton("refresh", on_click=refrescar_presupuesto)], alignment="spaceBetween"),
                tarjeta_presupuesto,
            ],
            spacing=12,
        ),
        padding=20, visible=False,
    )

    # ==========================================
    # 4. NAVEGACIÓN INFERIOR
    # ==========================================
    async def mostrar_vista(idx):
        vista_formulario.visible = idx == 0
        vista_transporte.visible = idx == 1
        vista_historial.visible = idx == 2
        vista_presupuesto.visible = idx == 3
        page.update()

        if idx == 1:
            try:
                datos = await obtener_datos()
                await actualizar_panel_transporte(datos)
            except Exception:
                pass
        elif idx == 2:
            await cargar_historial()
        elif idx == 3:
            await cargar_presupuesto()

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
            ft.NavigationBarDestination(icon="directions_bus", label="Transporte"),
            ft.NavigationBarDestination(icon="bar_chart", label="Reportes"),
            ft.NavigationBarDestination(icon="account_balance_wallet", label="Presupuesto"),
        ],
    )

    body = ft.Container(
        content=ft.Column(
            controls=[vista_formulario, vista_transporte, vista_historial, vista_presupuesto],
            scroll="auto", horizontal_alignment="center",
        ),
        expand=True, width=480,
    )

    page.add(body)
    actualizar_panel_semana([])
    page.update()

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 7860))
    ft.app(target=main, host="0.0.0.0", port=puerto)
