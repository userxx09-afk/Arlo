"""Sign in with Google (OpenID Connect, the authorization-code flow).

The ID token comes straight from Google's token endpoint over HTTPS, in exchange for the code
and our client secret, so (as OpenID Connect allows) its signature isn't checked again here;
its issuer, audience, expiry, nonce and verified email are.
"""
import base64
import json
import time
from urllib.parse import urlencode

import requests

from . import users

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class SignInError(Exception):
    pass


def auth_url(state, nonce):
    g = users.google()
    return AUTH_URL + "?" + urlencode({
        "client_id": g["client_id"], "redirect_uri": users.redirect_uri(), "response_type": "code",
        "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account"})


def _claims(id_token):
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        raise SignInError("Google sent back something BCN can't read.")


def exchange(code, nonce):
    """The code Google gave the browser -> {"email", "name"} of a verified Google account."""
    g = users.google()
    try:
        r = requests.post(TOKEN_URL, timeout=15, data={
            "code": code, "client_id": g["client_id"], "client_secret": g["client_secret"],
            "redirect_uri": users.redirect_uri(), "grant_type": "authorization_code"})
    except requests.RequestException as e:
        raise SignInError(f"Couldn't reach Google: {e}")
    if r.status_code != 200:
        try:
            why = r.json().get("error_description") or r.json().get("error")
        except ValueError:
            why = r.text[:200]
        raise SignInError(f"Google turned the sign-in down ({why}).")
    c = _claims(r.json().get("id_token") or "")
    if c.get("iss") not in ISSUERS or c.get("aud") != g["client_id"]:
        raise SignInError("That sign-in wasn't meant for BCN.")
    if int(c.get("exp") or 0) < time.time():
        raise SignInError("That sign-in expired. Try again.")
    if not nonce or c.get("nonce") != nonce:
        raise SignInError("That sign-in didn't start here. Try again.")
    if not c.get("email") or c.get("email_verified") not in (True, "true"):
        raise SignInError("That Google account has no verified email address.")
    return {"email": c["email"].lower(), "name": c.get("name") or c["email"]}
