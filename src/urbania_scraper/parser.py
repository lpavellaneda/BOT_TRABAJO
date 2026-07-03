import re
import base64
import json
from typing import Dict, List, Optional
from urllib.parse import unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from .models import Listing


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# Campos tecnicos que no son amenities reales
BLACKLIST_AMENITIES = {
    "número de pisos", "departamentos por piso", "antigüedad", 
    "mantenimiento", "asensor", "ascensor", "nro de pisos",
    "ambientes", "dormitorios", "baños", "cocheras",
    "promotion", "description", "title", "multimedia", "pago"
}


def _is_valid_amenity(text: str) -> bool:
    if not text: return False
    t = text.lower().strip()
    if len(t) < 4: return False
    if re.search(r'\d', t) and "km" not in t: return False
    if any(bad in t for bad in BLACKLIST_AMENITIES): return False
    return True


def extract_total_pages(html: str) -> int:
    """Intenta encontrar el número total de páginas en el listado."""
    soup = BeautifulSoup(html, "html.parser")
    pages = [1]
    
    # 1. Buscar en el JSON interno de Urbania (suelen guardar el total allí)
    # Buscamos patrones como "pages":XX o "totalPages":XX en los scripts
    scripts = soup.find_all("script")
    for s in scripts:
        content = s.string if s.string else ""
        # Buscar patrones de paginación en el JSON de estado inicial
        match = re.search(r'"(?:total|last|max)Page[s]?":\s*(\d+)', content, re.I)
        if match:
            pages.append(int(match.group(1)))
            
    # 2. Selectores específicos de Urbania (data-qa)
    selectors = [
        "[data-qa='PAGINATION_LAST_PAGE']",
        "[class*='Pagination'] li",
        "[class*='Pagination'] a",
        "li[class*='PageItem']",
        "a[class*='PageLink']"
    ]
    
    for sel in selectors:
        nodes = soup.select(sel)
        for n in nodes:
            txt = n.get_text().strip()
            if txt.isdigit():
                pages.append(int(txt))
    
    # 3. Buscar el número más alto en links que contienen "pagina-"
    links = soup.find_all("a", href=True)
    for link in links:
        href = link["href"]
        match = re.search(r"pagina-(\d+)\.html", href, re.I)
        if match:
            pages.append(int(match.group(1)))
            
    total = max(pages)
    return total


def _safe_text(node) -> Optional[str]:
    if not node: return None
    return _normalize_spaces(node.get_text(" ", strip=True))


def _parse_number(text: Optional[str]) -> Optional[float]:
    if not text: return None
    cleaned = text.replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    return float(match.group(0)) if match else None


def _parse_int(text: Optional[str]) -> Optional[int]:
    value = _parse_number(text)
    return int(value) if value is not None else None


def url_key(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(query="", fragment="")).rstrip("/")


def _extract_price_only(text: str) -> Optional[str]:
    """Extrae el monto numérico, priorizando Soles (S/) sobre Dólares si ambos están presentes."""
    if not text: return None
    
    # 1. Intentar buscar Soles primero
    soles_match = re.search(r"(?:s/|s\./|soles)\s*([\d\.,]+)", text, re.I)
    if soles_match:
        return soles_match.group(1)
    
    # 2. Si no hay soles, buscar Dólares
    usd_match = re.search(r"(?:us\$|\$|d[o\u00f3]lares)\s*([\d\.,]+)", text, re.I)
    return usd_match.group(1) if usd_match else None


def _first_text(node, selectors: List[str]) -> Optional[str]:
    if not node: return None
    for selector in selectors:
        text = _safe_text(node.select_one(selector))
        if text: return text
    return None


def _detect_property_type(soup: BeautifulSoup) -> str:
    """Detecta si es Casa, Departamento, Local, etc. usando SOLO selectores CSS."""
    # 1. Selector principal en Detalle
    type_el = soup.select_one("h2.title-type-sup-property, [class*='title-type-sup-property']")
    if type_el:
        text = type_el.get_text().split("\u00b7")[0].strip()
        if text: return text.capitalize()
    
    # 2. Fallback Breadcrumbs
    bc_links = soup.select("[class*='breadcrumb'] li, [class*='Breadcrumb'] a")
    for link in bc_links:
        txt = link.get_text().strip().lower()
        if any(kw in txt for kw in ["departamento", "casa", "local", "oficina", "terreno", "cochera"]):
            return txt.capitalize()
            
    # 3. Fallback H1
    h1 = soup.select_one("h1")
    if h1:
        txt = h1.get_text().lower()
        if "departamento" in txt: return "Departamento"
        if "casa" in txt: return "Casa"
        if "local" in txt: return "Local"
        if "oficina" in txt: return "Oficina"
        if "terreno" in txt: return "Terreno"
    return "Otro"


