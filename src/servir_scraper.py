"""
servir_scraper.py
Scraper para https://app.servir.gob.pe/DifusionOfertasExterno/faces/consultas/ofertas_laborales.xhtml

La web de SERVIR usa JavaServer Faces (JSF) con PrimeFaces.
Los datos NO están en una <table> sino en paneles repetidos (ui:repeat / p:panel).
Cada oferta tiene botón "Ver más" que revela los detalles completos.

Usa Playwright para renderizar el JavaScript y navegar la paginación.

Uso desde el directorio raíz del proyecto:
    python -m src.servir_scraper                           # Lima, 2 páginas
    python -m src.servir_scraper --departamento AREQUIPA
    python -m src.servir_scraper --departamento TODOS      # todas las regiones
    python -m src.servir_scraper --paginas 5
    python -m src.servir_scraper --output output/mis_convocatorias.csv
"""

import os
import re
import csv
import sys
import time
import argparse
from typing import List, Dict, Any, Optional

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    print("[ERROR] Playwright no está instalado. Ejecuta:\n  pip install playwright && playwright install chromium", file=sys.stderr)
    sys.exit(1)

from bs4 import BeautifulSoup

# -----------------------------------------------------------------------
# Constantes
# -----------------------------------------------------------------------
SERVIR_URL = "https://app.servir.gob.pe/DifusionOfertasExterno/faces/consultas/ofertas_laborales.xhtml"

DEPARTAMENTOS_SERVIR = [
    "AMAZONAS", "ANCASH", "APURIMAC", "AREQUIPA", "AYACUCHO",
    "CAJAMARCA", "CALLAO", "CUSCO", "HUANCAVELICA", "HUANUCO",
    "ICA", "JUNIN", "LA LIBERTAD", "LAMBAYEQUE", "LIMA",
    "LORETO", "MADRE DE DIOS", "MOQUEGUA", "PASCO", "PIURA",
    "PUNO", "SAN MARTIN", "TACNA", "TUMBES", "UCAYALI"
]

# IDs fijos del formulario JSF de SERVIR (obtenidos por inspección)
BTN_BUSCAR_ID  = "frmLstOfertsLabo:j_idt42"
BTN_SIGUIENTE_ID = "frmLstOfertsLabo:j_idt56"   # botón "Sig." superior
BTN_ULTIMO_ID    = "frmLstOfertsLabo:j_idt57"
PANEL_REPEAT_PREFIX = "frmLstOfertsLabo:idPnlRepeatPuestos"  # base de los paneles de oferta
INPUT_DEPARTAMENTO_ID = "frmLstOfertsLabo:cboDep_focus"


# -----------------------------------------------------------------------
# Parsear experiencia / salario
# -----------------------------------------------------------------------
def parse_experience_years(text: str) -> float:
    if not text:
        return 0.0
    t = text.lower()
    if any(k in t for k in ["no requiere", "sin experiencia", "no indispensable", "no se requiere"]):
        return 0.0
    if any(k in t for k in ["practicante", "prácticas", "practicas"]):
        return 0.0

    m = re.search(r'\((\d+)\)\s*años?', t)
    if m: return float(m.group(1))
    m = re.search(r'\((\d+)\)\s*meses?', t)
    if m: return float(m.group(1)) / 12.0
    m = re.search(r'(\d+)\s*años?', t)
    if m: return float(m.group(1))
    m = re.search(r'(\d+)\s*meses?', t)
    if m: return float(m.group(1)) / 12.0

    spanish = {
        'un': 1, 'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4,
        'cinco': 5, 'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10
    }
    for word, val in spanish.items():
        if re.search(rf'\b{word}\b\s*años?', t): return float(val)
        if re.search(rf'\b{word}\b\s*meses?', t): return float(val) / 12.0
    return 0.0


def parse_salary(text: str) -> float:
    if not text:
        return 0.0
    # Eliminar S/., espacios y comas (separador de miles)
    # Ej: "S/. 12,000.00" -> 12000.0
    cleaned = re.sub(r'[S/\s]', '', text).replace(',', '')
    m = re.search(r'\d+\.?\d*', cleaned)
    if m:
        try:
            val = float(m.group())
            if val > 100:
                return val
        except ValueError:
            pass
    return 0.0


