import csv
import json
import logging
import os
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from curl_cffi import requests

from .models import Listing
from .parser import (
    detect_block_reason,
    extract_exact_unit_links,
    extract_total_pages,
    parse_listing_detail_fields,
    parse_listings,
    parse_project_fields,
    url_key,
)

logger = logging.getLogger(__name__)


def _merge_listing_with_detail(base: Listing, detail_fields: dict, detail_url: str) -> Listing:
    """Combina los campos de la tarjeta con los campos del detalle."""
    # Combinar amenities de forma limpia
    amen_set = set()
    for source in [base.amenities, detail_fields.get("amenities")]:
        if source:
            parts = [a.strip() for a in str(source).split(",") if a.strip()]
            amen_set.update(parts)
    
    combined_amenities = ", ".join(sorted(list(amen_set))) if amen_set else None

    return Listing(
        property_type=detail_fields.get("property_type") or base.property_type,
        price=detail_fields.get("price") or base.price,
        location=detail_fields.get("location") or base.location,
        total_area=detail_fields.get("total_area"),
        covered_area=detail_fields.get("covered_area"),
        bedrooms=detail_fields.get("bedrooms"),
        bathrooms=detail_fields.get("bathrooms"),
        parking=detail_fields.get("parking"),
        advertiser=detail_fields.get("advertiser") or base.advertiser,
        description=detail_fields.get("description") or base.description,
        detail_url=detail_url,
        delivery_status=detail_fields.get("delivery_status") or base.delivery_status,
        delivery_date=detail_fields.get("delivery_date") or base.delivery_date,
        amenities=combined_amenities,
        age=detail_fields.get("age"),
        condition=detail_fields.get("condition"),
        latitude=detail_fields.get("latitude"),
        longitude=detail_fields.get("longitude"),
    )


def _expand_listing_via_detail_curlcffi_standalone(item: Listing, timeout: int, proxy: Optional[str]) -> List[Listing]:
    """
    Entra al detalle de un anuncio. Si es un proyecto con unidades, devuelve las unidades.
    Si no, devuelve el anuncio base enriquecido con los datos del detalle.
    """
    try:
        with requests.Session() as session:
            resp = session.get(item.detail_url, timeout=timeout, impersonate="chrome124", proxies={"http": proxy, "https": proxy} if proxy else None)
            if resp.status_code != 200:
                return [item]
            
            html = resp.text
            
            # 1. Detectar si es un proyecto con unidades
            type_links = extract_exact_unit_links(html, item.detail_url)
            
            # 2. Extraer campos adicionales (Amenities del proyecto, Anunciante, etc)
            p_fields = parse_project_fields(html, item.detail_url)
            enhanced_base = _merge_listing_with_detail(item, p_fields, item.detail_url)
            
            if not type_links:
                # No hay unidades -> Devolvemos el anuncio actual enriquecido
                return [enhanced_base]
            
            # 3. Si hay unidades, las procesamos y DESCARTAMOS el anuncio padre
            results: List[Listing] = []
            base_url_key = url_key(item.detail_url)
            for u_url in type_links:
                if url_key(u_url) == base_url_key: continue
                try:
                    # Pequeño delay para evitar bloqueos
                    time.sleep(0.1)
                    u_resp = session.get(u_url, timeout=timeout, impersonate="chrome124", proxies={"http": proxy, "https": proxy} if proxy else None)
                    if u_resp.status_code == 200:
                        u_fields = parse_listing_detail_fields(u_resp.text, u_url)
                        unit = _merge_listing_with_detail(enhanced_base, u_fields, u_resp.url)
                        
                        # Si es un proyecto con unidades, solo queremos las unidades con datos reales
                        # para evitar la fila "vacia" del resumen del proyecto.
                        if unit.total_area or unit.covered_area or unit.bedrooms or unit.bathrooms:
                            results.append(unit)
                        else:
                            logger.debug(f"Saltando unidad/resumen sin datos tecnicos: {u_url}")
                except Exception as e:
                    logger.error(f"Error expandiendo unidad {u_url}: {e}")
            
            return results if results else [enhanced_base]

    except Exception as e:
        logger.error(f"Error expandiendo anuncio {item.detail_url}: {e}")
        return [item]


