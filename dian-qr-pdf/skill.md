---
name: dian-qr-pdf
description: Descarga el PDF de una factura electrónica colombiana leyendo el código QR de una imagen, extrae los detalles de productos del PDF, y guarda todos los datos (factura + productos) en SQLite. Activar cuando el usuario comparta una imagen con un QR de factura DIAN, mencione "descargar factura", "PDF de la factura", "QR de la DIAN", o quiera obtener el PDF de una factura electrónica colombiana a partir de su imagen.
---

# DIAN QR → PDF Downloader + Extractor de Productos

Lee el código QR de una imagen de factura electrónica colombiana para obtener la URL de la DIAN, descarga el PDF oficial, extrae los detalles de productos del PDF, y guarda todo en SQLite.

**Flujo directo: sin confirmación.** Se procede automáticamente tras leer el QR.

## Workflow

### Paso 1: Obtener la imagen

Si el usuario no ha compartido la imagen, pedirla. Si ya la compartió, usarla directamente.

### Paso 2: Leer el QR y extraer la URL/CUFE

Leer el código QR de la imagen. A partir del contenido del QR:

- Si es una **URL completa** (`https://catalogo-vpfe.dian.gov.co/document/searchqr?documentkey=...`): usarla directamente.
- Si es el **CUFE directamente** (96 caracteres hexadecimales): usarlo como argumento.
- Si el QR no es legible, pedir al usuario una imagen más nítida o que proporcione el CUFE manualmente.

### Paso 3: Ejecutar el script (sin confirmación previa)

Proceder directamente con la descarga. No pedir confirmación.

```bash
# Con URL completa (más común)
python <skill_path>/scripts/dian_scrape.py '<url_del_qr>'

# Con ruta de salida explícita
python <skill_path>/scripts/dian_scrape.py '<url_del_qr>' '<output_path>'

# Con solo el CUFE
python <skill_path>/scripts/dian_scrape.py '<cufe_96chars>'
```

El script maneja todo automáticamente:
1. Abre navegador visible y resuelve Cloudflare Turnstile
2. Hace clic en "Buscar" y espera los datos del documento
3. Extrae emisor, receptor, IVA y total desde la página
4. Resuelve el Turnstile del formulario de descarga
5. Intercepta el POST a `/Document/DownloadPDF` y guarda el PDF
6. Extrae la tabla de Detalles de Productos del PDF descargado
7. Guarda factura y productos en SQLite

**Ruta de salida configurable (prioridad descendente):**
1. Segundo argumento del script
2. `$DIAN_OUTPUT_DIR/factura_<cufe8>.pdf`
3. Default: `./factura_<cufe8>.pdf`

**Ruta de la DB configurable:**
- `$DIAN_DB_PATH` — default: `./facturas_dian.db`

### Paso 4: Mostrar resultado

Parsear el stdout del script (formato `CLAVE: valor`) y mostrar al usuario:

**Si exitoso (stdout contiene `PDF_PATH:`):**
```
Factura procesada
══════════════════════════════════════════════
EMISOR
  NIT:    [EMISOR_NIT]
  Nombre: [EMISOR_NOMBRE]

RECEPTOR
  NIT:    [RECEPTOR_NIT]
  Nombre: [RECEPTOR_NOMBRE]
──────────────────────────────────────────────
IVA:    $[IVA]
Total:  $[TOTAL]
══════════════════════════════════════════════
PDF: [PDF_PATH] ([PDF_SIZE] bytes)

Productos ([PRODUCTOS_COUNT])
──────────────────────────────────────────────
[NRO]. [DESC] — [CANT] [UM] × $[PRECIO_UNIT] = $[PRECIO_VENTA] [IVA: $[IVA_VALOR] ([IVA_PCT]%)]
...
══════════════════════════════════════════════
✅ Guardada en BD: [DB_PATH] (Factura ID #[DB_ID], [PRODUCTOS_DB_COUNT] productos)
```

**Si duplicada (stdout contiene `DB_DUPLICATE: true`):**
```
⚠️ Esta factura ya estaba en la base de datos (ID #[DB_ID])
```

**Si falla (script sale con código 1):**
```
No se pudo descargar el PDF.
[mensaje de error del stderr]
Posibles causas:
- La URL/CUFE es inválido
- El portal de la DIAN no está disponible
- El Cloudflare no pudo resolverse automáticamente
```

**Si no se encontraron productos (PRODUCTOS_COUNT: 0):**
- Mostrar igual la factura, pero indicar: `ℹ️ No se detectaron productos en el PDF.`

## Esquema SQLite

### Tabla `facturas`
| Columna | Tipo | Descripción |
|---|---|---|
| id | INTEGER PK | ID autoincremental |
| cufe | TEXT UNIQUE | CUFE 96 chars |
| emisor_nit | TEXT | NIT del emisor |
| emisor_nombre | TEXT | Razón social |
| receptor_nit | TEXT | NIT del receptor |
| receptor_nombre | TEXT | Nombre del receptor |
| iva | TEXT | Total IVA |
| total | TEXT | Total factura |
| pdf_path | TEXT | Ruta del PDF guardado |
| guardado_en | TEXT | Timestamp UTC |

### Tabla `productos`
| Columna | Tipo | Descripción |
|---|---|---|
| id | INTEGER PK | ID autoincremental |
| factura_id | INTEGER | FK → facturas.id |
| cufe | TEXT | CUFE de la factura |
| nro | TEXT | Número de ítem |
| codigo | TEXT | Código del producto |
| descripcion | TEXT | Descripción |
| um | TEXT | Unidad de Medida |
| cantidad | TEXT | Cantidad |
| precio_unitario | TEXT | Precio unitario |
| descuento | TEXT | Descuento |
| recargo | TEXT | Recargo |
| iva_valor | TEXT | Valor IVA del ítem |
| iva_porcentaje | TEXT | % IVA |
| inc_valor | TEXT | Valor INC |
| inc_porcentaje | TEXT | % INC |
| precio_venta | TEXT | Precio unitario de venta |

## Notas

- El portal de la DIAN puede estar lento fuera de horario laboral colombiano (UTC-5).
- Si el QR es pequeño o borroso, pedir al usuario una foto más cercana.
- Para configurar carpetas permanentes:
  ```bash
  export DIAN_OUTPUT_DIR=~/facturas
  export DIAN_DB_PATH=~/facturas/dian.db
  ```
- **Dependencias requeridas:**
  ```bash
  pip install scrapling playwright patchright pdfplumber
  patchright install chromium
  ```
- Para leer QR desde imágenes se requiere `zbar-tools` (apt) o `pyzbar` (pip).