# -----------------------------------------------------------------------
# Seleccionar departamento en el dropdown JSF/PrimeFaces
# -----------------------------------------------------------------------
def select_departamento(page, departamento: str) -> bool:
    """
    El campo de departamento en SERVIR es un PrimeFaces SelectOneMenu.
    El input visible tiene id 'frmLstOfertsLabo:cboDep_focus'.
    El componente real tiene id 'frmLstOfertsLabo:cboDep'.
    """
    try:
        # Intentar con el select nativo primero (por si hay fallback)
        select_el = page.query_selector('#frmLstOfertsLabo\\:cboDep')
        if select_el:
            select_el.select_option(label=departamento)
            page.wait_for_timeout(500)
            print(f"    -> Departamento seleccionado: {departamento}")
            return True

        # PrimeFaces SelectOneMenu: click en el label para abrir el panel
        sf_trigger = page.query_selector(
            '#frmLstOfertsLabo\\:cboDep_label, '
            '.ui-selectonemenu-trigger, '
            'div[id*="cboDep"] .ui-selectonemenu-label'
        )
        if sf_trigger:
            sf_trigger.click()
            page.wait_for_timeout(600)
            # Buscar los items del dropdown abierto
            items = page.query_selector_all('.ui-selectonemenu-item, .ui-selectonemenu-list-item')
            for item in items:
                if departamento.upper() in item.inner_text().upper():
                    item.click()
                    page.wait_for_timeout(400)
                    print(f"    -> Departamento seleccionado (PrimeFaces): {departamento}")
                    return True

        print(f"    [WARN] No se pudo seleccionar departamento '{departamento}'.")
        return False
    except Exception as e:
        print(f"    [WARN] Error seleccionando departamento: {e}")
        return False


# -----------------------------------------------------------------------
# Extraer datos de los paneles de la página actual
# -----------------------------------------------------------------------
def extract_panels_data(page) -> List[Dict[str, Any]]:
    """
    SERVIR muestra cada oferta en un div con clase 'cuadro-vacantes'.
    Estructura real descubierta por inspección:
      - <div class="col-sm-12 cuadro-vacantes">  ← contenedor de cada oferta
        - <div class="titulo-vacante"><label>NOMBRE PUESTO</label>
        - <div class="nombre-entidad"><span class="detalle-sp"><b>ENTIDAD</b>
        - <span class="sub-titulo">Ubicación:</span> + <span class="detalle-sp">VALOR</span>
        - <span class="sub-titulo">Remuneración:</span> + <span class="detalle-sp">S/. X</span>
        - <span class="sub-titulo">Fecha Fin...</span> + <span class="detalle-sp">dd/mm/yyyy</span>
    """
    jobs = []

    html = page.content()
    soup = BeautifulSoup(html, 'html.parser')

    # Buscar los contenedores de cada oferta por clase CSS real
    panels = soup.find_all('div', class_='cuadro-vacantes')

    if not panels:
        # Fallback: buscar divs que contengan botones de "Ver más" por id conocido
        btn_panels = soup.find_all(id=lambda i: i and 'idPnlRepeatPuestos' in i and 'j_idt71' in i)
        if btn_panels:
            # Subir al abuelo de cada botón para obtener el contenedor real
            seen = set()
            for btn_el in btn_panels:
                ancestor = btn_el.parent
                for _ in range(4):
                    if ancestor and ancestor.parent:
                        ancestor = ancestor.parent
                    else:
                        break
                if ancestor and id(ancestor) not in seen:
                    seen.add(id(ancestor))
                    panels.append(ancestor)

    if not panels:
        print("    [WARN] No se encontraron paneles de ofertas en la página.")
        return []

    print(f"    -> {len(panels)} ofertas encontradas.")

    for panel in panels:
        job = {
            "entidad": "",
            "puesto": "",
            "regimen": "",
            "departamento": "",
            "n_conv": "",
            "fecha_inicio": "",
            "fecha_fin": "",
            "remuneracion_raw": "",
            "remuneracion_numeric": 0.0,
            "n_plazas": "",
            "experiencia_raw": "",
            "experiencia_years": 0.0,
            "link_detalle": ""
        }

        # 1. Título del puesto
        titulo_div = panel.find('div', class_='titulo-vacante')
        if titulo_div:
            lbl = titulo_div.find('label')
            job['puesto'] = (lbl.get_text(strip=True) if lbl else titulo_div.get_text(strip=True))[:200]

        # 2. Entidad
        entidad_div = panel.find('div', class_='nombre-entidad')
        if entidad_div:
            job['entidad'] = entidad_div.get_text(strip=True)[:200]

        # 3. Campos con etiqueta sub-titulo + valor detalle-sp
        sub_titulos = panel.find_all('span', class_='sub-titulo')
        for st in sub_titulos:
            label_text = st.get_text(strip=True).rstrip(':').strip().lower()
            # El valor está en el siguiente span.detalle-sp o como texto siguiente
            valor_span = st.find_next_sibling('span', class_='detalle-sp')
            if not valor_span:
                # A veces está envuelto en el mismo div padre
                parent_div = st.parent
                valor_span = parent_div.find('span', class_='detalle-sp') if parent_div else None
            valor = ' '.join(valor_span.get_text(strip=True).split()) if valor_span else ""

            if 'ubicaci' in label_text or 'departamento' in label_text or 'regi' in label_text:
                job['departamento'] = valor[:100]
            elif 'convocatoria' in label_text and 'número' in label_text:
                job['regimen'] = valor[:100]  # número de conv. incluye régimen laboral
                job['n_conv'] = valor[:100]
            elif 'vacante' in label_text or 'plaza' in label_text or 'cantidad' in label_text:
                job['n_plazas'] = valor[:10]
            elif 'remuner' in label_text or 'sueldo' in label_text:
                job['remuneracion_raw'] = valor[:60]
                job['remuneracion_numeric'] = parse_salary(valor)
            elif 'inicio' in label_text and 'fecha' in label_text:
                job['fecha_inicio'] = valor[:30]
            elif ('fin' in label_text or 'término' in label_text or 'termino' in label_text) and 'fecha' in label_text:
                job['fecha_fin'] = valor[:30]
            elif 'experiencia' in label_text:
                job['experiencia_raw'] = valor[:200]
                job['experiencia_years'] = parse_experience_years(valor)

        # Extraer régimen del número de convocatoria (ej: "D.LEG 1057 - DETERMINADO-150")
        if job['n_conv'] and not job['regimen']:
            job['regimen'] = job['n_conv']
        elif job['n_conv']:
            # Limpiar: dejar solo la parte del régimen (antes del número final)
            m = re.match(r'^(.*?)-\d+$', job['n_conv'])
            if m:
                job['regimen'] = m.group(1).strip()

        if job['puesto'] or job['entidad']:
            jobs.append(job)

    return jobs