def _parse_feature_number(features_text: str, pattern: str) -> Optional[int]:
    match = re.search(pattern, features_text, re.I)
    return int(match.group(1)) if match else None


def _parse_feature_area(features_text: str) -> Optional[float]:
    match = re.search(r"(\d+(?:[\.,]\d+)?)\s*(?:a\s*\d+(?:[\.,]\d+)?\s*)?m(?:2|\u00b2)", features_text, re.I)
    return _parse_number(match.group(1)) if match else None


def _extract_li_value_by_icon(soup: BeautifulSoup, icon_pattern: str) -> Optional[str]:
    # 1. Buscar el icono directamente
    alt_pattern = icon_pattern.replace("-", "_")
    icon = soup.select_one(f"[class*='{icon_pattern}'], [class*='{alt_pattern}']")
    
    if not icon: return None
    
    # 2. Buscar el texto en el padre inmediato o en el propio nodo
    parent = icon.parent
    if not parent: return None
    
    # Extraemos el texto del contenedor pequeño (li o div pequeño)
    txt = _safe_text(parent)
    
    # 3. Validación: si el texto es gigante, es que agarramos un contenedor equivocado
    # La antigüedad o el área no suelen medir más de 40 caracteres.
    if txt and len(txt) > 40:
        # Reintento: buscar solo nodos de texto hijos directos
        txt = "".join([t for t in parent.find_all(string=True, recursive=False)]).strip()
        
    return txt if txt and len(txt) <= 40 else None


def _extract_coordinates(html: str) -> tuple:
    """Extrae latitud y longitud decodificando el Base64 de las variables de Urbania."""
    try:
        lat_match = re.search(r'mapLatOf\s*=\s*["\']([^"\']+)["\']', html)
        lng_match = re.search(r'mapLngOf\s*=\s*["\']([^"\']+)["\']', html)
        
        lat, lng = None, None
        if lat_match:
            lat = base64.b64decode(lat_match.group(1)).decode("utf-8")
        if lng_match:
            lng = base64.b64decode(lng_match.group(1)).decode("utf-8")
        return lat, lng
    except Exception:
        return None, None


def detect_block_reason(html: str) -> Optional[str]:
    lower = html.lower()
    if any(m in lower for m in ["just a moment", "un momento", "_cf_chl_opt", "verificar que usted es un ser humano"]):
        return "Cloudflare challenge"
    if "attention required" in lower and "cloudflare" in lower:
        return "Cloudflare block"
    return None


