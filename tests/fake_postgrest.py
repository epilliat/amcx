"""Faux PostgREST en mémoire — le sous-ensemble utilisé par bank_online.py.

Permet de vérifier le CLIENT (invariants, requêtes émises, formes de retour)
sans instance Supabase. Ce qu'il ne couvre pas, et qui n'est vérifiable que sur
une vraie base : les policies RLS et le trigger `bank_categories_check_tree`.
Le client refait ces contrôles de son côté pour rendre un message lisible, donc
c'est bien cette couche-là qui est testée ici.
"""

import re
import uuid
from urllib.parse import parse_qsl, urlparse


class FakePostgrest:
    def __init__(self):
        self.tables = {"bank_categories": [], "question_categories": [],
                       "bank_questions": []}
        self.calls = []          # (method, table) de chaque requête émise

    # -- helpers de test ---------------------------------------------------
    def add_question(self, title, author_id="me", status="public", tags=None):
        qid = str(uuid.uuid4())
        self.tables["bank_questions"].append({
            "id": qid, "author_id": author_id, "kind": "question_qcm",
            "data": {}, "title": title, "tags": tags or [], "status": status,
            "version": 1, "source_project": "", "created_at": "2026-01-01",
            "modified_at": "2026-01-01"})
        return qid

    def wrote(self, table):
        return [m for m, t in self.calls if t == table and m != "GET"]

    # -- moteur ------------------------------------------------------------
    def request(self, method, path, *, body=None, params=None,
                extra_headers=None, retry_on_401=True):
        parsed = urlparse(path)
        table = parsed.path.rsplit("/", 1)[-1]
        q = dict(parse_qsl(parsed.query))
        q.update(params or {})
        self.calls.append((method, table))
        rows = self.tables.setdefault(table, [])

        if method == "GET":
            return [self._project(table, r) for r in self._match(rows, q)]
        if method == "POST":
            items = body if isinstance(body, list) else [body]
            out = []
            for item in items:
                item = dict(item)
                if table in ("bank_categories", "bank_questions"):
                    item.setdefault("id", str(uuid.uuid4()))
                    item.setdefault("created_at", "2026-01-01")
                    item.setdefault("modified_at", "2026-01-01")
                    # `resolution=ignore-duplicates` sur la clé primaire.
                    if any(r.get("id") == item["id"] for r in rows):
                        continue
                if table == "bank_categories":
                    item.setdefault("parent_id", None)
                    item.setdefault("position", 0)
                    # Index d'unicité (parent, lower(name)) — un conflit lève,
                    # il n'est PAS silencieusement ignoré.
                    key = (item.get("parent_id"), str(item.get("name", "")).strip().lower())
                    if any((r.get("parent_id"), str(r.get("name", "")).strip().lower()) == key
                           for r in rows):
                        raise RuntimeError(
                            'duplicate key value violates unique constraint '
                            '"bank_categories_sibling_name_idx"')
                if table == "question_categories":
                    key = (item["question_id"], item["category_id"])
                    if any((r["question_id"], r["category_id"]) == key for r in rows):
                        continue          # resolution=ignore-duplicates
                rows.append(item)
                out.append(item)
            return out
        if method == "PATCH":
            hit = self._match(rows, q)
            for r in hit:
                r.update(body or {})
            return list(hit)
        if method == "DELETE":
            for r in self._match(rows, q):
                rows.remove(r)
                if table == "bank_categories":
                    # on delete cascade
                    self.tables["question_categories"] = [
                        j for j in self.tables["question_categories"]
                        if j["category_id"] != r["id"]]
            return []
        raise AssertionError(method)

    def _match(self, rows, q):
        out = list(rows)
        for field, expr in q.items():
            if field in ("select", "order", "limit", "offset"):
                continue
            if expr.startswith("eq."):
                want = expr[3:]
                out = [r for r in out if str(r.get(field)) == want]
            elif expr.startswith("in.("):
                vals = {v.strip('"') for v in expr[4:-1].split(",") if v}
                out = [r for r in out if str(r.get(field)) in vals]
            elif expr.startswith("ilike."):
                pat = expr[6:].replace("*", "")
                out = [r for r in out if pat.lower() in str(r.get(field, "")).lower()]
            elif expr.startswith("ov."):
                vals = {v.strip('"') for v in expr[4:-1].split(",") if v}
                out = [r for r in out if vals & set(r.get(field) or [])]
            else:
                raise AssertionError(f"filtre non simulé : {field}={expr}")
        return out

    def _project(self, table, row):
        out = dict(row)
        if table == "bank_questions":
            out["author_profile"] = {"display_name": "Moi", "institution": ""}
            out["question_categories"] = [
                {"category_id": j["category_id"]}
                for j in self.tables["question_categories"]
                if j["question_id"] == row["id"]]
        return out


def install(monkey_target, fake, user_id="me"):
    """Branche le faux backend sur un module bank_online."""
    monkey_target._request = fake.request
    monkey_target.current_user_id = lambda: user_id
    return fake
