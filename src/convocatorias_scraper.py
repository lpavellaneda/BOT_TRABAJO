import os
import re
import csv
import sys
import time
import argparse
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, parse_qs, urlunparse, urlencode, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

# Define helper to parse experience years
def parse_experience_years(text: str) -> float:
    if not text:
        return 0.0
    text_lower = text.lower()
    if "no requiere" in text_lower or "sin experiencia" in text_lower or "no indispensable" in text_lower:
        return 0.0

    # Let's first search specifically for "general" experience years in this text if present
    if "general" in text_lower:
        # Search for pattern close to general: e.g. "general de seis (6) años" or "general ... 3 años"
        # We can match: general ... X años / meses
        match_gen_yr = re.search(r'general.*?(?:(\d+)\s*años?|\((\d+)\)\s*años?|\b(un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\b\s*años?)', text_lower)
        if match_gen_yr:
            val = match_gen_yr.group(1) or match_gen_yr.group(2)
            if val:
                return float(val)
            word = match_gen_yr.group(3)
            if word:
                spanish_numbers = {
                    'un': 1, 'dos': 2, 'tres': 3, 'cuatro': 4,
                    'cinco': 5, 'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10
                }
                return float(spanish_numbers[word])
                
        match_gen_mo = re.search(r'general.*?(?:(\d+)\s*meses?|\((\d+)\)\s*meses?|\b(un|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\b\s*meses?)', text_lower)
        if match_gen_mo:
            val = match_gen_mo.group(1) or match_gen_mo.group(2)
            if val:
                return float(val) / 12.0
            word = match_gen_mo.group(3)
            if word:
                spanish_numbers = {
                    'un': 1, 'dos': 2, 'tres': 3, 'cuatro': 4,
                    'cinco': 5, 'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10
                }
                return float(spanish_numbers[word]) / 12.0

    # Check years in parentheses, e.g. "Seis (06) años"
    parentheses_years = re.search(r'\((\d+)\)\s*años?', text_lower)
    if parentheses_years:
        return float(parentheses_years.group(1))
        
    # Check months in parentheses, e.g. "Seis (06) meses"
    parentheses_months = re.search(r'\((\d+)\)\s*meses?', text_lower)
    if parentheses_months:
        return float(parentheses_months.group(1)) / 12.0

    # Check normal digit years, e.g. "2 años"
    digit_years = re.search(r'(\d+)\s*años?', text_lower)
    if digit_years:
        return float(digit_years.group(1))

    # Check normal digit months, e.g. "6 meses"
    digit_months = re.search(r'(\d+)\s*meses?', text_lower)
    if digit_months:
        return float(digit_months.group(1)) / 12.0

    # Spanish numbers representation
    spanish_numbers = {
        'un': 1, 'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4,
        'cinco': 5, 'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10
    }
    for name, val in spanish_numbers.items():
        if re.search(rf'\b{name}\b\s*años?', text_lower):
            return float(val)
        if re.search(rf'\b{name}\b\s*meses?', text_lower):
            return float(val) / 12.0

    if "practicante" in text_lower or "prácticas" in text_lower or "practicas" in text_lower:
        return 0.0
        
    return 0.0


def extract_experience_info(requisitos_div) -> Dict[str, Any]:
    experience_raw = ""
    exp_general = ""
    exp_especifica = ""
    
    # 1. Search all list items anywhere in requirements block
    lis = requisitos_div.find_all('li')
    for li in lis:
        li_text = li.get_text(" ", strip=True)
        if "experiencia" in li_text.lower():
            experience_raw += li_text + "\n"
            if "general" in li_text.lower():
                exp_general = li_text
            elif "específica" in li_text.lower() or "especifica" in li_text.lower():
                exp_especifica += li_text + " | "
                
    # 2. Fallback to paragraph searching if no list item contains the word "experiencia"
    if not experience_raw:
        for p in requisitos_div.find_all(['p', 'div']):
            span = p.find('span')
            if span and 'experiencia' in span.text.lower():
                experience_raw = p.get_text(" ", strip=True)
                ul = p.find('ul')
                if ul:
                    for li in ul.find_all('li'):
                        li_text = li.get_text(" ", strip=True)
                        experience_raw += "\n" + li_text
                        if "general" in li_text.lower():
                            exp_general = li_text
                        elif "específica" in li_text.lower() or "especifica" in li_text.lower():
                            exp_especifica += li_text + " | "
                break

    exp_especifica = exp_especifica.rstrip(" | ")
    experience_raw = experience_raw.strip()
    
    # Calculate years
    years_gen = parse_experience_years(exp_general)
    years_spec = parse_experience_years(exp_especifica)
    
    if years_gen > 0:
        years = years_gen
    elif years_spec > 0:
        years = years_spec
    else:
        years = parse_experience_years(experience_raw)
        
    return {
        "experience_raw": experience_raw,
        "experience_years": years,
        "experience_general": exp_general,
        "experience_specific": exp_especifica
    }


