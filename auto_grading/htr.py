"""Reconnaissance d'écriture manuscrite (HTR) via Claude Vision.

Deux voies, détection auto par ordre de préférence :
1. **API key** (extra `[api]` + `config.anthropic_api_key`/env) : appel direct
   au SDK `anthropic` avec image en base64. Rapide, ~0.1¢/copie avec Haiku.
2. **Claude Code subprocess** (binaire `claude` dans le PATH ou env
   `CLAUDE_CODE_EXECPATH`) : spawn le CLI Claude Code en lui passant un
   path vers le crop JPG temporaire ; utilise l'auth OAuth (abonnement
   Pro/Max), pas de coût $. Lent (~3-5s/copie). Pratique pour les users
   sans clé API mais authentifiés via Claude Code.

Sans aucune des deux → `is_available()=False` → boutons UI désactivés.

Deux entrées principales :
- `recognize_name(image_rgb, candidates)` : auto-id par nom manuscrit, match
  contre une liste fermée de N candidats étudiants (utilisé par `/identites`).
- `recognize_text(image_rgb)` : OCR libre (utilisé par Feature B
  `question_freeform` pour lire les cases ouvertes).
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from pathlib import Path

DEFAULT_MODEL = "claude-haiku-4-5"
INSTALL_HINT_NO_KEY = (
    "pose une clé API dans Réglages > IA (dashboard ⚙) "
    "ou installe Claude Code (https://claude.com/claude-code) puis "
    "authentifie-toi"
)
INSTALL_HINT = INSTALL_HINT_NO_KEY  # alias rétrocompat


def _api_key() -> str:
    """Lit la clé API : config.json puis env. Retourne '' si rien."""
    try:
        import config
        k = (config.load_config().get("anthropic_api_key") or "").strip()
        if k:
            return k
    except Exception:
        pass
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def _model_id() -> str:
    """Modèle Claude pour le HTR : `config.ai_model_htr` puis défaut."""
    try:
        import config
        cfg = config.load_config()
        return (cfg.get("ai_model_htr") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    except Exception:
        return DEFAULT_MODEL


def _claude_code_path() -> str:
    """Cherche le binaire `claude` (Claude Code CLI).

    Précédence : env `CLAUDE_CODE_EXECPATH` (extension VSCode) → `which claude` →
    extensions VSCode standards. Retourne "" si rien trouvé.
    """
    env = (os.environ.get("CLAUDE_CODE_EXECPATH") or "").strip()
    if env and Path(env).is_file():
        return env
    import shutil as _sh
    w = _sh.which("claude")
    if w:
        return w
    home = Path.home()
    for pat in (
        ".vscode/extensions/anthropic.claude-code-*-linux-x64/resources/native-binary/claude",
        ".vscode/extensions/anthropic.claude-code-*-darwin-*/resources/native-binary/claude",
    ):
        for p in home.glob(pat):
            if p.is_file():
                return str(p)
    return ""


def _backend() -> str:
    """Retourne 'api' (clé API + SDK), 'cc' (Claude Code subprocess), ou ''."""
    if _api_key():
        try:
            import anthropic  # noqa: F401
            return "api"
        except ImportError:
            pass
    if _claude_code_path():
        return "cc"
    return ""


def is_available() -> bool:
    """True si l'une des deux voies (API ou CC subprocess) est disponible."""
    return bool(_backend())


def status() -> dict:
    """Snapshot pour `/api/htr/status`."""
    try:
        import anthropic  # noqa: F401
        sdk_ok = True
    except ImportError:
        sdk_ok = False
    has_key = bool(_api_key())
    cc_path = _claude_code_path()
    backend = _backend()
    return {
        "available":     bool(backend),
        "backend":       backend,        # "api" | "cc" | ""
        "sdk_installed": sdk_ok,
        "has_api_key":   has_key,
        "has_claude_code": bool(cc_path),
        "claude_code_path": cc_path,
        "model_id":      _model_id(),
        "install_hint":  "" if backend else INSTALL_HINT_NO_KEY,
    }


def _image_to_b64(image_rgb) -> str:
    """RGB array → base64 JPEG (compact pour Claude, quality 90)."""
    import cv2
    bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR) if image_rgb.ndim == 3 else image_rgb
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise RuntimeError("htr: cv2.imencode KO")
    return base64.standard_b64encode(buf.tobytes()).decode("ascii")


