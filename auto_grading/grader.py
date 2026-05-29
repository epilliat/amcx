"""Appel Claude Opus 4.7 vision sur une image de copie -> JSON validé."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from answer_key import ANSWER_KEY
from vision_prompt import SYSTEM_PROMPT, USER_PROMPT

# Charge auto_grading/.env (à côté de ce fichier)
load_dotenv(Path(__file__).resolve().parent / ".env")

MODEL = "claude-opus-4-7"
MAX_TOKENS = 2000


def _b64_image(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("ascii")


def _extract_json(text: str) -> dict:
    """Robustement: si fence ```json ... ```, sinon premier { ... } équilibré."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        return json.loads(fence.group(1))
    # premier { jusqu'au } correspondant
    start = text.find("{")
    if start < 0:
        raise ValueError(f"Pas de JSON trouvé: {text[:200]!r}")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("JSON non équilibré")


def _validate(data: dict) -> tuple[dict, list[str]]:
    """Renvoie (data_nettoyé, warnings)."""
    warnings = []
    out = {
        "student_name": str(data.get("student_name", "")).strip(),
        "student_id": str(data.get("student_id", "")).strip(),
        "answers": {},
        "notes": str(data.get("notes", "")).strip(),
    }
    if len(out["student_id"]) != 4:
        warnings.append(f"student_id != 4 chiffres: {out['student_id']!r}")
    raw_answers = data.get("answers", {})
    for q, spec in ANSWER_KEY.items():
        key = str(q) if str(q) in raw_answers else q
        sel = raw_answers.get(key, [])
        if not isinstance(sel, list):
            warnings.append(f"Q{q} non-liste: {sel!r}")
            sel = []
        # nettoyer: majuscules, dans options autorisées
        cleaned = []
        for letter in sel:
            L = str(letter).upper().strip()
            if L in spec["options"]:
                cleaned.append(L)
            else:
                warnings.append(f"Q{q} lettre invalide {letter!r} (autorisé: {spec['options']})")
        out["answers"][q] = cleaned
    return out, warnings


_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def grade_image(image_path: Path, retries: int = 1) -> dict:
    """Envoie l'image au modèle, parse et valide le JSON.

    Retourne: {"student_name", "student_id", "answers", "notes", "warnings",
               "raw_text", "usage"}
    """
    client = get_client()
    img_b64 = _b64_image(image_path)
    media_type = "image/jpeg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"

    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                temperature=0,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": img_b64,
                                },
                            },
                            {"type": "text", "text": USER_PROMPT},
                        ],
                    }
                ],
            )
            raw_text = resp.content[0].text
            data = _extract_json(raw_text)
            cleaned, warnings = _validate(data)
            cleaned["warnings"] = warnings
            cleaned["raw_text"] = raw_text
            cleaned["usage"] = {
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cache_read_input_tokens": getattr(
                    resp.usage, "cache_read_input_tokens", 0
                ),
                "cache_creation_input_tokens": getattr(
                    resp.usage, "cache_creation_input_tokens", 0
                ),
            }
            return cleaned
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            if attempt < retries:
                continue
            raise RuntimeError(f"Parsing JSON échoué après {retries+1} tentatives: {e}") from e


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python grader.py <image.jpg>")
        sys.exit(1)
    r = grade_image(Path(sys.argv[1]))
    print(json.dumps(r, indent=2, ensure_ascii=False))