def extract_academic_info(requisitos_div) -> str:
    for p in requisitos_div.find_all(['p', 'div']):
        span = p.find('span')
        if span and ('formación' in span.text.lower() or 'formacion' in span.text.lower() or 'estudios' in span.text.lower()):
            return p.get_text(" ", strip=True).replace(span.text, "").strip()
    return ""


def parse_salary(text: str) -> float:
    if not text:
        return 0.0
    digits = re.findall(r'\d+', text.replace(',', '').replace('.', ''))
    if digits:
        return float(digits[0])
    return 0.0


def fetch_job_details(detail_url: str, timeout: int) -> Dict[str, Any]:
    """Downloads a job detail page and extracts experience and degree info."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    result = {
        "experience_raw": "",
        "experience_years": 0.0,
        "experience_general": "",
        "experience_specific": "",
        "academic_degree": ""
    }
    try:
        resp = requests.get(detail_url, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            requisitos_div = soup.find('div', class_='requisitos')
            if requisitos_div:
                exp_info = extract_experience_info(requisitos_div)
                result.update(exp_info)
                result["academic_degree"] = extract_academic_info(requisitos_div)
    except Exception as e:
        print(f"[ERROR] Error fetching details for {detail_url}: {e}", file=sys.stderr)
    return result


def get_page_url(base_url: str, page_num: int) -> str:
    """Updates or adds the 'page' parameter in a query string URL."""
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query['page'] = [str(page_num)]
    new_query = urlencode(query, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def scrape_convocatorias(base_url: str, pages: int, workers: int, timeout: int) -> List[Dict[str, Any]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    jobs = []
    
    for page in range(1, pages + 1):
        page_url = get_page_url(base_url, page)
        print(f"[*] Crawling list page {page}/{pages}: {page_url} ...")
        
        try:
            resp = requests.get(page_url, headers=headers, timeout=timeout)
            if resp.status_code != 200:
                print(f"[WARNING] Status code {resp.status_code} for page {page}. Stopping.", file=sys.stderr)
                break
                
            soup = BeautifulSoup(resp.text, 'html.parser')
            articles = soup.find_all('article', class_='convocatoria')
            if not articles:
                print("[*] No more jobs found on this page. Stopping.")
                break
                
            page_jobs = []
            for art in articles:
                # Parse basic info
                header_sec = art.find('section', class_='conv-header')
                if not header_sec:
                    continue
                    
                h4 = header_sec.find('h4')
                if not h4 or not h4.find('a'):
                    continue
                
                a_tag = h4.find('a')
                title_text = a_tag.text.strip()
                detail_path = a_tag['href']
                detail_url = urljoin("https://www.convocatoriasdetrabajo.com/", detail_path)
                
                # Extract organization/entity from title or image alt
                entity = ""
                img = header_sec.find('img')
                if img and img.get('alt'):
                    entity = img['alt'].replace("Convocatoria", "").strip()
                else:
                    # Fallback to parsing entity from title text
                    if ":" in title_text:
                        entity = title_text.split(":")[0].strip()
                
                # Under conv-detalle
                detalle_div = art.find('div', class_='conv-detalle')
                
                degree = ""
                salary_text = ""
                location = ""
                vacancies = 1
                ends_date = ""
                
                if detalle_div:
                    # Degree
                    grado_li = detalle_div.find('li')
                    if grado_li and grado_li.find('span'):
                        degree = grado_li.find('span').text.strip()
                    
                    # Group items
                    group_li = detalle_div.find('li', class_='convocatoria_group')
                    if group_li:
                        # Location & vacancies
                        map_item = group_li.find('p', class_='convocatoria__group-item')
                        if map_item:
                            spans = map_item.find_all('span')
                            if len(spans) >= 1:
                                location = spans[0].text.strip()
                            if len(spans) >= 2 and "plazas" in spans[1].text:
                                vacancies_match = re.search(r'\d+', spans[1].text)
                                if vacancies_match:
                                    vacancies = int(vacancies_match.group(0))
                        
                        # Salary & calendar
                        other_items = group_li.find_all('p', class_='convocatoria__group-item')
                        for item in other_items:
                            if item.find('i', class_='icon-moneda'):
                                salary_text = item.text.strip()
                            elif item.find('i', class_='icon-calendario'):
                                ends_date = item.text.replace("Finaliza el", "").strip()
                
                page_jobs.append({
                    "title": title_text,
                    "entity": entity,
                    "location": location,
                    "vacancies": vacancies,
                    "salary_raw": salary_text,
                    "salary_numeric": parse_salary(salary_text),
                    "ends_date": ends_date,
                    "detail_url": detail_url,
                    "degree_basic": degree,
                    # Placeholder values to be filled by detail scraper
                    "experience_raw": "",
                    "experience_years": 0.0,
                    "experience_general": "",
                    "experience_specific": "",
                    "academic_degree": ""
                })
            
            # Fetch details in parallel for this page
            print(f"    -> Crawling details for {len(page_jobs)} jobs using {workers} threads...")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_job = {
                    executor.submit(fetch_job_details, job["detail_url"], timeout): job
                    for job in page_jobs
                }
                for future in as_completed(future_to_job):
                    job = future_to_job[future]
                    details = future.result()
                    job.update(details)
                    
            jobs.extend(page_jobs)
            
            # Gentle sleep between list pages
            time.sleep(1)
            
        except Exception as e:
            print(f"[ERROR] Failed to parse page {page}: {e}", file=sys.stderr)
            break
            
    return jobs


def main():
    parser = argparse.ArgumentParser(description="Scraper de Convocatorias de Trabajo")
    parser.add_argument(
        "--url",
        default="https://www.convocatoriasdetrabajo.com/ofertas-de-empleo-en-INGENIERIA-INDUSTRIAL-15.html?page=1&sort=1-valor_salario",
        help="URL base de convocatoriasdetrabajo.com con la búsqueda/carrera requerida"
    )
    parser.add_argument("--pages", type=int, default=1, help="Número de páginas a recorrer")
    parser.add_argument("--workers", type=int, default=5, help="Hilos concurrentes para descargar detalles")
    parser.add_argument("--timeout", type=int, default=15, help="Timeout para requests en segundos")
    parser.add_argument("--output", default="output/convocatorias.csv", help="Ruta de salida del archivo CSV")
    
    args = parser.parse_args()
    
    # Generate timestamped file if output path is default to prevent overwrite
    if args.output == "output/convocatorias.csv":
        os.makedirs("output", exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        args.output = f"output/convocatorias_{timestamp}.csv"
        
    print(f"[*] Iniciando Convocatorias Scraper...")
    print(f"[*] URL: {args.url}")
    print(f"[*] Páginas a procesar: {args.pages}")
    
    start_time = time.time()
    jobs = scrape_convocatorias(args.url, args.pages, args.workers, args.timeout)
    
    if not jobs:
        print("[!] No se encontraron convocatorias.")
        return 0
        
    # Sort:
    # 1. Experience years (Ascending) - Lowest experience first
    # 2. Salary numeric (Descending) - Highest salary for same experience
    jobs.sort(key=lambda x: (x["experience_years"], -x["salary_numeric"]))
    
    # Export to CSV
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    
    fieldnames = [
        "entity",
        "title",
        "experience_years",
        "salary_numeric",
        "vacancies",
        "location",
        "ends_date",
        "degree_basic",
        "academic_degree",
        "experience_general",
        "experience_specific",
        "detail_url"
    ]
    
    try:
        with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            for job in jobs:
                writer.writerow(job)
                
        print(f"\n[OK] Scraping completado en {time.time() - start_time:.1f}s")
        print(f"[*] Total convocatorias guardadas: {len(jobs)}")
        print(f"[*] Archivo Excel/CSV generado en: {args.output}")
        
    except Exception as e:
        print(f"[ERROR] No se pudo guardar el archivo CSV: {e}", file=sys.stderr)
        return 1
        
    return 0


if __name__ == "__main__":
    sys.exit(main())