def fetch_listings_curlcffi(
    search_url: str,
    pages: int = 1,
    start_page: int = 1,
    workers: int = 5,
    timeout: int = 30,
    max_results: int = 0,
    proxy: Optional[str] = None
) -> List[Listing]:
    """Scraper principal usando curl-cffi."""
    all_items = []
    
    with requests.Session() as session:
        for page in range(start_page, start_page + pages):
            page_url = f"{search_url.rstrip('/')}-pagina-{page}.html" if page > 1 else search_url
            print(f">>> Procesando página {page}...")
            logger.info(f"[curl-cffi] Listado pagina {page}: {page_url}")
            
            try:
                # Modo Sigilo: Pausa aleatoria para no saturar Cloudflare
                time.sleep(random.uniform(1.0, 3.0))
                
                resp = session.get(page_url, timeout=timeout, impersonate="chrome124", proxies={"http": proxy, "https": proxy} if proxy else None)
                if resp.status_code != 200:
                    logger.warning(f"Error {resp.status_code} en pagina {page}")
                    continue
                
                reason = detect_block_reason(resp.text)
                if reason:
                    logger.error(f"Bloqueo detectado: {reason}")
                    break
                
                page_listings = parse_listings(resp.text, resp.url)
                if max_results > 0 and len(all_items) + len(page_listings) > max_results:
                    page_listings = page_listings[:max_results - len(all_items)]

                logger.info(f"[curl-cffi] Pagina {page}: {len(page_listings)} anuncios encontrados")
                
                if not page_listings:
                    break
                
                # Expansion de detalles con hilos
                print(f"    -> Expandiendo detalles de {len(page_listings)} anuncios...")
                logger.info(f"[curl-cffi] Expandiendo {len(page_listings)} anuncios con {workers} hilos...")
                
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = [
                        executor.submit(_expand_listing_via_detail_curlcffi_standalone, item, timeout, proxy)
                        for item in page_listings
                    ]
                    
                    for future in as_completed(futures):
                        try:
                            expanded = future.result()
                            all_items.extend(expanded)
                        except Exception as e:
                            logger.error(f"Error en hilo: {e}")
                
                if max_results > 0 and len(all_items) >= max_results:
                    break
                
            except Exception as e:
                logger.error(f"Error en pagina {page}: {e}")
                
    return all_items


def run_automated_scraper(
    search_url: str,
    workers: int = 5,
    timeout: int = 30,
    output_path: str = "output/urbania.csv",
    proxy: Optional[str] = None
):
    """Ejecuta el scraper de forma automática gestionando progreso y reintentos."""
    state_file = ".scraper_state"
    start_page = 1
    all_results: List[Listing] = []
    
    # 1. Cargar estado anterior si existe
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
                if state.get("url") == search_url:
                    start_page = state.get("last_page", 0) + 1
                    # Recuperar el nombre del archivo original para seguir escribiendo en él
                    if state.get("output_path"):
                        output_path = state.get("output_path")
                    print(f"[*] Resumiendo desde la página {start_page} en el archivo {output_path}...")
        except: pass

    # 2. Obtener total de páginas (opcional)
    total_pages = 999 # Valor alto por defecto
    try:
        with requests.Session() as s:
            r = s.get(search_url, timeout=timeout, impersonate="chrome124", proxies={"http": proxy, "https": proxy} if proxy else None)
            total_pages = extract_total_pages(r.text)
            print(f"[*] Total estimado de páginas: {total_pages}")
    except: pass

    # 3. Bucle de scraping por bloques de 1 página para guardar progreso
    for current_page in range(start_page, total_pages + 1):
        print(f"\n[AUTO] Procesando bloque {current_page} de {total_pages}...")
        
        try:
            items = fetch_listings_curlcffi(
                search_url=search_url,
                pages=1,
                start_page=current_page,
                workers=workers,
                timeout=timeout,
                proxy=proxy
            )
            
            if not items:
                print("[!] No se encontraron más anuncios. Finalizando.")
                break
                
            all_results.extend(items)
            
            # Guardar progreso parcial en el CSV para no perder nada
            # (Si es la primera página escribimos cabecera, si no, añadimos)
            is_new = not os.path.exists(output_path) or current_page == 1
            mode = "w" if is_new else "a"
            
            # Exportación parcial
            with open(output_path, mode, newline="", encoding="utf-8-sig") as f:
                keys = items[0].to_dict().keys()
                writer = csv.DictWriter(f, fieldnames=keys)
                if is_new: writer.writeheader()
                for item in items:
                    writer.writerow(item.to_dict())
            
            # Guardar estado con el nombre del archivo para la próxima vez
            with open(state_file, "w") as f:
                json.dump({
                    "url": search_url, 
                    "last_page": current_page,
                    "output_path": output_path
                }, f)
                
            print(f"[OK] Bloque {current_page} completado. Total acumulado: {len(all_results)}")
            
        except Exception as e:
            print(f"[ERROR] Falló la página {current_page}: {e}")
            print("[*] Reintentando en 60 segundos...")
            time.sleep(60)
            continue # Reintentar la misma página

    # Al terminar, borrar el archivo de estado
    if os.path.exists(state_file):
        os.remove(state_file)
    
    return all_results


def export_csv(items: List[Listing], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not items: return
    keys = items[0].to_dict().keys()
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for item in items:
            writer.writerow(item.to_dict())


def export_json(items: List[Listing], path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump([item.to_dict() for item in items], f, indent=2, ensure_ascii=False)
