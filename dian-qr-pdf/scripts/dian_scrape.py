"""
Usage:
  python dian_scrape.py '<url_o_cufe>' [output_path]

  url_o_cufe  — URL completa de DIAN o solo el CUFE (96 hex chars)
  output_path — Ruta del PDF de salida (opcional).
                Fallback: $DIAN_OUTPUT_DIR/<cufe8>.pdf
                Default:  ./factura_<cufe8>.pdf

Env vars:
  DIAN_OUTPUT_DIR  — Carpeta de salida para el PDF
  DIAN_DB_PATH     — Ruta del archivo SQLite (default: ./facturas_dian.db)
"""
import os
import re
import sys
import time
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from scrapling.fetchers import StealthyFetcher

# ── Parsear argumento de entrada ──────────────────────────────────────────────

def _parse_input(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("http"):
        qs = parse_qs(urlparse(raw).query)
        cufe = (qs.get("documentkey") or qs.get("DocumentKey") or [""])[0]
        if not cufe:
            raise ValueError(f"No se encontró 'documentkey' en la URL: {raw}")
    else:
        cufe = raw
    if len(cufe) != 96:
        raise ValueError(f"CUFE inválido ({len(cufe)} chars, se esperan 96): {cufe}")
    return cufe


def _resolve_output(cufe: str, arg: str | None) -> str:
    if arg:
        return os.path.abspath(arg)
    prefix = cufe[:8]
    base_dir = os.environ.get("DIAN_OUTPUT_DIR", ".")
    return os.path.abspath(os.path.join(base_dir, f"factura_{prefix}.pdf"))


# ── Helpers de parseo ─────────────────────────────────────────────────────────

def _parse_nit_nombre(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    nit    = next((l.split(":", 1)[1].strip() for l in lines if "NIT" in l), "")
    nombre = next((l.split(":", 1)[1].strip() for l in lines if "Nombre" in l), "")
    return nit, nombre


def _parse_money(text: str, label: str) -> str:
    for line in text.splitlines():
        if label in line and "$" in line:
            return line.split("$", 1)[1].strip().rstrip("\\n").strip()
    return ""


def _clean_text(t: str) -> str:
    """Normaliza texto: quita saltos de línea internos y espacios extra."""
    if not t:
        return ""
    return " ".join(t.replace("\n", " ").split())


# ── PDF: extracción de productos ──────────────────────────────────────────────

def _extract_productos(pdf_path: str) -> list[dict]:
    """Extrae la tabla de Detalles de Productos desde el PDF usando pdfplumber."""
    try:
        import pdfplumber
    except ImportError:
        print("PRODUCTOS_COUNT: 0", file=sys.stderr)
        print("PRODUCTOS_ERROR: pdfplumber no instalado", file=sys.stderr)
        return []

    productos: list[dict] = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    # Detectar si esta tabla es la de productos
                    if not table or len(table) < 2:
                        continue
                    # Header puede estar en fila 0 o fila 1 (header multilínea DIAN)
                    hdr0 = [_clean_text(str(c)) for c in table[0]]
                    hdr1 = [_clean_text(str(c)) for c in table[1]] if len(table) > 1 else []
                    # Buscar "Nro." y "Descripción" en ambas filas del header
                    found_nro = "Nro." in hdr0 or "Nro." in hdr1
                    found_desc = "Descripción" in hdr0 or "Descripción" in hdr1
                    if not found_nro or not found_desc:
                        continue
                    # Usar hdr1 como header principal si contiene las columnas clave
                    header = hdr1 if ("Nro." in hdr1 and "Descripción" in hdr1) else hdr0

                    # Mapear índices de columnas clave
                    idx = {}
                    for i, col in enumerate(header):
                        col_lower = col.lower()
                        if "nro." in col_lower or col in ("Nro.",):
                            idx["nro"] = i
                        elif "código" in col_lower or "codigo" in col_lower:
                            idx["codigo"] = i
                        elif "descripción" in col_lower or "descripcion" in col_lower:
                            idx["descripcion"] = i
                        elif col.strip() in ("U/M", "UM", "U/M:"):
                            idx["um"] = i
                        elif "cantidad" in col_lower:
                            idx["cantidad"] = i
                        elif "precio unitario" in col_lower and "venta" not in col_lower:
                            idx["precio_unitario"] = i
                        elif "descuento" in col_lower:
                            idx["descuento"] = i
                        elif "recargo" in col_lower:
                            idx["recargo"] = i
                        elif "iva" in col_lower and "%" not in col_lower:
                            idx["iva_valor"] = i
                        elif "iva %" in col_lower or ("iva" in col_lower and "%" in col_lower):
                            idx["iva_porcentaje"] = i
                        elif "inc" in col_lower and "%" not in col_lower:
                            idx["inc_valor"] = i
                        elif "inc %" in col_lower or ("inc" in col_lower and "%" in col_lower):
                            idx["inc_porcentaje"] = i
                        elif "precio" in col_lower and "venta" in col_lower:
                            idx["precio_venta"] = i

                    # El header puede estar en table[0] (hdr0) o table[1] (hdr1).
                    # Si el header real está en hdr1, saltamos las primeras 2 filas.
                    # Si está en hdr0 y hdr1 es header secundario (%/detalle/venta), saltamos 2.
                    # Si el header real está en hdr0 y hdr1 es dato, saltamos 1.
                    header_is_row1 = ("Nro." in hdr1 and "Descripción" in hdr1)

                    if header_is_row1:
                        # hdr1 es el header real, hdr0 es la primera línea del header multilínea
                        for j in range(len(header)):
                            col0 = _clean_text(str(table[0][j])) if len(table[0]) > j else ""
                            col1 = _clean_text(str(table[1][j])) if len(table[1]) > j else ""
                            combined = (col0 + " " + col1).lower()
                            if "iva %" in combined or ("iva" in combined and "%" in combined):
                                idx["iva_porcentaje"] = j
                            elif "iva" in combined and "%" not in combined and "iva_valor" not in idx:
                                idx["iva_valor"] = j
                            elif "inc %" in combined or ("inc" in combined and "%" in combined):
                                idx["inc_porcentaje"] = j
                            elif "inc" in combined and "%" not in combined and "inc_valor" not in idx:
                                idx["inc_valor"] = j
                            elif "precio" in combined and "venta" in combined:
                                idx["precio_venta"] = j
                        # Detectar columnas "%" sueltas por posición:
                        # la primera tras "IVA" → IVA%, la primera tras "INC" → INC%
                        if "iva_porcentaje" not in idx:
                            for j in range(len(header)):
                                if header[j].strip() == "%":
                                    for k in range(j - 1, -1, -1):
                                        if header[k].strip().upper() == "IVA":
                                            idx["iva_porcentaje"] = j
                                            break
                                    break
                        if "inc_porcentaje" not in idx:
                            for j in range(len(header)):
                                if header[j].strip() == "%" and j != idx.get("iva_porcentaje", -1):
                                    for k in range(j - 1, -1, -1):
                                        if header[k].strip().upper() == "INC":
                                            idx["inc_porcentaje"] = j
                                            break
                                    break
                        start = 2  # Saltar hdr0 y hdr1
                    else:
                        # hdr0 es el header real; ver si hdr1 es sub-header
                        has_second_header = (
                            len(table) > 1 and table[1]
                            and any(c and ("%" in str(c) or "detalle" in str(c).lower() or "venta" in str(c).lower()) for c in table[1])
                        )
                        if has_second_header:
                            for j in range(len(header)):
                                col0 = _clean_text(str(table[0][j])) if len(table[0]) > j else ""
                                col1 = _clean_text(str(table[1][j])) if len(table[1]) > j else ""
                                combined = (col0 + " " + col1).lower()
                                if "iva %" in combined:
                                    idx["iva_porcentaje"] = j
                                elif "iva" in combined and "%" not in combined and "iva_valor" not in idx:
                                    idx["iva_valor"] = j
                                elif "inc %" in combined:
                                    idx["inc_porcentaje"] = j
                                elif "inc" in combined and "%" not in combined and "inc_valor" not in idx:
                                    idx["inc_valor"] = j
                                elif "precio" in combined and "venta" in combined:
                                    idx["precio_venta"] = j
                            start = 2
                        else:
                            start = 1

                    # Si no se detectó precio_venta, usar última columna
                    if "precio_venta" not in idx and len(header) > 0:
                        idx["precio_venta"] = len(header) - 1

                    for row in table[start:]:
                        if not row or not row[0] or not str(row[0]).strip().isdigit():
                            continue
                        prod = {
                            "nro": _clean_text(str(row[idx["nro"]])) if "nro" in idx else "",
                            "codigo": _clean_text(str(row[idx["codigo"]])) if "codigo" in idx else "",
                            "descripcion": _clean_text(str(row[idx["descripcion"]])) if "descripcion" in idx else "",
                            "um": _clean_text(str(row[idx["um"]])) if "um" in idx else "",
                            "cantidad": _clean_text(str(row[idx["cantidad"]])) if "cantidad" in idx else "",
                            "precio_unitario": _clean_text(str(row[idx["precio_unitario"]])) if "precio_unitario" in idx else "",
                            "descuento": _clean_text(str(row[idx["descuento"]])) if "descuento" in idx else "",
                            "recargo": _clean_text(str(row[idx["recargo"]])) if "recargo" in idx else "",
                            "iva_valor": _clean_text(str(row[idx["iva_valor"]])) if "iva_valor" in idx else "",
                            "iva_porcentaje": _clean_text(str(row[idx["iva_porcentaje"]])) if "iva_porcentaje" in idx else "",
                            "inc_valor": _clean_text(str(row[idx["inc_valor"]])) if "inc_valor" in idx else "",
                            "inc_porcentaje": _clean_text(str(row[idx["inc_porcentaje"]])) if "inc_porcentaje" in idx else "",
                            "precio_venta": _clean_text(str(row[idx["precio_venta"]])) if "precio_venta" in idx else "",
                        }
                        productos.append(prod)

    except Exception as e:
        print(f"PRODUCTOS_COUNT: 0", file=sys.stderr)
        print(f"PRODUCTOS_ERROR: {e}", file=sys.stderr)
        return []

    return productos


# ── SQLite ────────────────────────────────────────────────────────────────────

def _save_to_db(db_path: str, cufe: str, state: dict, pdf_path: str, productos: list[dict]):
    db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS facturas (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            cufe            TEXT UNIQUE,
            emisor_nit      TEXT,
            emisor_nombre   TEXT,
            receptor_nit    TEXT,
            receptor_nombre TEXT,
            iva             TEXT,
            total           TEXT,
            pdf_path        TEXT,
            guardado_en     TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS productos (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            factura_id      INTEGER,
            cufe            TEXT,
            nro             TEXT,
            codigo          TEXT,
            descripcion     TEXT,
            um              TEXT,
            cantidad        TEXT,
            precio_unitario TEXT,
            descuento       TEXT,
            recargo         TEXT,
            iva_valor       TEXT,
            iva_porcentaje  TEXT,
            inc_valor       TEXT,
            inc_porcentaje  TEXT,
            precio_venta    TEXT,
            FOREIGN KEY (factura_id) REFERENCES facturas(id)
        )
    """)
    con.commit()

    existing = con.execute("SELECT id FROM facturas WHERE cufe = ?", (cufe,)).fetchone()
    if existing:
        con.close()
        print(f"DB_DUPLICATE: true")
        print(f"DB_ID: {existing[0]}")
        print(f"DB_PATH: {db_path}")
        print(f"PRODUCTOS_DB_SKIPPED: Factura duplicada")
        return

    now = datetime.now(timezone.utc).isoformat()
    cur = con.execute(
        """INSERT INTO facturas
           (cufe, emisor_nit, emisor_nombre, receptor_nit, receptor_nombre, iva, total, pdf_path, guardado_en)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            cufe,
            state["emisor_nit"],
            state["emisor_nombre"],
            state["receptor_nit"],
            state["receptor_nombre"],
            state["iva"],
            state["total"],
            pdf_path,
            now,
        ),
    )
    factura_id = cur.lastrowid

    # Insertar productos
    productos_insertados = 0
    for prod in productos:
        con.execute(
            """INSERT INTO productos
               (factura_id, cufe, nro, codigo, descripcion, um, cantidad,
                precio_unitario, descuento, recargo, iva_valor, iva_porcentaje,
                inc_valor, inc_porcentaje, precio_venta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                factura_id, cufe,
                prod["nro"], prod["codigo"], prod["descripcion"], prod["um"],
                prod["cantidad"], prod["precio_unitario"], prod["descuento"],
                prod["recargo"], prod["iva_valor"], prod["iva_porcentaje"],
                prod["inc_valor"], prod["inc_porcentaje"], prod["precio_venta"],
            ),
        )
        productos_insertados += 1

    con.commit()
    con.close()
    print(f"DB_ID: {factura_id}")
    print(f"DB_PATH: {db_path}")
    print(f"PRODUCTOS_DB_COUNT: {productos_insertados}")


# ── Lógica de scraping ────────────────────────────────────────────────────────

_state: dict = {
    "emisor_nit": "", "emisor_nombre": "",
    "receptor_nit": "", "receptor_nombre": "",
    "iva": "", "total": "",
    "pdf": None,
}


def _intercept_pdf(route):
    try:
        response = route.fetch()
        body = response.body()
        content_type = response.headers.get("content-type", "")
        if body[:4] == b"%PDF" or "pdf" in content_type:
            _state["pdf"] = body
        else:
            print(f"[route] Respuesta inesperada — content-type: {content_type}", file=sys.stderr)
        route.fulfill(status=200, content_type="text/html", body=b"<html><body>ok</body></html>")
    except Exception as e:
        print(f"[route] Error: {e}", file=sys.stderr)
        route.continue_()


def _wait_turnstile(page, label=""):
    for i in range(60):
        try:
            val = page.locator('input[name="cf-turnstile-response"]').first.get_attribute("value", timeout=500) or ""
            if val:
                return
        except Exception:
            pass
        time.sleep(0.5)


def _make_action():
    def action(page):
        # Esperar Turnstile (SearchDocument o ShowDocumentToPublic)
        _wait_turnstile(page)

        # Clic en Buscar solo si estamos en SearchDocument
        try:
            page.locator("button.search-document").first.wait_for(state="visible", timeout=5000)
            page.locator("button.search-document").first.click()
        except Exception:
            pass  # Ya en ShowDocumentToPublic (redirección directa)

        # Esperar contenido del documento (AJAX)
        page.locator("span.datos-receptor").first.wait_for(state="visible", timeout=30000)

        # Extraer los tres bloques: emisor, receptor, totales
        blocks = page.locator("span.datos-receptor").all()
        if len(blocks) >= 1:
            emisor_text = blocks[0].locator("..").inner_text()
            _state["emisor_nit"], _state["emisor_nombre"] = _parse_nit_nombre(emisor_text)
        if len(blocks) >= 2:
            receptor_text = blocks[1].locator("..").inner_text()
            _state["receptor_nit"], _state["receptor_nombre"] = _parse_nit_nombre(receptor_text)
        # TOTALES no usa span.datos-receptor — extraer con regex del body
        body_text = page.inner_text("body")
        iva_m = re.search(r'IVA:\s*\$([^\n\r]+)', body_text)
        tot_m = re.search(r'Total:\s*\$([^\n\r]+)', body_text)
        _state["iva"]   = iva_m.group(1).strip() if iva_m else ""
        _state["total"] = tot_m.group(1).strip() if tot_m else ""

        # Esperar Turnstile del form de descarga
        _wait_turnstile(page)

        # Interceptar y descargar PDF
        page.route("**/Document/DownloadPDF", _intercept_pdf)
        page.locator(".downloadLink").first.click()

        for _ in range(60):
            if _state["pdf"]:
                break
            time.sleep(0.5)

    return action


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cufe = _parse_input(sys.argv[1])
    output_path = _resolve_output(cufe, sys.argv[2] if len(sys.argv) > 2 else None)
    target_url = f"https://catalogo-vpfe.dian.gov.co/document/searchqr?documentkey={cufe}"

    StealthyFetcher.fetch(
        target_url,
        solve_cloudflare=True,
        headless=False,
        network_idle=True,
        page_action=_make_action(),
        timeout=90,
    )

    if not _state["pdf"]:
        print("ERROR: No se pudo descargar el PDF", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(_state["pdf"])

    # Salida estructurada (parseble por el skill)
    print(f"EMISOR_NIT: {_state['emisor_nit']}")
    print(f"EMISOR_NOMBRE: {_state['emisor_nombre']}")
    print(f"RECEPTOR_NIT: {_state['receptor_nit']}")
    print(f"RECEPTOR_NOMBRE: {_state['receptor_nombre']}")
    print(f"IVA: {_state['iva']}")
    print(f"TOTAL: {_state['total']}")
    print(f"PDF_PATH: {output_path}")
    print(f"PDF_SIZE: {len(_state['pdf'])}")

    # Extraer productos del PDF
    productos = _extract_productos(output_path)
    print(f"PRODUCTOS_COUNT: {len(productos)}")
    for i, prod in enumerate(productos):
        print(f"PRODUCTO_{i+1}_NRO: {prod['nro']}")
        print(f"PRODUCTO_{i+1}_CODIGO: {prod['codigo']}")
        print(f"PRODUCTO_{i+1}_DESC: {prod['descripcion']}")
        print(f"PRODUCTO_{i+1}_UM: {prod['um']}")
        print(f"PRODUCTO_{i+1}_CANT: {prod['cantidad']}")
        print(f"PRODUCTO_{i+1}_PRECIO_UNIT: {prod['precio_unitario']}")
        print(f"PRODUCTO_{i+1}_IVA: {prod['iva_valor']}")
        print(f"PRODUCTO_{i+1}_IVA_PCT: {prod['iva_porcentaje']}")
        print(f"PRODUCTO_{i+1}_TOTAL: {prod['precio_venta']}")

    # Guardar en base de datos
    db_path = os.environ.get("DIAN_DB_PATH", "./facturas_dian.db")
    _save_to_db(db_path, cufe, _state, output_path, productos)


if __name__ == "__main__":
    main()
