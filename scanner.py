"""AI Bill Scanner -> Tally push-back.

Run:  python scanner.py        then open http://localhost:5090

Flow
  1. Upload bills (JPG/PNG, PDF, or a ZIP of them). PDFs are rendered to JPG pages.
  2. Scan: each bill is read by Gemini (free tier) or Claude and turned into a
     voucher draft mapped to the company's real Tally ledgers.
  3. Review: the customer checks / edits the voucher next to the bill image.
  4. Push: the voucher is imported into TallyPrime - by default as an
     OPTIONAL voucher, so it waits in Tally as a ready-made entry until the
     customer opens it and marks it Regular (Ctrl+L).
"""
import json
import os
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, abort, jsonify, request, send_file, send_from_directory, Response

import ai_extract
import file_prep
import mapping
import tally_push

APP_NAME = "AIBillScanner"
VERSION = "1.0.0"
PORT = int(os.environ.get("SCANNER_PORT", "5090"))

if getattr(sys, "frozen", False):                       # PyInstaller EXE
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_NAME)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.environ.get("SCANNER_DATA") or os.path.join(BASE_DIR, "data")
DOCS_DIR = os.path.join(DATA_DIR, "docs")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
HISTORY_FILE = os.path.join(DATA_DIR, "push_history.json")
os.makedirs(DOCS_DIR, exist_ok=True)

DEFAULT_SETTINGS = {
    "tally_url": tally_push.DEFAULT_URL,
    "company": "",
    "provider": "gemini",
    "gemini_key": "",
    "claude_key": "",
    "model": "",
    "own_gstin": "",
    "push_as_optional": True,
    "create_missing_ledgers": True,
    "default_purchase_ledger": "",
    "default_sales_ledger": "",
    "default_roundoff_ledger": "",
    "default_freight_ledger": "",
    "vtype_map": {},
}
SECRET_KEYS = ("gemini_key", "claude_key")

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024
_lock = threading.RLock()
_ledger_cache = {}          # company -> (time, LedgerBook, ledgers list, vtypes)


# ── storage helpers ─────────────────────────────────────────────────────────
def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(_read_json(SETTINGS_FILE, {}))
    return s


def public_settings(s):
    out = dict(s)
    for k in SECRET_KEYS:
        v = out.get(k) or ""
        out[k] = ("•" * 8 + v[-4:]) if v else ""
        out[k + "_set"] = bool(v)
    return out


def tally():
    return tally_push.Tally(load_settings()["tally_url"])


def doc_dir(doc_id):
    if not doc_id or not all(c in "0123456789abcdef" for c in doc_id):
        abort(400, "bad document id")
    d = os.path.join(DOCS_DIR, doc_id)
    if not os.path.isdir(d):
        abort(404, "document not found")
    return d


def load_doc(doc_id):
    return _read_json(os.path.join(doc_dir(doc_id), "meta.json"), None) or abort(404)


def save_doc(meta):
    meta["updated"] = datetime.now().isoformat(timespec="seconds")
    _write_json(os.path.join(DOCS_DIR, meta["id"], "meta.json"), meta)


def list_docs():
    out = []
    for d in os.listdir(DOCS_DIR):
        m = _read_json(os.path.join(DOCS_DIR, d, "meta.json"), None)
        if m:
            out.append(m)
    out.sort(key=lambda m: m.get("created", ""))
    return out


def ledger_book(company, refresh=False):
    with _lock:
        hit = _ledger_cache.get(company)
        if hit and not refresh and time.time() - hit[0] < 600:
            return hit
    t = tally()
    ledgers = t.ledgers(company)
    try:
        groups = t.groups(company)
    except tally_push.TallyError:
        groups = {}
    try:
        vtypes = t.voucher_types(company)
    except tally_push.TallyError:
        vtypes = []
    entry = (time.time(), mapping.LedgerBook(ledgers, groups), ledgers, vtypes)
    with _lock:
        _ledger_cache[company] = entry
    return entry