def parse_listings(html: str, base_url: str) -> List[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    items: List[Listing] = []

    cards = soup.select("[data-qa^='posting'][data-to-posting], div[class*='postingCardLayout'][data-to-posting]")
    for card in cards:
        href = (card.get("data-to-posting") or "").strip()
        if not href:
            link = card.select_one("a[href*='/inmueble/']")
            href = (link.get("href") or "").strip() if link else ""
        if not href or "/inmueble/" not in href.lower(): continue

        url = urljoin(base_url, href)
        url = url_key(url)
        if url in seen: continue
        seen.add(url)

        price_text = _first_text(card, ["[data-qa='POSTING_CARD_PRICE']", "[class*='postingPrices-module__price']"])
        price = _extract_price_only(price_text or "")
        
        address = _first_text(card, ["[class*='postingLocations-module__location-address']"])
        location_text = _first_text(card, ["[data-qa='POSTING_CARD_LOCATION']", "[class*='postingLocations-module__location-text']"])
        description = _first_text(card, ["[data-qa='POSTING_CARD_DESCRIPTION']", "[class*='postingCard-module__posting-description']"])
        features_text = _first_text(card, ["[data-qa='POSTING_CARD_FEATURES']", "[class*='postingMainFeatures-module__posting-main-features-block']"]) or ""
        
        advertiser = _first_text(card, ["h3[class*='publisher-name']", "h3[class*='publisherData-module__publisher-name']"])
        pills = card.select("[class*='pill-item-feature']")
        amenities_pills = ", ".join([_normalize_spaces(p.text) for p in pills if _is_valid_amenity(p.text)])
        location_parts = [part for part in [address, location_text] if part]

        items.append(
            Listing(
                property_type=_detect_property_type(card),
                price=price or price_text,
                location=" - ".join(location_parts) if location_parts else None,
                advertiser=advertiser,
                description=description,
                detail_url=url,
                amenities=amenities_pills or None,
            )
        )
    return items


def extract_exact_unit_links(html: str, base_url: str) -> List[str]:
    """Busca los links de departamentos individuales dentro de un proyecto."""
    soup = BeautifulSoup(html, "html.parser")
    links: List[str] = []
    seen = set()
    base_key = url_key(base_url)

    def add_link(raw_href: str) -> None:
        if not raw_href: return
        href = str(raw_href).strip().replace("\\/", "/")
        if "/inmueble/" not in href.lower(): return
        absolute = urljoin(base_url, href)
        key = url_key(absolute)
        if key != base_key and key not in seen:
            seen.add(key)
            links.append(absolute)

    # 1. Selectores CSS (Unidades pre-renderizadas o tablas)
    selectors = [
        "#reactDevelopmentUnits a[href*='/inmueble/']",
        "[class*='dataContainer'] a[href*='/inmueble/']",
        "[class*='UnitGroup'] a[href*='/inmueble/']",
        "[class*='UnitsTable'] a[href*='/inmueble/']"
    ]
    for sel in selectors:
        for node in soup.select(sel):
            add_link(node.get("href") or "")

    # 2. Fallback de Texto (Para cuando React no ha renderizado el HTML)
    # Buscamos patrones de URLs de inmuebles en los scripts
    urls_in_scripts = re.findall(r'["\'](https?://urbania\.pe/inmueble/[^"\']+)["\']', html)
    for u in urls_in_scripts:
        add_link(u)
    
    # Tambien URLs relativas en scripts de unidades
    rel_urls = re.findall(r'["\']url["\']:\s*["\'](/inmueble/[^"\']+)["\']', html)
    for u in rel_urls:
        add_link(u)

    return links


def _parse_delivery_status(soup: BeautifulSoup) -> tuple:
    try:
        status_div = soup.select_one(".status-delivery")
        if not status_div: return None, None
        delivery_status, delivery_date = None, None
        for item in status_div.select(".item"):
            classes = item.get("class", [])
            if "IN_PROGRESS" in classes:
                label_el = item.select_one(".label")
                if label_el:
                    if "bold" in classes:
                        delivery_status = re.sub(r"[\u2713\u2192\u25ba\u25b8\u2714\u279c\u00bb]+", "", label_el.text).strip()
                    else:
                        delivery_date = _normalize_spaces(label_el.text)
        return delivery_status, delivery_date
    except Exception: return None, None


def parse_project_fields(html: str, base_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    amenities = None
    amen_cont = soup.select_one("[class*='devFeaturesNew'], [class*='features-module__features']")
    if amen_cont:
        labels = amen_cont.select("[class*='icon-label'], [class*='feature-label']")
        amenity_list = [_normalize_spaces(n.text) for n in labels if _is_valid_amenity(n.text)]
        amenities = ", ".join(amenity_list) if amenity_list else None
    
    delivery_status, delivery_date = _parse_delivery_status(soup)
    advertiser = _first_text(soup, ["h3[class*='publisher-name']", "h3[class*='publisherData-module__publisher-name']"])
    if not advertiser:
        match = re.search(r'publisherId["\']:\s*["\']\d+["\'],\s*["\']name["\']:\s*["\']([^"\']+)["\']', html, re.S)
        if match: advertiser = match.group(1)

    # Extraer caracteristicas principales de los iconos/li
    # Usamos regex especificos sobre el texto del li para evitar cruces (ej: area en dormitorios)
    total_area_text = _extract_li_value_by_icon(soup, "icon-stotal") or \
              _extract_li_value_by_icon(soup, "icon-terreno") or \
              _extract_li_value_by_icon(soup, "icon-area") or \
              _extract_li_value_by_icon(soup, "icon-superficie") or \
              _extract_li_value_by_icon(soup, "icon-dimensiones")
    covered_area_text = _extract_li_value_by_icon(soup, "icon-scubierta") or \
              _extract_li_value_by_icon(soup, "icon-construido")
    
    total_area = _parse_feature_area(total_area_text) if total_area_text else None
    covered_area = _parse_feature_area(covered_area_text) if covered_area_text else None
    
    # Fallback: si falta uno, se usa el otro
    if total_area is None and covered_area is not None: total_area = covered_area
    if covered_area is None and total_area is not None: covered_area = total_area

    bed_text = _extract_li_value_by_icon(soup, "icon-dormitorio")
    bath_text = _extract_li_value_by_icon(soup, "icon-bano")
    park_text = _extract_li_value_by_icon(soup, "icon-cochera") or _extract_li_value_by_icon(soup, "icon-estacionamiento")

    age_text = _extract_li_value_by_icon(soup, "icon-antiguedad")
    age = _normalize_spaces(age_text) if age_text else None

    # Lógica de Condición
    condition = "Estreno" # Los proyectos son por definición de estreno
    if not delivery_status and age and "estrenar" not in age.lower():
        # Si no es un proyecto y tiene años, es segunda
        condition = "Segunda"

    lat, lng = _extract_coordinates(html)

    return {
        "property_type": _detect_property_type(soup),
        "total_area": total_area,
        "covered_area": covered_area,
        "bedrooms": _parse_int(bed_text),
        "bathrooms": _parse_int(bath_text),
        "parking": _parse_int(park_text),
        "amenities": amenities,
        "delivery_status": delivery_status,
        "delivery_date": delivery_date,
        "advertiser": advertiser,
        "age": age,
        "condition": condition,
        "latitude": lat,
        "longitude": lng,
    }


def parse_listing_detail_fields(html: str, detail_url: str) -> Dict[str, Optional[object]]:
    soup = BeautifulSoup(html, "html.parser")
    full_text = _normalize_spaces(soup.get_text(" ", strip=True))
    
    price_node = soup.select_one(".price-items")
    price_text = _safe_text(price_node)
    price = _extract_price_only(price_text or "")
    if not price: # Fallback al texto completo de la página si no hay en el nodo
        price = _extract_price_only(full_text)
    
    amenities = None
    labels = soup.select("[class*='icon-label']")
    amen_list = [_normalize_spaces(n.text) for n in labels if _is_valid_amenity(n.text)]
    amenities = ", ".join(amen_list) if amen_list else None

    ds, dd = _parse_delivery_status(soup)
    
    # Extraer caracteristicas principales de los iconos/li
    total_area_text = _extract_li_value_by_icon(soup, "icon-stotal") or \
              _extract_li_value_by_icon(soup, "icon-terreno") or \
              _extract_li_value_by_icon(soup, "icon-area") or \
              _extract_li_value_by_icon(soup, "icon-superficie") or \
              _extract_li_value_by_icon(soup, "icon-dimensiones")
    covered_area_text = _extract_li_value_by_icon(soup, "icon-scubierta") or \
              _extract_li_value_by_icon(soup, "icon-construido")
    
    total_area = _parse_feature_area(total_area_text) if total_area_text else None
    covered_area = _parse_feature_area(covered_area_text) if covered_area_text else None
    
    if total_area is None and covered_area is not None: total_area = covered_area
    if covered_area is None and total_area is not None: covered_area = total_area

    bed_text = _extract_li_value_by_icon(soup, "icon-dormitorio")
    bath_text = _extract_li_value_by_icon(soup, "icon-bano") # bano/banio/baño
    park_text = _extract_li_value_by_icon(soup, "icon-cochera") or _extract_li_value_by_icon(soup, "icon-estacionamiento")

    age_text = _extract_li_value_by_icon(soup, "icon-antiguedad")
    age = _normalize_spaces(age_text) if age_text else None

    lat, lng = _extract_coordinates(html)

    # Lógica de Condición: 
    # 1. Si tiene status de entrega (construcción/inmediata) -> Estreno
    # 2. Si la antigüedad dice "A estrenar" -> Estreno
    # 3. Si tiene años -> Segunda
    condition = "Segunda"
    if ds or (age and "estrenar" in age.lower()):
        condition = "Estreno"
    elif age and any(char.isdigit() for char in age):
        condition = "Segunda"

    fields = {
        "property_type": _detect_property_type(soup),
        "price": _extract_price_only(_first_text(soup, [".price-value", ".re-detail-price", "[class*='price']"])),
        "location": _first_text(soup, [".re-detail-location", ".section-location-property h2", "[class*='location']"]),
        "total_area": total_area,
        "covered_area": covered_area,
        "bedrooms": _parse_int(bed_text),
        "bathrooms": _parse_int(bath_text),
        "parking": _parse_int(park_text),
        "advertiser": _first_text(soup, ["h3[class*='publisher-name']", "h3[class*='publisherData-module__publisher-name']", "h3[data-qa='linkMicrositioAnunciante']"]),
        "description": _first_text(soup, ["#style-7", ".section-description--content", "[class*='description']"]),
        "delivery_status": ds,
        "delivery_date": dd,
        "amenities": amenities,
        "age": age,
        "condition": condition,
        "latitude": lat,
        "longitude": lng,
    }
    
    if not fields["advertiser"]:
        match = re.search(r'publisherId["\']:\s*["\']\d+["\'],\s*["\']name["\']:\s*["\']([^"\']+)["\']', html, re.S)
        if not match: match = re.search(r'publisherName\s*=\s*["\']([^"\']+)["\']', html, re.S)
        if match: fields["advertiser"] = match.group(1)

    return fields