def _claude_vision_api(image_rgb, prompt: str, max_tokens: int = 200) -> tuple[str, dict]:
    """Voie 1 : appel direct API via SDK anthropic."""
    from anthropic import Anthropic
    client = Anthropic(api_key=_api_key())
    img_b64 = _image_to_b64(image_rgb)
    resp = client.messages.create(
        model=_model_id(),
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    text = ""
    for blk in (resp.content or []):
        if getattr(blk, "type", "") == "text":
            text = (blk.text or "").strip()
            break
    usage = {}
    if resp.usage:
        usage = {
            "input_tokens": getattr(resp.usage, "input_tokens", 0) or 0,
            "output_tokens": getattr(resp.usage, "output_tokens", 0) or 0,
        }
    return text, usage


def _claude_vision_cc(image_rgb, prompt: str, max_tokens: int = 200,
                     timeout_s: int = 90) -> tuple[str, dict]:
    """Voie 2 : spawn Claude Code CLI avec un path vers le crop sur disque.

    CC lit l'image via son tool Read (qu'on autorise explicitement, contrairement
    à l'édition IA qui désactive tout). Plus lent (~3-5s/copie) mais utilise
    l'auth OAuth (abonnement) au lieu d'une clé API.
    """
    import cv2
    import subprocess as _sp
    cc = _claude_code_path()
    if not cc:
        raise RuntimeError("Claude Code introuvable (binaire `claude` absent)")
    # Crop temporaire — CC accède au file via son tool Read.
    bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR) if image_rgb.ndim == 3 else image_rgb
    fd, tmp_path = tempfile.mkstemp(prefix="htr_", suffix=".jpg")
    os.close(fd)
    try:
        cv2.imwrite(tmp_path, bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])
        # Le prompt référence le fichier explicitement → CC le lit avec Read.
        full_prompt = (
            f"Read the image at {tmp_path} and then: {prompt}\n\n"
            "Reply with ONLY the answer, no explanation, no preamble."
        )
        cmd = [
            cc,
            "--print",
            "--output-format", "json",
            # Read autorisé (pour ouvrir le JPG). Tout le reste désactivé.
            "--disallowed-tools", "Bash", "Write", "Edit", "Grep",
            "Glob", "NotebookEdit", "WebFetch", "WebSearch", "TodoWrite",
            "Agent", "ExitPlanMode", "ScheduleWakeup",
            "--model", _model_id(),
            "-p", full_prompt,
        ]
        try:
            proc = _sp.run(cmd, capture_output=True, timeout=timeout_s,
                           text=True, cwd="/tmp")
        except _sp.TimeoutExpired:
            raise RuntimeError(f"Claude Code timeout après {timeout_s}s")
        if proc.returncode != 0:
            raise RuntimeError(
                f"Claude Code rc={proc.returncode} : "
                f"{(proc.stderr or '').strip()[-300:]}")
        try:
            result = json.loads(proc.stdout)
        except Exception as e:
            raise RuntimeError(f"CC sortie non-JSON : {e}")
        text = (result.get("result") or "").strip()
        usage = result.get("usage") or {}
        # Normalise usage pour _record_ai_usage (input_tokens/output_tokens).
        norm_usage = {
            "input_tokens":  usage.get("input_tokens", 0) or 0,
            "output_tokens": usage.get("output_tokens", 0) or 0,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens":     usage.get("cache_read_input_tokens", 0) or 0,
            "_cost_usd": result.get("total_cost_usd"),
        }
        return text, norm_usage
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _claude_vision(image_rgb, prompt: str, max_tokens: int = 200) -> tuple[str, dict]:
    """Dispatch API → CC selon ce qui est dispo. Retourne (text, usage_dict)."""
    backend = _backend()
    if backend == "api":
        return _claude_vision_api(image_rgb, prompt, max_tokens)
    if backend == "cc":
        return _claude_vision_cc(image_rgb, prompt, max_tokens)
    raise RuntimeError(f"htr indisponible — {INSTALL_HINT_NO_KEY}")


def recognize_text(image_rgb, lang_hint: str | None = None) -> dict:
    """OCR libre du contenu manuscrit d'une image. Pour Feature B
    (`question_freeform` cases libres).

    Retourne `{text, confidence, usage}`. La `confidence` est heuristique
    (1.0 si la réponse n'est pas vide, 0.0 sinon) — Claude ne renvoie pas
    de proba calibrée.
    """
    hint = f" Le texte attendu est en {lang_hint}." if lang_hint else ""
    prompt = (
        "Transcribe the handwritten text in this image exactly as written."
        + hint
        + " Reply with ONLY the transcribed text, no quotes, no explanation."
    )
    text, usage = _claude_vision(image_rgb, prompt, max_tokens=120)
    return {
        "text": text,
        "confidence": 1.0 if text else 0.0,
        "usage": usage,
    }