def err(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


# ── pages ───────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/info")
def info():
    return jsonify({"version": VERSION, "providers": ai_extract.PROVIDERS, "data_dir": DATA_DIR})


# ── settings ────────────────────────────────────────────────────────────────
@app.get("/api/settings")
def get_settings():
    return jsonify(public_settings(load_settings()))


@app.post("/api/settings")
def set_settings():
    body = request.get_json(force=True) or {}
    s = load_settings()
    for k, default in DEFAULT_SETTINGS.items():
        if k not in body:
            continue
        v = body[k]
        if k in SECRET_KEYS:
            v = (v or "").strip()
            if v.startswith("•"):         # masked value sent back unchanged
                continue
        elif isinstance(default, bool):
            v = bool(v)
        elif isinstance(default, dict):
            v = v if isinstance(v, dict) else {}
        else:
            v = str(v or "").strip()
        s[k] = v
    if s["provider"] not in ai_extract.PROVIDERS:
        s["provider"] = "gemini"
    s["own_gstin"] = s["own_gstin"].upper()
    _write_json(SETTINGS_FILE, s)
    return jsonify(public_settings(s))


# ── Tally reads ─────────────────────────────────────────────────────────────
@app.get("/api/ai/models")
def ai_models():
    """Live list of Gemini models the saved key can use (for the Settings dropdown)."""
    s = load_settings()
    if s["provider"] != "gemini" or not s["gemini_key"]:
        return jsonify({"ok": True, "models": ai_extract.PROVIDERS[s["provider"]]["models"]})
    try:
        return jsonify({"ok": True, "models": ai_extract.gemini_models(s["gemini_key"])})
    except ai_extract.AIError as e:
        return err(e, 502)


@app.get("/api/tally/status")
def tally_status():
    t = tally()
    if not t.ping():
        return jsonify({"online": False, "url": t.url, "companies": []})
    try:
        companies = t.companies()
    except tally_push.TallyError as e:
        return jsonify({"online": True, "url": t.url, "companies": [], "error": str(e)})
    return jsonify({"online": True, "url": t.url, "companies": companies})


@app.get("/api/tally/ledgers")
def tally_ledgers():
    company = request.args.get("company") or load_settings()["company"]
    if not company:
        return err("Choose a company first")
    try:
        _, book, ledgers, vtypes = ledger_book(company, refresh=request.args.get("refresh") == "1")
    except tally_push.TallyError as e:
        return err(e, 502)
    return jsonify({"ok": True, "company": company,
                    "ledgers": [{"name": l["name"], "parent": l["parent"], "primary": l["primary"],
                                 "gstin": l["gstin"]} for l in book.all],
                    "voucher_types": vtypes})


# ── documents ───────────────────────────────────────────────────────────────
@app.post("/api/upload")
def upload():
    files = request.files.getlist("files")
    if not files:
        return err("No files received")
    created, skipped = [], []
    for f in files:
        data = f.read()
        try:
            docs, sk = file_prep.load_file(f.filename or "upload", data)
        except file_prep.PrepError as e:
            skipped.append(str(e))
            continue
        skipped.extend(sk)
        for d in docs:
            if not d.pages:
                continue
            doc_id = uuid.uuid4().hex
            folder = os.path.join(DOCS_DIR, doc_id)
            os.makedirs(folder)
            for i, jpg in enumerate(d.pages):
                with open(os.path.join(folder, f"page_{i + 1}.jpg"), "wb") as out:
                    out.write(jpg)
            meta = {"id": doc_id, "name": d.name, "pages": len(d.pages), "status": "new",
                    "created": datetime.now().isoformat(timespec="seconds"),
                    "bill": None, "draft": None, "error": "", "push": None}
            save_doc(meta)
            created.append(meta)
    return jsonify({"ok": True, "documents": created, "skipped": skipped})


@app.get("/api/docs")
def docs():
    return jsonify({"ok": True, "documents": list_docs()})


@app.get("/api/docs/<doc_id>")
def doc_get(doc_id):
    return jsonify({"ok": True, "document": load_doc(doc_id)})


@app.delete("/api/docs/<doc_id>")
def doc_delete(doc_id):
    shutil.rmtree(doc_dir(doc_id), ignore_errors=True)
    return jsonify({"ok": True})


@app.get("/api/docs/<doc_id>/page/<int:n>")
def doc_page(doc_id, n):
    p = os.path.join(doc_dir(doc_id), f"page_{n}.jpg")
    if not os.path.isfile(p):
        abort(404)
    return send_file(p, mimetype="image/jpeg", max_age=3600)


@app.post("/api/docs/<doc_id>/scan")
def doc_scan(doc_id):
    meta = load_doc(doc_id)
    s = load_settings()
    provider = s["provider"]
    key = s["gemini_key"] if provider == "gemini" else s["claude_key"]
    pages = []
    for i in range(1, meta["pages"] + 1):
        with open(os.path.join(doc_dir(doc_id), f"page_{i}.jpg"), "rb") as f:
            pages.append(f.read())
    hint = f"Our company is '{s['company']}'" + (f", GSTIN {s['own_gstin']}" if s["own_gstin"] else "") \
        if s["company"] else ""
    meta["status"] = "scanning"
    save_doc(meta)
    try:
        bill = ai_extract.extract(pages, provider, key, s["model"] or None, hint)
        used = bill.pop("_model", None)
        if used and used != (s["model"] or ai_extract.PROVIDERS[provider]["default"]):
            # The chosen model was retired and we switched; remember the working one.
            s2 = load_settings()
            s2["model"] = used
            _write_json(SETTINGS_FILE, s2)
    except ai_extract.AIError as e:
        meta.update(status="error", error=str(e))
        save_doc(meta)
        return err(e, 502)
    meta["bill"] = bill
    meta["error"] = ""
    try:
        draft = make_draft(bill, s)
    except tally_push.TallyError as e:
        # AI worked; Tally is offline. Draft with no ledger matching, user can redo.
        draft = mapping.build_draft(bill, mapping.LedgerBook([]), s)
        draft["notes"].insert(0, f"Tally not reachable, ledgers not matched: {e}")
    meta["draft"] = draft
    meta["status"] = "ready"
    save_doc(meta)
    return jsonify({"ok": True, "document": meta})


def make_draft(bill, s):
    if not s["company"]:
        return mapping.build_draft(bill, mapping.LedgerBook([]), s)
    _, book, _, _ = ledger_book(s["company"])
    return mapping.build_draft(bill, book, s)


@app.post("/api/docs/<doc_id>/remap")
def doc_remap(doc_id):
    """Rebuild the draft from the AI result (e.g. after choosing a company)."""
    meta = load_doc(doc_id)
    if not meta.get("bill"):
        return err("Scan the document first")
    try:
        meta["draft"] = make_draft(meta["bill"], load_settings())
    except tally_push.TallyError as e:
        return err(e, 502)
    save_doc(meta)
    return jsonify({"ok": True, "document": meta})


@app.put("/api/docs/<doc_id>/draft")
def doc_save_draft(doc_id):
    meta = load_doc(doc_id)
    draft = request.get_json(force=True) or {}
    meta["draft"] = draft
    if meta["status"] in ("new", "error") and draft:
        meta["status"] = "ready"
    save_doc(meta)
    return jsonify({"ok": True, "document": meta})


# ── push to Tally ───────────────────────────────────────────────────────────
def _history():
    return _read_json(HISTORY_FILE, [])


def _dup_key(company, d):
    return "|".join([company.lower(), (d.get("voucher_type") or "").lower(), (d.get("party_ledger") or "").lower(),
                     (d.get("reference") or d.get("voucher_number") or "").lower()])


def _clean_entries(draft):
    out = []
    for e in draft.get("entries") or []:
        try:
            amt = round(float(e.get("amount") or 0), 2)
        except (TypeError, ValueError):
            raise tally_push.TallyError(f"Amount '{e.get('amount')}' is not a number")
        if amt and (e.get("ledger") or "").strip():
            out.append({"ledger": e["ledger"].strip(), "dr_cr": "Dr" if e.get("dr_cr") == "Dr" else "Cr",
                        "amount": amt, "bill_ref": (e.get("bill_ref") or "").strip()})
    return out


def _prepared(draft, book):
    v = dict(draft)
    v["entries"] = _clean_entries(draft)
    # Bill-wise reference only on ledgers that keep bills (creditors / debtors)
    for e in v["entries"]:
        if e.get("bill_ref"):
            l = book.get(e["ledger"]) if book else None
            new = any(n["name"].lower() == e["ledger"].lower() and n.get("create")
                      for n in draft.get("new_ledgers") or [])
            keeps_bills = (new and e["ledger"] == v.get("party_ledger")) or \
                bool(l and l["primary"] in ("Sundry Creditors", "Sundry Debtors"))
            if not keeps_bills:
                e["bill_ref"] = ""
    return v


@app.post("/api/xml")
def xml_preview():
    """The exact XML that would be sent - for download / manual import in Tally."""
    body = request.get_json(force=True) or {}
    s = load_settings()
    company = body.get("company") or s["company"]
    optional = body.get("optional", s["push_as_optional"])
    try:
        book = ledger_book(company)[1] if company else None
    except tally_push.TallyError:
        book = None
    masters, vouchers = [], []
    try:
        for draft in body.get("drafts") or []:
            for n in draft.get("new_ledgers") or []:
                if n.get("create") and not (book and book.exists(n["name"])):
                    masters.append(tally_push.ledger_master_xml(n))
            vouchers.append(tally_push.voucher_xml(_prepared(draft, book), optional))
    except tally_push.TallyError as e:
        return err(e)
    # One envelope: new ledgers first so the vouchers that use them import cleanly.
    # Tally > Import > Transactions accepts this file as-is.
    xml = tally_push.build_import_envelope(company, "Vouchers", "".join(dict.fromkeys(masters)) + "".join(vouchers))
    return Response(xml, mimetype="application/xml",
                    headers={"Content-Disposition": "attachment; filename=tally_vouchers.xml"})


@app.post("/api/push")
def push():
    body = request.get_json(force=True) or {}
    s = load_settings()
    company = body.get("company") or s["company"]
    if not company:
        return err("Choose the Tally company first")
    optional = bool(body.get("optional", s["push_as_optional"]))
    create_missing = bool(body.get("create_missing_ledgers", s["create_missing_ledgers"]))
    force = bool(body.get("force"))
    t = tally()
    try:
        _, book, _, _ = ledger_book(company, refresh=True)
    except tally_push.TallyError as e:
        return err(e, 502)

    history = _history()
    seen = {h["key"] for h in history if h.get("ok")}
    results = []
    for doc_id in body.get("doc_ids") or []:
        meta = load_doc(doc_id)
        draft = meta.get("draft")
        res = {"id": doc_id, "name": meta["name"], "ok": False}
        results.append(res)
        if not draft:
            res["error"] = "Not scanned yet"
            continue
        key = _dup_key(company, draft)
        if key in seen and not force:
            res["error"] = "Already pushed earlier (same party, type and bill number). Tick 'push again' to override."
            res["duplicate"] = True
            continue
        try:
            v = _prepared(draft, book)
            # 1) masters
            missing = []
            for e in v["entries"]:
                if not book.exists(e["ledger"]):
                    spec = next((n for n in draft.get("new_ledgers") or []
                                 if n["name"].lower() == e["ledger"].lower()), None)
                    if spec and spec.get("create") and create_missing:
                        if spec not in missing:
                            missing.append(spec)
                    else:
                        raise tally_push.TallyError(f"Ledger '{e['ledger']}' does not exist in {company}. "
                                                    "Pick an existing ledger or allow creating it.")
            if missing:
                mres = t.create_ledgers(company, missing)
                if mres["errors"]:
                    raise tally_push.TallyError("Could not create ledger(s): " + "; ".join(mres["messages"] or ["unknown error"]))
                ledger_book(company, refresh=True)
                book = _ledger_cache[company][1]
                res["ledgers_created"] = [m["name"] for m in missing]
            # 2) voucher
            vres = t.push_voucher(company, v, optional)
            if vres["errors"] or not (vres["created"] or vres["altered"]):
                raise tally_push.TallyError("Tally rejected the voucher: " +
                                            "; ".join(vres["messages"] or [f"created={vres['created']} errors={vres['errors']}"]))
            res.update(ok=True, vch_id=vres["last_vch_id"], optional=optional)
            meta["status"] = "pushed"
            meta["push"] = {"at": datetime.now().isoformat(timespec="seconds"), "company": company,
                            "optional": optional, "vch_id": vres["last_vch_id"]}
            meta["error"] = ""
            save_doc(meta)
            seen.add(key)
            history.append({"key": key, "ok": True, "doc": meta["name"], "at": meta["push"]["at"],
                            "company": company, "voucher_type": v["voucher_type"], "party": v.get("party_ledger"),
                            "reference": v.get("reference") or v.get("voucher_number"),
                            "amount": next((e["amount"] for e in v["entries"] if e["ledger"] == v.get("party_ledger")), None),
                            "optional": optional, "vch_id": vres["last_vch_id"]})
        except tally_push.TallyError as e:
            res["error"] = str(e)
            meta["status"] = "error" if meta["status"] != "pushed" else meta["status"]
            meta["error"] = str(e)
            save_doc(meta)
    _write_json(HISTORY_FILE, history[-2000:])
    return jsonify({"ok": all(r["ok"] for r in results), "results": results})


@app.get("/api/history")
def history():
    return jsonify({"ok": True, "history": list(reversed(_history()))[:500]})


@app.errorhandler(400)
@app.errorhandler(404)
def http_err(e):
    if request.path.startswith("/api/"):
        return err(getattr(e, "description", str(e)), e.code)
    return e


def main():
    import webbrowser
    url = f"http://localhost:{PORT}"
    print(f"AI Bill Scanner {VERSION} on {url}  (data: {DATA_DIR})")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
