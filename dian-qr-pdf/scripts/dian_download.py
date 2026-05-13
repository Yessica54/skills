import os
import time
from scrapling.fetchers import StealthyFetcher

TARGET_URL = (
    "https://catalogo-vpfe.dian.gov.co/document/searchqr"
    "?documentkey=76b847b40460853840631faa0345be2327fccde0f2d45a2956279749bc07df1a6b08486a4fb46ac75da0424d92f2b51d"
)

OUTPUT_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "factura_dian.pdf")
)

_result = {"bytes": None, "error": None}


def _intercept(route):
    try:
        response = route.fetch()
        body = response.body()
        if body[:4] == b"%PDF" or response.headers.get("content-type", "").startswith("application/pdf"):
            _result["bytes"] = body
            print(f"[route] PDF captured: {len(body):,} bytes")
            # Return a blank page so scrapling can still collect a response
            route.fulfill(status=200, content_type="text/html", body=b"<html><body>ok</body></html>")
        else:
            print(f"[route] Unexpected content-type: {response.headers.get('content-type')} status={response.status}")
            route.continue_()
    except Exception as e:
        print(f"[route] Error: {e}")
        route.continue_()


def download_action(page):
    # Intercept the PDF POST before clicking
    page.route("**/Document/DownloadPDF", _intercept)

    # Wait for the embedded Turnstile captcha token (up to 45s)
    print("[action] Waiting for Turnstile captcha on download form...")
    captcha_ready = False
    for i in range(90):
        try:
            val = page.locator('input[name="captcha"]').first.get_attribute("value", timeout=500) or ""
            if val:
                print(f"[action] Captcha ready ({len(val)} chars)")
                captcha_ready = True
                break
        except Exception:
            pass
        if i % 10 == 0:
            print(f"[action] Still waiting... {i * 0.5:.0f}s")
        time.sleep(0.5)

    if not captcha_ready:
        print("[action] Captcha not detected — proceeding anyway (may fail server-side)")

    # Click the download link — JS will fill captcha + submit the form
    print("[action] Clicking Descargar PDF...")
    page.locator(".downloadLink").first.click()

    # Wait for the route handler to capture the PDF
    print("[action] Waiting for PDF response...")
    for _ in range(60):
        if _result["bytes"]:
            break
        time.sleep(0.5)

    if _result["bytes"]:
        with open(OUTPUT_PATH, "wb") as f:
            f.write(_result["bytes"])
        print(f"[action] Saved: {OUTPUT_PATH}")
    else:
        print("[action] PDF not captured — check browser or captcha state")


StealthyFetcher.fetch(
    TARGET_URL,
    solve_cloudflare=True,
    headless=False,
    network_idle=True,
    page_action=download_action,
    timeout=120,
)

if _result["bytes"]:
    size = os.path.getsize(OUTPUT_PATH)
    print(f"\nDone! PDF saved to: {OUTPUT_PATH} ({size:,} bytes)")
else:
    print("\nFailed to download PDF. Check console output above.")
