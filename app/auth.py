"""Accounts: sign in with Google or with a one-time email code, restricted to organisation domains.

Configuration (environment / .env):
    ALLOWED_DOMAINS=vitstudent.ac.in,violetcloud.io   who may create an account (comma separated)
    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET            enables "Continue with Google"
    APP_URL=https://your-host                          public base URL used for the OAuth redirect
    SMTP_*                                             enables the email-code login (see mailer.py)
    ADMIN_EMAILS=a@x.org,b@x.org                       optional; shown as admins in /auth/me
    AUTH_DEV_CODE=1                                    dev only: print login codes to the server log
    SECRET_KEY                                         cookie signing key; auto-generated into DATA_DIR if unset

If neither Google nor SMTP is configured, authentication is disabled and the app is open (local demos).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .pipeline import mailer, storage

COOKIE = "ra_session"
SESSION_DAYS = 14
CODE_TTL_S = 10 * 60

router = APIRouter()


# ------------------------------------------------------------------ config
def allowed_domains() -> list[str]:
    raw = os.environ.get("ALLOWED_DOMAINS", "vitstudent.ac.in")
    return [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]


def google_configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def email_login_configured() -> bool:
    return mailer.configured() or os.environ.get("AUTH_DEV_CODE") == "1"


def auth_enabled() -> bool:
    return google_configured() or email_login_configured()


def admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def _secret() -> bytes:
    k = os.environ.get("SECRET_KEY")
    if k:
        return k.encode()
    p = storage.DATA_DIR / "secret.key"
    if not p.exists():
        storage.DATA_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_urlsafe(48))
    return p.read_text().strip().encode()


def domain_allowed(email: str) -> bool:
    email = (email or "").strip().lower()
    return "@" in email and email.rsplit("@", 1)[1] in allowed_domains()


# ------------------------------------------------------------------ session cookie (HMAC-signed)
def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def _verify(token: str) -> Optional[dict]:
    try:
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32], sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data if data.get("exp", 0) > time.time() else None
    except Exception:
        return None


def current_user(request: Request) -> Optional[dict]:
    if not auth_enabled():
        return None
    tok = request.cookies.get(COOKIE)
    data = _verify(tok) if tok else None
    if not data:
        return None
    u = storage.get_user(data.get("email", ""))
    if u:
        u["is_admin"] = u["email"] in admin_emails()
    return u


def _login(resp, email: str, name: str, picture: str = "", via: str = "email"):
    email = email.strip().lower()
    storage.upsert_user(email, name or email.split("@")[0], picture, email.rsplit("@", 1)[1])
    # make sure the person exists in the organisation directory so receipts attach to them
    if not storage.get_person(name or email):
        storage.add_person(name or email.split("@")[0], email, "", "")
    resp.set_cookie(COOKIE, _sign({"email": email, "via": via, "exp": time.time() + SESSION_DAYS * 86400}),
                    max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=_is_https())
    return resp


def _is_https() -> bool:
    return os.environ.get("APP_URL", "").startswith("https://")


def _base_url(request: Request) -> str:
    return os.environ.get("APP_URL") or str(request.base_url).rstrip("/")


# ------------------------------------------------------------------ routes
@router.get("/auth/config")
def auth_config():
    return {"enabled": auth_enabled(), "google": google_configured(), "email_code": email_login_configured(),
            "domains": allowed_domains()}


@router.get("/auth/me")
def me(request: Request):
    u = current_user(request)
    if not auth_enabled():
        return {"enabled": False, "user": None}
    if not u:
        raise HTTPException(401, "Not signed in")
    return {"enabled": True, "user": u}


@router.post("/auth/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# ---- Google
@router.get("/auth/google")
def google_start(request: Request):
    if not google_configured():
        raise HTTPException(404, "Google sign-in not configured")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"], "redirect_uri": f"{_base_url(request)}/auth/google/callback",
        "response_type": "code", "scope": "openid email profile", "state": state, "prompt": "select_account",
    }
    if len(allowed_domains()) == 1:
        params["hd"] = allowed_domains()[0]   # hint Google's chooser to that domain (still verified server-side)
    resp = RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))
    resp.set_cookie("ra_oauth_state", state, max_age=600, httponly=True, samesite="lax", secure=_is_https())
    return resp


@router.get("/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse("/login?error=" + (error or "cancelled"))
    if state != request.cookies.get("ra_oauth_state"):
        return RedirectResponse("/login?error=state")
    try:
        tok = requests.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": f"{_base_url(request)}/auth/google/callback", "grant_type": "authorization_code",
        }, timeout=20).json()
        info = requests.get("https://openidconnect.googleapis.com/v1/userinfo",
                            headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=20).json()
    except Exception:
        return RedirectResponse("/login?error=google")
    email = (info.get("email") or "").lower()
    if not info.get("email_verified") or not domain_allowed(email):
        return RedirectResponse("/login?error=domain&email=" + email)
    resp = RedirectResponse("/")
    resp.delete_cookie("ra_oauth_state")
    return _login(resp, email, info.get("name", ""), info.get("picture", ""), via="google")


# ---- Email one-time code
@router.post("/auth/email/start")
async def email_start(body: dict):
    if not email_login_configured():
        raise HTTPException(404, "Email sign-in not configured")
    email = (body.get("email") or "").strip().lower()
    if not domain_allowed(email):
        raise HTTPException(403, f"Only {', '.join('@' + d for d in allowed_domains())} addresses can sign in.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    storage.save_login_code(email, hashlib.sha256(code.encode()).hexdigest(), time.time() + CODE_TTL_S)
    if os.environ.get("AUTH_DEV_CODE") == "1":
        print(f"\n  [auth] login code for {email}: {code}\n")
    if mailer.configured():
        try:
            mailer.send_plain(email, "Your Receipt Agent sign-in code", f"Your sign-in code is {code}. It expires in 10 minutes.")
        except Exception as e:
            raise HTTPException(500, f"Could not send the code: {e}")
    return {"ok": True, "sent_to": email}


@router.post("/auth/email/verify")
async def email_verify(body: dict):
    email = (body.get("email") or "").strip().lower()
    code = (body.get("code") or "").strip()
    row = storage.get_login_code(email)
    if not row or row["expires_at"] < time.time() or row["attempts"] >= 5:
        raise HTTPException(400, "Code expired or too many attempts. Request a new one.")
    storage.bump_login_attempts(email)
    if not hmac.compare_digest(row["code_hash"], hashlib.sha256(code.encode()).hexdigest()):
        raise HTTPException(400, "Wrong code.")
    storage.delete_login_code(email)
    name = (body.get("name") or "").strip() or email.split("@")[0].replace(".", " ").title()
    return _login(JSONResponse({"ok": True}), email, name, via="email")


# ------------------------------------------------------------------ gate
OPEN_PREFIXES = ("/auth/", "/login", "/static/", "/api/health", "/favicon.ico")


def gate(request: Request) -> Optional[object]:
    """Return a response to short-circuit with, or None to let the request through."""
    if not auth_enabled():
        return None
    path = request.url.path
    if any(path.startswith(p) for p in OPEN_PREFIXES):
        return None
    if current_user(request):
        return None
    if path.startswith("/api/") or path.startswith("/samples/"):
        return JSONResponse({"detail": "Not signed in"}, status_code=401)
    return RedirectResponse("/login")
