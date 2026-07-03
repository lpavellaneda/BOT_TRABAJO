# Urbania Scraper (Python)

Proyecto base para extraer anuncios de Urbania y exportarlos a CSV o JSON.

## Requisitos

- Python 3.10+

## Instalacion

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

## Configuracion

1. Copia el ejemplo de variables:

```powershell
Copy-Item .env.example .env
```

2. Ajusta los valores en `.env`.

## Uso

```powershell
python src/main.py --url "https://urbania.pe/buscar/venta-de-departamentos?keyword=lima" --pages 1 --max-results 1 --format csv --output output/urbania.csv
```

Para proyectos con seleccion de unidades, usa navegador automatizado para que carguen los enlaces finales:

```powershell
python src/main.py --url "https://urbania.pe/buscar/venta-de-departamentos?keyword=lima" --pages 1 --max-results 1 --engine playwright --format csv --output output/urbania.csv
```

Si obtienes 0 resultados, ejecuta en modo visible para resolver challenge/captcha manualmente:

```powershell
python src/main.py --url "https://urbania.pe/buscar/venta-de-departamentos?keyword=lima" --pages 1 --max-results 1 --engine playwright --headed --format csv --output output/urbania.csv
```

Puedes ampliar el tiempo para resolver challenge/captcha con:

```powershell
python src/main.py --url "https://urbania.pe/buscar/venta-de-departamentos?keyword=lima" --pages 1 --max-results 1 --engine playwright --headed --challenge-wait 120 --format csv --output output/urbania.csv
```

Para forzar confirmacion manual desde terminal (recomendado):

```powershell
python src/main.py --url "https://urbania.pe/buscar/venta-de-departamentos?keyword=lima" --pages 1 --max-results 1 --engine playwright --headed --manual-continue --challenge-wait 180 --browser-channel msedge --format csv --output output/urbania.csv
```

Cuando veas Edge abierto, completa cookies/captcha y vuelve a la terminal para presionar Enter. Si el perfil ya quedo marcado por Cloudflare, prueba con un perfil nuevo:

```powershell
python src/main.py --url "https://urbania.pe/buscar/venta-de-departamentos?keyword=lima" --pages 1 --max-results 1 --engine playwright --headed --manual-continue --challenge-wait 180 --fresh-profile --browser-channel msedge --user-data-dir output/edge-profile --format csv --output output/urbania.csv
```

Si Edge abierto por Playwright sigue mostrando el aviso de software automatizado, abre Edge manualmente con CDP:

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="C:\Users\Leonardo\Documents\urbania scrapper\output\edge-cdp-profile"
```

Luego ejecuta el scraper contra esa sesion:

```powershell
python src/main.py --url "https://urbania.pe/buscar/venta-de-departamentos?keyword=lima" --pages 1 --max-results 1 --engine playwright --manual-continue --challenge-wait 180 --cdp-url http://127.0.0.1:9222 --format csv --output output/urbania.csv
```

Si Urbania bloquea las paginas de detalle, puedes evitar ese paso y exportar los datos visibles del listado:

```powershell
python src/main.py --url "https://urbania.pe/buscar/venta-de-departamentos?keyword=lima" --pages 1 --max-results 1 --engine playwright --skip-details --format csv --output output/urbania.csv
```

No uses `--skip-details` si necesitas las unidades finales dentro del proyecto. En ese caso usa modo visible/manual y resuelve el challenge en la ventana de Edge que abre Playwright:

```powershell
python src/main.py --url "https://urbania.pe/buscar/venta-de-departamentos?keyword=lima" --pages 1 --max-results 1 --engine playwright --headed --manual-continue --challenge-wait 180 --browser-channel msedge --format csv --output output/urbania.csv
```

Campos exportados:

- title
- price
- location
- area_m2
- bedrooms
- bathrooms
- parking
- advertiser
- description
- detail_url

## Nota

Este scraper es de uso educativo. Respeta siempre los terminos de uso y `robots.txt` del sitio.
Urbania puede activar Cloudflare (challenge anti-bot); en ese caso la extraccion automatizada puede quedar bloqueada.
