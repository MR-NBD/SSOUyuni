"""
uyuni_web_client.py — Login alla Web UI di UYUNI e restituzione cookie di sessione.

UYUNI usa un meccanismo anti-CSRF: la pagina di login contiene un csrf_token
nascosto da includere nella POST.

Flusso:
  1. GET /rhn/manager/login        → ottieni pagina HTML con csrf_token nascosto
  2. POST /rhn/manager/api/login   → credenziali + csrf_token → cookie di sessione
  3. Verifica JSESSIONID e pxt-session-cookie presenti
"""

import logging
import re

import requests

logger = logging.getLogger(__name__)

# Regex per estrarre csrf_token dall'HTML della pagina di login UYUNI.
# Pattern: csrf_token" value="<TOKEN>"
_CSRF_RE = re.compile(r'csrf_token"\s+value="([^"]+)"')

_TIMEOUT = 15   # secondi — allineato con _XMLRPC_TIMEOUT in uyuni_user_provisioner.py


def get_session_cookies(
    uyuni_url: str,
    username: str,
    password: str,
    *,
    verify: str | bool = False,
) -> dict[str, str]:
    """
    Esegue il login alla Web UI UYUNI e restituisce i cookie di sessione.

    verify: path al CA cert (str) oppure False per skip verifica SSL.
            Passato da app.py come _CA_CERT — non letto da env qui.

    Restituisce: {"JSESSIONID": "...", "pxt-session-cookie": "...", ...}
    Solleva RuntimeError se il login fallisce.
    """
    base    = uyuni_url.rstrip("/")
    session = requests.Session()
    session.verify = verify

    # Step 1: GET login page → estrai csrf_token
    # UYUNI rifiuta POST senza csrf_token valido (protezione CSRF lato server)
    resp = session.get(f"{base}/rhn/manager/login", timeout=_TIMEOUT)
    resp.raise_for_status()

    match = _CSRF_RE.search(resp.text)
    if not match:
        raise RuntimeError("CSRF token non trovato nella pagina di login UYUNI")
    csrf_token = match.group(1)

    # Step 2: POST credenziali
    # L'API interna UYUNI richiede csrf_token sia nel body JSON che nell'header
    resp = session.post(
        f"{base}/rhn/manager/api/login",
        json={"login": username, "password": password, "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()

    # Step 3: verifica risposta
    data = resp.json()
    if not data.get("success"):
        # Espone solo il campo message — non il dict completo che potrebbe contenere
        # dettagli interni di UYUNI (versione, stack trace, ecc.)
        msg = data.get("message") or "credenziali non valide"
        raise RuntimeError(f"Login UYUNI fallito per {username!r}: {msg}")

    cookies = {name: val for name, val in session.cookies.items()}

    required = {"pxt-session-cookie", "JSESSIONID"}
    missing  = required - cookies.keys()
    if missing:
        raise RuntimeError(f"Cookie di sessione mancanti dopo login: {missing}")

    logger.info("UYUNI web login OK: %r", username)
    return cookies