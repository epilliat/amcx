"""Authentification Supabase pour la banque AMCx en ligne.

Flot **OTP code** (6 chiffres par email) :
1. `send_otp(email)` → Supabase envoie un mail avec un code à 6 chiffres.
2. L'user entre le code dans l'UI.
3. `verify_otp(email, code)` → reçoit access_token + refresh_token,
   les persiste dans config.json.
4. `refresh_token_if_possible()` → renouvelle le JWT avant expiration.

On évite volontairement le flot "magic link cliquable" qui nécessite de
parser un URL fragment côté JS + configurer un redirect URL côté Supabase
— le code à 6 chiffres marche partout sans config additionnelle.

Auth aussi simple que possible : pas de PKCE, pas de session côté serveur.
Le JWT user dort dans `config.json` et est envoyé en `Authorization: Bearer`
à chaque appel `bank_online._request()`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import config  # noqa: E402


class BankAuthError(RuntimeError):
    pass


def _supabase_url_and_anon() -> tuple[str, str]:
    entry = config.active_bank_cfg()
    url = (entry.get("supabase_url") or "").rstrip("/")
    anon = entry.get("supabase_anon_key") or ""
    if not (url and anon):
        raise BankAuthError("Banque en ligne non configurée (URL + clé anon manquantes).")
    return url, anon


def _post_auth(path: str, body: dict) -> dict:
    """POST vers {supabase}/auth/v1/<path>. Retourne le JSON décodé."""
    base, anon = _supabase_url_and_anon()
    req = Request(
        base + "/auth/v1" + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "apikey":       anon,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            msg = err.get("error_description") or err.get("msg") or err.get("error") or str(err)
        except Exception:
            msg = f"HTTP {e.code}"
        # Cas spécial : signup désactivé côté Supabase (mode invite-only)
        low = msg.lower()
        if "signup" in low and ("not allowed" in low or "disabled" in low):
            msg = ("Cette banque est en mode invite-only. Demande à l'admin "
                   "de t'inviter via le dashboard Supabase.")
        raise BankAuthError(f"Supabase auth : {msg}") from e
    except URLError as e:
        raise BankAuthError(f"Réseau injoignable : {e.reason}") from e


# --------------------------------------------------------------------------
# OTP code flow
# --------------------------------------------------------------------------

def send_otp(email: str) -> dict:
    """Envoie un code à 6 chiffres à `email`. Le user le saisit ensuite via
    `verify_otp()`.

    Crée le compte si l'email n'existe pas ET que le signup est autorisé
    (Dashboard → Auth → Providers → Email → Enable email signups). Si
    l'instance est en mode invite-only, seuls les emails déjà invités
    (via Dashboard → Authentication → Users → Invite user) reçoivent un
    code ; les autres reçoivent une erreur claire.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise BankAuthError("Email invalide.")
    return _post_auth("/otp", {"email": email, "create_user": True})


def verify_otp(email: str, code: str) -> dict:
    """Vérifie le code OTP. Si succès : persiste access_token, refresh_token,
    user_id, user_email dans config.json. Retourne `{user_id, email, expires_at}`.

    Lève BankAuthError si code incorrect / expiré.
    """
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not (email and code):
        raise BankAuthError("Email et code requis.")
    resp = _post_auth("/verify", {"email": email, "token": code, "type": "email"})

    access = resp.get("access_token")
    refresh = resp.get("refresh_token")
    user = resp.get("user") or {}
    uid = user.get("id")
    if not (access and refresh and uid):
        raise BankAuthError("Réponse Supabase incomplète (pas de tokens).")

    config.update_active_bank({
        "user_token":       access,
        "refresh_token":    refresh,
        "user_id":          uid,
        "user_email":       user.get("email") or email,
        "token_expires_at": int(resp.get("expires_at", 0)),
    })
    return {
        "user_id":    uid,
        "email":      user.get("email") or email,
        "expires_at": resp.get("expires_at", 0),
    }


def logout() -> None:
    """Efface les tokens de la banque active. (Pas d'appel à Supabase /logout —
    inutile pour un JWT court (1h) et évite une req qui peut échouer.)"""
    config.update_active_bank({
        "user_token":       "",
        "refresh_token":    "",
        "user_id":          "",
        "user_email":       "",
        "token_expires_at": 0,
    })


# --------------------------------------------------------------------------
# Refresh
# --------------------------------------------------------------------------

def refresh_token_if_possible() -> bool:
    """Renouvelle access_token via refresh_token sur la banque active.
    Retourne True si succès, False sinon (l'user doit re-login).
    """
    entry = config.active_bank_cfg()
    refresh = entry.get("refresh_token") or ""
    if not refresh:
        return False
    try:
        base, anon = _supabase_url_and_anon()
        req = Request(
            base + "/auth/v1/token?grant_type=refresh_token",
            data=json.dumps({"refresh_token": refresh}).encode("utf-8"),
            method="POST",
            headers={
                "apikey":       anon,
                "Content-Type": "application/json",
                "Accept":       "application/json",
            },
        )
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError):
        return False

    access = data.get("access_token")
    new_refresh = data.get("refresh_token")
    user = data.get("user") or {}
    if not (access and new_refresh):
        return False
    config.update_active_bank({
        "user_token":       access,
        "refresh_token":    new_refresh,
        "user_id":          user.get("id") or entry.get("user_id") or "",
        "user_email":       user.get("email") or entry.get("user_email") or "",
        "token_expires_at": int(data.get("expires_at", 0)),
    })
    return True


# --------------------------------------------------------------------------
# Status (pour l'UI)
# --------------------------------------------------------------------------

def auth_status() -> dict:
    """Retourne `{configured, logged_in, user_id, email, expires_at}` pour
    la banque active."""
    entry = config.active_bank_cfg()
    is_online = (entry.get("type") == "online")
    configured = is_online and bool(entry.get("supabase_url") and entry.get("supabase_anon_key"))
    logged_in = is_online and bool(entry.get("user_token"))
    return {
        "configured": configured,
        "logged_in":  logged_in,
        "user_id":    entry.get("user_id") or "",
        "email":      entry.get("user_email") or "",
        "expires_at": int(entry.get("token_expires_at") or 0),
    }