def recognize_name(image_rgb, candidates: list[dict]) -> dict:
    """Identifie un nom manuscrit dans une liste fermée de candidats étudiants.

    `candidates = [{id, full}, …]`. Claude reçoit le crop + la liste numérotée,
    et renvoie l'index du candidat. On parse, on map.

    Retourne `{best_id, best_full, confidence, raw_text, usage}` —
    `best_id=None` si Claude répond hors-range ou de manière incompréhensible.
    """
    if not candidates:
        return {"best_id": None, "best_full": None, "confidence": 0.0,
                "raw_text": "", "usage": {}}
    lines = [f"{i + 1}. {c['full']}" for i, c in enumerate(candidates)]
    candidate_list = "\n".join(lines)
    prompt = (
        "This image shows a handwritten student name (last name in capitals, "
        "first name in lowercase, French handwriting). Match it to ONE of "
        "the students below.\n\n"
        + candidate_list
        + "\n\nReply with ONLY the number of the matching student "
          "(an integer between 1 and "
        + str(len(candidates))
        + "). If you cannot tell, reply with 0."
    )
    raw, usage = _claude_vision(image_rgb, prompt, max_tokens=20)
    m = re.search(r"\d+", raw or "")
    idx = int(m.group(0)) if m else 0
    if 1 <= idx <= len(candidates):
        c = candidates[idx - 1]
        return {
            "best_id":    c["id"],
            "best_full":  c["full"],
            "confidence": 1.0,   # placeholder ; on n'a pas de proba
            "raw_text":   raw,
            "usage":      usage,
        }
    return {
        "best_id":    None,
        "best_full":  None,
        "confidence": 0.0,
        "raw_text":   raw,
        "usage":      usage,
    }


def crop_zone(warped_gray, zone: tuple, margin_px: int = 18,
              v_band: tuple[float, float] | None = (0.30, 0.72)):
    """Crop d'une zone canonique 300dpi → array RGB pour le HTR.

    `zone = (xmin, xmax, ymin, ymax)` au format `cv_grade.load_name_field()`.
    `v_band = (top_frac, bot_frac)` : restreint la zone verticalement (defaut :
    30%-72% ≈ ligne du milieu où l'étudiant écrit, pour éviter le label imprimé
    en haut et la ligne pointillée vide en bas). Passer `None` pour conserver
    toute la hauteur.

    Note : Claude Vision est multi-line robuste, donc `v_band` est moins
    critique qu'avec TrOCR. On garde quand même le défaut pour focaliser sur
    l'écriture (moins de tokens d'image facturés).
    """
    import numpy as np
    import cv2
    xmin, xmax, ymin, ymax = zone
    h_w, w_w = warped_gray.shape[:2]
    x0 = max(0, int(xmin) - margin_px)
    y0 = max(0, int(ymin) - margin_px)
    x1 = min(w_w, int(xmax) + margin_px)
    y1 = min(h_w, int(ymax) + margin_px)
    crop = warped_gray[y0:y1, x0:x1]
    if v_band is not None:
        h = crop.shape[0]
        crop = crop[int(h * v_band[0]):int(h * v_band[1]), :]
    if crop.ndim == 2:
        crop = cv2.cvtColor(crop, cv2.COLOR_GRAY2RGB)
    return np.ascontiguousarray(crop)


# --- Matching helpers (Feature B) ---------------------------------------

def _normalize_numeric(s: str) -> str | None:
    """Normalise un texte en nombre : virgule fr → point, strip."""
    if s is None:
        return None
    s2 = s.strip().replace(",", ".").replace(" ", "")
    if not s2:
        return None
    try:
        float(s2)
        return s2
    except ValueError:
        return None


def match_answer(ocr_text: str, expected, mode: str = "exact",
                 numeric_tol: float = 0.01) -> bool:
    """True si `ocr_text` matche `expected` selon `mode`.

    Modes : exact | numeric_tol | contains | regex.
    `expected` peut être un str ou une liste d'alternatives acceptées.
    """
    candidates = expected if isinstance(expected, (list, tuple)) else [expected]
    candidates = [c for c in candidates if c not in (None, "")]
    if not candidates:
        return False
    text = (ocr_text or "").strip()

    if mode == "exact":
        t = text.lower()
        return any((c or "").strip().lower() == t for c in candidates)

    if mode == "numeric_tol":
        got = _normalize_numeric(text)
        if got is None:
            return False
        try:
            got_v = float(got)
        except ValueError:
            return False
        for c in candidates:
            ref = _normalize_numeric(str(c))
            if ref is None:
                continue
            try:
                if abs(float(ref) - got_v) <= float(numeric_tol):
                    return True
            except ValueError:
                continue
        return False

    if mode == "contains":
        t = text.lower()
        return any((c or "").strip().lower() in t for c in candidates if c)

    if mode == "regex":
        for c in candidates:
            if not c:
                continue
            try:
                if re.search(c, text):
                    return True
            except re.error:
                continue
        return False

    return False