# -----------------------------------------------------------------------
# Navegar paginación y extraer todas las páginas
# -----------------------------------------------------------------------
def scrape_all_pages(page, max_pages: int = 20) -> List[Dict[str, Any]]:
    all_jobs = []
    current_page = 1

    while current_page <= max_pages:
        print(f"\n  [Página {current_page}/{max_pages}]")
        jobs = extract_panels_data(page)

        if not jobs:
            print(f"  -> Sin datos. Deteniendo.")
            break

        all_jobs.extend(jobs)
        print(f"  -> {len(jobs)} ofertas. Total acumulado: {len(all_jobs)}")

        # Buscar botón "Sig." para siguiente página
        next_btn = page.query_selector(
            f'#{BTN_SIGUIENTE_ID.replace(":", "\\:")}, '
            'button:has-text("Sig."), '
            'button:has-text("Siguiente")'
        )

        if not next_btn:
            print("  -> No se encontró botón Siguiente. Fin de paginación.")
            break

        # Verificar si está deshabilitado
        is_disabled = next_btn.is_disabled() if hasattr(next_btn, 'is_disabled') else False
        btn_class = next_btn.get_attribute('class') or ''
        if is_disabled or 'disabled' in btn_class.lower():
            print("  -> Botón Siguiente deshabilitado. Última página.")
            break

        try:
            print("  -> Navegando a siguiente página...")
            next_btn.click()
            page.wait_for_timeout(4000)
            current_page += 1
        except Exception as e:
            print(f"  [WARN] Error en paginación: {e}")
            break

    return all_jobs


