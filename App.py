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
    "https://script.google.com/macros/s/AKfycbwfeW7j1DSik9ZiALV_jr1c35Ffbe4orCRaZr276Nfb32-6ja3JIWV0BFyArE0aIxT3/exec",
)
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "America/Bogota")
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "20"))
API_CONNECT_TIMEOUT = float(os.environ.get("API_CONNECT_TIMEOUT", "8"))
API_READ_TIMEOUT = float(os.environ.get("API_READ_TIMEOUT", "35"))
API_REINTENTOS = int(os.environ.get("API_REINTENTOS", "2"))

COLOR_MAP = {
    "Compras": "blue",
    "Iglesia": "purple",
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
        # Colombia no usa horario de verano; este fallback evita que la app falle
        # si la imagen de despliegue no trae la base de zonas horarias.
        return timezone(timedelta(hours=-5))


TZ = obtener_zona_horaria()


def hoy_local():
    return datetime.now(TZ).date()


def formato_moneda(valor):
    """Formato simple para COP: $500.000."""
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
    """
    Convierte entradas frecuentes en Colombia:
    500000, 500.000, 500,000, 12500,50 y 12500.50.
    """
    if texto is None:
        raise ValueError("Monto vacío")

    valor = str(texto).strip().replace("$", "").replace(" ", "")
    if not valor:
        raise ValueError("Monto vacío")

    if "," in valor and "." in valor:
        # El último separador se interpreta como decimal.
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
    """Devuelve date cuando la fecha del API es reconocible."""
    if valor in (None, ""):
        return None

    texto = str(valor).strip()
    if not texto:
        return None

    # ISO de Apps Script / JSON: 2026-09-10 o 2026-09-10T13:00:00.000Z
    try:
        iso = texto.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone(TZ)
        return dt.date()
    except ValueError:
        pass

    # Formatos de respaldo frecuentes.
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


async def main(page: ft.Page):
    page.title = "Mis Gastos"
    page.theme_mode = "dark"
    page.padding = 10
    page.horizontal_alignment = "center"

    # Una sesión por usuario reutiliza conexiones HTTP. Las solicitudes de la
    # pestaña Presupuesto se hacen de forma secuencial para evitar dos GET
    # simultáneos contra el mismo Web App de Google Apps Script.
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
        """
        Llama al Web App de Apps Script con timeout amplio y reintentos.

        Google Apps Script puede tardar varios segundos cuando el despliegue
        estaba inactivo. La versión anterior usaba 12 s y convertía cualquier
        HTTP 4xx/5xx en el mensaje genérico "No se pudo actualizar...".
        """
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
                    raise RuntimeError(
                        "El Web App de Google no permite acceso. "
                        "Despliega Apps Script como 'Ejecutar como: yo' y "
                        "'Quién tiene acceso: cualquier persona'."
                    )

                if respuesta.status_code >= 400:
                    detalle = respuesta.text.strip().replace("\n", " ")[:180]
                    raise RuntimeError(
                        f"Google Apps Script respondió HTTP {respuesta.status_code}"
                        + (f": {detalle}" if detalle else "")
                    )

                try:
                    return respuesta.json()
                except ValueError as ex:
                    inicio = respuesta.text.strip().replace("\n", " ")[:180]
                    if "<html" in respuesta.text.lower():
                        raise RuntimeError(
                            "Google devolvió una página HTML en vez de JSON. "
                            "Revisa que el despliegue del Web App sea público y que uses la URL /exec."
                        ) from ex
                    raise RuntimeError(
                        "El servidor no devolvió JSON válido"
                        + (f": {inicio}" if inicio else "")
                    ) from ex

            except (requests.Timeout, requests.ConnectionError) as ex:
                ultimo_error = ex
                if intento < API_REINTENTOS:
                    time.sleep(0.8 * (intento + 1))
                    continue
                raise RuntimeError(
                    "No fue posible conectar con Google Apps Script después de varios intentos. "
                    "Comprueba Internet y el despliegue del Web App."
                ) from ex
            except requests.RequestException as ex:
                ultimo_error = ex
                if intento < API_REINTENTOS:
                    time.sleep(0.8 * (intento + 1))
                    continue
                raise RuntimeError(f"Error de conexión con Google Apps Script: {ex}") from ex

        raise RuntimeError(f"No se pudo completar la solicitud: {ultimo_error}")

    async def solicitar_json(metodo, *, params=None, payload=None):
        return await asyncio.to_thread(
            solicitar_json_sync,
            metodo,
            params=params,
            payload=payload,
        )

    async def obtener_datos(force=False):
        """Obtiene los gastos usando caché corta para evitar GET repetidos."""
        ahora = time.monotonic()
        if (
            not force
            and cache["datos"] is not None
            and (ahora - cache["actualizado"]) < CACHE_TTL_SECONDS
        ):
            return cache["datos"]

        async with cache_lock:
            ahora = time.monotonic()
            if (
                not force
                and cache["datos"] is not None
                and (ahora - cache["actualizado"]) < CACHE_TTL_SECONDS
            ):
                return cache["datos"]

            datos = await solicitar_json("GET")
            if not isinstance(datos, list):
                if isinstance(datos, dict) and datos.get("status") == "error":
                    raise RuntimeError(datos.get("message", "Error al consultar los gastos"))
                raise RuntimeError(
                    "El GET principal del API debe devolver una lista de gastos. "
                    "Verifica que el Apps Script actualizado esté desplegado."
                )

            cache["datos"] = datos
            cache["actualizado"] = time.monotonic()
            return datos

    async def obtener_presupuesto_api():
        """Obtiene desde Google Sheets el presupuesto de la semana actual."""
        resultado = await solicitar_json(
            "GET",
            params={"action": "presupuesto_actual"},
        )

        if not isinstance(resultado, dict):
            raise RuntimeError(
                "El endpoint presupuesto_actual no está activo en Apps Script. "
                "Vuelve a desplegar la última versión del código."
            )
        if resultado.get("status") == "error":
            raise RuntimeError(resultado.get("message", "No se pudo consultar el presupuesto"))

        # Aceptamos tanto {status, presupuesto} como una futura respuesta que
        # añada más campos, siempre que presupuesto exista o sea 0.
        if "presupuesto" not in resultado:
            raise RuntimeError(
                "La respuesta del servidor no contiene el campo 'presupuesto'. "
                "Actualiza el código de Apps Script y vuelve a desplegarlo."
            )
        return resultado

    async def enviar_datos_api(payload, mensaje_exito):
        if api_write_lock.locked():
            show_snackbar("Ya se está guardando otra operación", es_error=True)
            return False

        async with api_write_lock:
            btn_guardar.disabled = True
            page.update()

            try:
                resultado = await solicitar_json("POST", payload=payload)

                if not isinstance(resultado, dict):
                    raise RuntimeError("El servidor devolvió una respuesta inválida")
                if resultado.get("status") != "success":
                    raise RuntimeError(resultado.get("message", "El servidor rechazó la operación"))

                cache["datos"] = None
                cache["actualizado"] = 0.0
                show_snackbar(resultado.get("message", mensaje_exito))
                return True
            except Exception as ex:
                show_snackbar(f"Error al guardar: {ex}", es_error=True)
                return False
            finally:
                btn_guardar.disabled = False
                page.update()

    # ==========================================
    # 1. FORMULARIO REGISTRO
    # ==========================================
    monto_input = ft.TextField(
        label="Monto ($)",
        keyboard_type="number",
        prefix=ft.Text("$ "),
        border_radius=12,
    )

    categoria_dropdown = ft.Dropdown(
        label="Categoría",
        border_radius=12,
        options=[ft.dropdown.Option(cat) for cat in CATEGORIAS],
    )

    descripcion_input = ft.TextField(label="Descripción", border_radius=12)
    chk_4x1000 = ft.Checkbox(label="Aplica impuesto 4x1000", value=False)
    btn_guardar = ft.ElevatedButton(
        "Guardar Gasto",
        icon="save",
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=12),
            padding=18,
        ),
        width=350,
    )

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

        guardado = await enviar_datos_api(
            payload,
            f"¡Gasto guardado! Impuesto: {formato_moneda(impuesto)}",
        )
        if guardado:
            monto_input.value = ""
            categoria_dropdown.value = None
            descripcion_input.value = ""
            chk_4x1000.value = False
            page.update()

    btn_guardar.on_click = enviar_gasto

    vista_formulario = ft.Container(
        content=ft.Column(
            controls=[
                ft.Text("Nuevo Gasto", size=22, weight="bold"),
                ft.Text("Registra tus consumos diarios", size=13, color="grey"),
                ft.Divider(height=10, color="transparent"),
                monto_input,
                categoria_dropdown,
                descripcion_input,
                chk_4x1000,
                ft.Container(height=5),
                btn_guardar,
            ],
            spacing=12,
        ),
        padding=20,
        visible=True,
    )

    # ==========================================
    # 2. VISTA TRANSPORTE
    # ==========================================
    async def registrar_transporte(monto):
        payload = {
            "action": "agregar",
            "monto": float(monto),
            "categoria": "Transporte",
            "descripcion": "Transporte rápido",
            "aplica4x1000": False,
            "valor4x1000": 0.0,
        }
        await enviar_datos_api(
            payload,
            f"✓ Transporte de {formato_moneda(monto)} registrado",
        )

    def crear_tarjeta_transporte(monto):
        async def click_transporte(e):
            await registrar_transporte(monto)

        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Icon("directions_bus", size=28, color="teal"),
                    ft.Text(formato_moneda(monto), size=18, weight="bold"),
                ],
                alignment="center",
                horizontal_alignment="center",
            ),
            bgcolor="#303030",
            border_radius=16,
            padding=15,
            ink=True,
            on_click=click_transporte,
            width=145,
            height=100,
        )

    vista_transporte = ft.Container(
        content=ft.Column(
            controls=[
                ft.Text("Acceso Rápido", size=22, weight="bold"),
                ft.Text("Selecciona una tarifa recurrente", size=13, color="grey"),
                ft.Container(height=15),
                ft.Row(
                    controls=[
                        crear_tarjeta_transporte(3000),
                        crear_tarjeta_transporte(4000),
                        crear_tarjeta_transporte(5000),
                        crear_tarjeta_transporte(6000),
                    ],
                    alignment="center",
                    wrap=True,
                    spacing=12,
                    run_spacing=12,
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

    presupuesto_input = ft.TextField(
        label="Presupuesto semanal ($)",
        keyboard_type="number",
        prefix=ft.Text("$ "),
        border_radius=12,
        expand=True,
    )
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
                        ft.Text(
                            formato_moneda_corto(monto),
                            size=9,
                            color="white" if es_hoy else "grey",
                        ),
                        ft.Container(
                            content=ft.Column(
                                controls=[
                                    ft.Container(expand=True),
                                    ft.Container(
                                        width=24,
                                        height=alto,
                                        bgcolor="blue" if not es_hoy else "green",
                                        border_radius=6,
                                    ),
                                ],
                                spacing=0,
                            ),
                            height=alto_max + 5,
                        ),
                        ft.Text(
                            DIAS_CORTOS[indice],
                            size=10,
                            weight="bold" if es_hoy else None,
                        ),
                    ],
                    spacing=3,
                    horizontal_alignment="center",
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
            disponible_dia_subtext.value = (
                f"Promedio disponible para {dias_restantes} día"
                + ("" if dias_restantes == 1 else "s")
                + " (incluyendo hoy)"
            )
            progreso_semana.value = min(gastado / presupuesto, 1)

        semana_rango_text.value = (
            f"Semana: {inicio.strftime('%d/%m')} - {fin.strftime('%d/%m/%Y')}"
        )
        construir_grafica_semanal(por_dia, presupuesto)

    async def guardar_presupuesto(e):
        try:
            valor = convertir_monto(presupuesto_input.value)
        except ValueError:
            show_snackbar("Presupuesto inválido", es_error=True)
            return

        if api_write_lock.locked():
            show_snackbar("Ya se está guardando otra operación", es_error=True)
            return

        async with api_write_lock:
            btn_presupuesto.disabled = True
            page.update()

            try:
                resultado = await solicitar_json(
                    "POST",
                    payload={
                        "action": "guardar_presupuesto",
                        "presupuesto": valor,
                    },
                )

                if not isinstance(resultado, dict):
                    raise RuntimeError("El API devolvió una respuesta inválida")
                if resultado.get("status") != "success":
                    raise RuntimeError(
                        resultado.get("message", "No se pudo guardar el presupuesto")
                    )

                estado_presupuesto["valor"] = valor

                datos = cache["datos"]
                if datos is None:
                    datos = await obtener_datos()

                actualizar_panel_semana(datos)
                show_snackbar(resultado.get("message", "Presupuesto semanal guardado"))

            except Exception as ex:
                show_snackbar(f"Error al guardar presupuesto: {ex}", es_error=True)
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
                    ft.Column(
                        controls=[
                            ft.Text(cat, weight="bold", size=14),
                            ft.Text(desc if desc else "Sin descripción", size=11, color="grey"),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.Text(formato_moneda(monto), weight="bold", size=14),
                ],
                alignment="spaceBetween",
            ),
            padding=12,
            bgcolor="#303030",
            border_radius=12,
        )

    def crear_grupo_fecha(fecha, items_dia):
        """
        Construcción perezosa: no crea todos los controles de movimientos hasta
        que el usuario abre ese día. Con historiales grandes reduce bastante el render.
        """
        total_dia = sum(monto_seguro(i.get("monto")) for i in items_dia)
        col_movimientos = ft.Column(controls=[], visible=False, spacing=8)
        icono_toggle = ft.Icon("keyboard_arrow_down", color="grey")
        estado = {"renderizado": False}

        def toggle_vis(e):
            if not estado["renderizado"]:
                col_movimientos.controls.extend(crear_control_gasto(item) for item in items_dia)
                estado["renderizado"] = True

            col_movimientos.visible = not col_movimientos.visible
            icono_toggle.name = (
                "keyboard_arrow_up" if col_movimientos.visible else "keyboard_arrow_down"
            )
            page.update()

        header = ft.Container(
            content=ft.Row(
                controls=[
                    ft.Text(str(fecha), weight="bold", size=15),
                    ft.Row(
                        controls=[
                            ft.Text(formato_moneda(total_dia), weight="bold", size=14, color="grey"),
                            icono_toggle,
                        ],
                        spacing=5,
                    ),
                ],
                alignment="spaceBetween",
            ),
            ink=True,
            on_click=toggle_vis,
            padding=ft.padding.symmetric(horizontal=10, vertical=15),
            border_radius=8,
        )

        return ft.Container(
            content=ft.Column(controls=[header, col_movimientos], spacing=0),
            bgcolor="#1e1e1e",
            border_radius=10,
            margin=ft.margin.only(bottom=8),
        )

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

        # Barras de categorías.
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
                                controls=[
                                    ft.Text(cat, size=13, weight="bold", expand=True),
                                    ft.Text(
                                        f"{formato_moneda(total)} ({porcentaje:.1f}%)",
                                        size=12,
                                        color="grey",
                                    ),
                                ],
                                alignment="spaceBetween",
                            ),
                            ft.Container(
                                content=ft.Stack(
                                    controls=[
                                        ft.Container(
                                            width=ancho_maximo,
                                            height=8,
                                            bgcolor="#424242",
                                            border_radius=4,
                                        ),
                                        ft.Container(
                                            width=ancho_relativo,
                                            height=8,
                                            bgcolor=color_cat,
                                            border_radius=4,
                                        ),
                                    ]
                                ),
                                width=ancho_maximo,
                                height=8,
                            ),
                        ],
                        spacing=4,
                    )
                )

        # Orden real por fecha cuando es ISO; los valores no reconocidos van al final.
        def clave_orden(fecha_texto):
            try:
                return (1, date.fromisoformat(fecha_texto))
            except ValueError:
                return (0, date.min)

        for fecha_key in sorted(gastos_por_fecha.keys(), key=clave_orden, reverse=True):
            lista_gastos.controls.append(
                crear_grupo_fecha(fecha_key, gastos_por_fecha[fecha_key])
            )

        total_text.value = formato_moneda(gran_total)
        actualizar_panel_semana(datos)

    async def cargar_historial(e=None, force=False):
        total_text.value = "Cargando..."
        page.update()

        try:
            datos = await obtener_datos(force=force)
            renderizar_historial(datos)
        except requests.RequestException:
            total_text.value = "Error"
            show_snackbar("No se pudo consultar la hoja de gastos", es_error=True)
        except Exception as ex:
            total_text.value = "Error"
            show_snackbar(f"Error al cargar reportes: {ex}", es_error=True)

        page.update()

    async def refrescar_historial(e):
        await cargar_historial(force=True)

    tarjeta_presupuesto = ft.Card(
        content=ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text("Presupuesto semanal", size=17, weight="bold"),
                                    semana_rango_text,
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            ft.Icon("account_balance_wallet", color="green"),
                        ]
                    ),
                    ft.Row(
                        controls=[presupuesto_input, btn_presupuesto],
                        vertical_alignment="center",
                    ),
                    ft.Divider(height=8),
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[ft.Text("Presupuesto", size=10, color="grey"), presupuesto_text],
                                expand=True,
                            ),
                            ft.Column(
                                controls=[ft.Text("Gastado", size=10, color="grey"), gastado_semana_text],
                                expand=True,
                            ),
                            ft.Column(
                                controls=[ft.Text("Disponible", size=10, color="grey"), disponible_semana_text],
                                expand=True,
                            ),
                        ],
                        spacing=8,
                    ),
                    progreso_semana,
                    ft.Container(height=4),
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Text("Puedes gastar por día", size=11, color="grey"),
                                disponible_dia_text,
                                disponible_dia_subtext,
                            ],
                            spacing=2,
                        ),
                        padding=14,
                        bgcolor="#252525",
                        border_radius=12,
                    ),
                    ft.Text("Gasto de esta semana", size=13, weight="bold"),
                    grafica_semana,
                ],
                spacing=12,
            ),
            padding=18,
        )
    )

    vista_historial = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Text("Reportes", size=22, weight="bold"),
                        ft.IconButton("refresh", on_click=refrescar_historial),
                    ],
                    alignment="spaceBetween",
                ),
                ft.Card(
                    content=ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Text("Gasto Total", size=12, color="grey"),
                                total_text,
                                ft.Divider(height=10),
                                resumen_barras,
                            ]
                        ),
                        padding=20,
                    )
                ),
                ft.Container(height=10),
                ft.Text("Historial por Día", size=16, weight="bold", color="white"),
                lista_gastos,
            ]
        ),
        padding=20,
        visible=False,
    )

    async def cargar_presupuesto(e=None, force=False):
        disponible_dia_subtext.value = "Cargando presupuesto y gastos..."
        page.update()

        # Importante: no usamos asyncio.gather aquí. La versión anterior hacía
        # dos solicitudes simultáneas con la misma Session de requests. Ahora
        # se consultan secuencialmente y cada una tiene reintentos.
        try:
            datos = await obtener_datos(force=force)
        except Exception as ex:
            disponible_dia_subtext.value = "No se pudieron cargar los gastos"
            show_snackbar(f"Error al consultar gastos: {ex}", es_error=True)
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
            # Los gastos ya se cargaron correctamente: no rompemos toda la
            # pestaña. Conservamos el valor visible anterior y mostramos el
            # motivo real para saber si falta desplegar/autorizar Apps Script.
            actualizar_panel_semana(datos)
            disponible_dia_subtext.value = "No se pudo leer el presupuesto guardado"
            show_snackbar(f"Presupuesto: {ex}", es_error=True)

        page.update()

    async def refrescar_presupuesto(e):
        await cargar_presupuesto(force=True)

    vista_presupuesto = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[
                        ft.Column(
                            controls=[
                                ft.Text("Presupuesto", size=22, weight="bold"),
                                ft.Text(
                                    "Controla cuánto puedes gastar esta semana",
                                    size=12,
                                    color="grey",
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        ft.IconButton("refresh", on_click=refrescar_presupuesto),
                    ],
                    alignment="spaceBetween",
                ),
                tarjeta_presupuesto,
            ],
            spacing=12,
        ),
        padding=20,
        visible=False,
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

        if idx == 2:
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
            controls=[
                vista_formulario,
                vista_transporte,
                vista_historial,
                vista_presupuesto,
            ],
            scroll="auto",
            horizontal_alignment="center",
        ),
        expand=True,
        width=480,
    )

    page.add(body)

    # El presupuesto ya no se guarda localmente. Se consulta en Google Sheets
    # al abrir la pestaña Presupuesto, para compartir el mismo valor entre dispositivos.
    actualizar_panel_semana([])
    page.update()


if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 7860))
    ft.app(target=main, host="0.0.0.0", port=puerto)