# -----------------------------------------------------------------------
# Función principal de scraping
# -----------------------------------------------------------------------
def scrape_servir(departamento: str = "LIMA", max_pages: int = 20) -> List[Dict[str, Any]]:
    all_jobs = []

    depts_to_scrape = DEPARTAMENTOS_SERVIR if departamento.upper() == "TODOS" else [departamento.upper()]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage']
        )

        for dept in depts_to_scrape:
            print(f"\n[*] Scrapeando SERVIR - Departamento: {dept}")
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900}
            )
            page = context.new_page()

            try:
                print(f"  -> Cargando página SERVIR...")
                page.goto(SERVIR_URL, timeout=60000, wait_until='domcontentloaded')

                # Esperar carga de componentes JSF/PrimeFaces
                print(f"  -> Esperando carga JSF (10s)...")
                page.wait_for_timeout(10000)

                # Seleccionar departamento si no es LIMA o si se especificó
                if dept != "LIMA":
                    select_departamento(page, dept)
                    page.wait_for_timeout(800)

                # Hacer clic en botón "Buscar"
                buscar_btn = page.query_selector(f'#{BTN_BUSCAR_ID.replace(":", "\\:")}')
                if not buscar_btn:
                    buscar_btn = page.query_selector('button:has-text("Buscar")')

                if buscar_btn:
                    print(f"  -> Haciendo clic en 'Buscar'...")
                    buscar_btn.click()
                    print(f"  -> Esperando resultados (8s)...")
                    page.wait_for_timeout(8000)
                else:
                    print(f"  [WARN] No se encontró botón Buscar.")

                # Scraping de todas las páginas
                dept_jobs = scrape_all_pages(page, max_pages)

                # Asegurar que el campo departamento esté lleno
                for job in dept_jobs:
                    if not job.get('departamento'):
                        job['departamento'] = dept

                all_jobs.extend(dept_jobs)
                print(f"\n  [OK] {dept}: {len(dept_jobs)} convocatorias extraídas.")

            except PlaywrightTimeoutError:
                print(f"  [ERROR] Timeout cargando SERVIR para {dept}.", file=sys.stderr)
            except Exception as e:
                print(f"  [ERROR] {dept}: {e}", file=sys.stderr)
            finally:
                context.close()

        browser.close()

    return all_jobs


# -----------------------------------------------------------------------
# Integración con el bot de Telegram (llamada externa)
# -----------------------------------------------------------------------
def scrape_servir_for_bot(departamento: str = "LIMA", max_pages: int = 3) -> List[Dict[str, Any]]:
    """Versión simplificada para usar desde el bot de Telegram (pocas páginas)."""
    return scrape_servir(departamento=departamento, max_pages=max_pages)


# -----------------------------------------------------------------------
# main()
# -----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Scraper de Ofertas Laborales de SERVIR (app.servir.gob.pe)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Departamentos válidos:\n  {', '.join(DEPARTAMENTOS_SERVIR)}\n  TODOS (para todas las regiones)"
    )
    parser.add_argument(
        "--departamento", default="LIMA",
        help="Departamento a filtrar (default: LIMA). Usa TODOS para todas las regiones."
    )
    parser.add_argument(
        "--paginas", type=int, default=5,
        help="Máximo de páginas a recorrer por departamento (default: 5)"
    )
    parser.add_argument(
        "--output", default="output/servir.csv",
        help="Ruta del archivo CSV de salida"
    )

    args = parser.parse_args()
    dept_upper = args.departamento.upper()

    if dept_upper != "TODOS" and dept_upper not in DEPARTAMENTOS_SERVIR:
        print(f"[WARN] '{args.departamento}' no reconocido. Usando LIMA.")
        print(f"  Válidos: {', '.join(DEPARTAMENTOS_SERVIR)}")
        dept_upper = "LIMA"

    os.makedirs("output", exist_ok=True)
    if args.output == "output/servir.csv":
        ts = time.strftime("%Y%m%d_%H%M%S")
        args.output = f"output/servir_{dept_upper.lower()}_{ts}.csv"

    print(f"[*] === Scraper SERVIR ===")
    print(f"[*] Departamento : {dept_upper}")
    print(f"[*] Páginas máx. : {args.paginas}")
    print(f"[*] Salida       : {args.output}")

    start = time.time()
    jobs = scrape_servir(departamento=dept_upper, max_pages=args.paginas)

    if not jobs:
        print("\n[!] No se encontraron convocatorias.")
        return 1

    # Ordenar: menor experiencia → mayor salario
    jobs.sort(key=lambda x: (x.get('experiencia_years', 0.0), -x.get('remuneracion_numeric', 0.0)))

    fieldnames = [
        "entidad", "puesto", "regimen", "n_conv", "departamento",
        "n_plazas", "fecha_inicio", "fecha_fin",
        "remuneracion_raw", "remuneracion_numeric",
        "experiencia_raw", "experiencia_years",
        "link_detalle"
    ]

    try:
        with open(args.output, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(jobs)

        elapsed = time.time() - start
        print(f"\n[OK] Completado en {elapsed:.1f}s")
        print(f"[*] Total: {len(jobs)} convocatorias")
        print(f"[*] Guardado en: {args.output}")

    except Exception as e:
        print(f"[ERROR] No se pudo guardar el CSV: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
