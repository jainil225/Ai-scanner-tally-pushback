"""Read a bill (one or more JPG pages) with an AI vision model and return
structured invoice data as a dict.

Two providers, both called over plain HTTPS (no SDKs, so the EXE stays small):

* Google Gemini  - has a FREE tier. Get a key at https://aistudio.google.com/apikey
* Anthropic Claude - paid, very accurate. Key at https://console.anthropic.com

The model is told to answer with JSON only. We still defend against code
fences, stray prose and missing fields, then normalise numbers and dates so
the rest of the app can trust the shape of the result.
"""
import base64
import json
import re
import threading
import time
from collections import deque
from datetime import datetime

import requests

PROVIDERS = {
    "gemini": {
        "label": "Google Gemini (free tier available)",
        # Google retires model names often. If the chosen one is gone, extract()
        # switches to the replacement Google names, or the best one the key can use.
        "models": ["gemini-3.8-flash"],
        "default": "gemini-3.8-flash",
        "key_url": "https://aistudio.google.com/apikey",
    },
    "claude": {
        "label": "Anthropic Claude",
        "models": ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"],
        "default": "claude-sonnet-5-5",
        "key_url": "https://console.anthropic.com/settings/keys",
    },
}

TIMEOUT = 120
RETRIES = 3          # on 429 / 5xx (free tiers rate-limit quickly)


class AIError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


PROMPT = """You are an expert Indian accountant reading a bill / invoice image for entry into TallyPrime.
The images are the pages of ONE document, in order.

Return ONLY a JSON object (no markdown, no comments) with exactly these keys:
{
  "doc_type": one of "purchase_invoice", "sales_invoice", "expense_receipt", "credit_note", "debit_note", "other",
  "supplier_name": string,            // seller / issuer of the bill
  "supplier_gstin": string,           // 15-char GSTIN or ""
  "supplier_state": string,           // state name or ""
  "supplier_address": string,
  "buyer_name": string,
  "buyer_gstin": string,
  "buyer_state": string,
  "invoice_number": string,
  "invoice_date": "YYYY-MM-DD" or "",
  "place_of_supply": string,
  "currency": "INR" unless clearly another currency,
  "items": [ { "description": string, "hsn_sac": string, "quantity": number, "unit": string,
               "rate": number, "discount": number, "taxable_value": number, "gst_rate": number } ],
  "tax_breakup": [ { "gst_rate": number, "taxable_value": number, "cgst": number, "sgst": number, "igst": number } ],
  "taxable_total": number,            // total before GST
  "cgst": number, "sgst": number, "igst": number, "cess": number,
  "tds": number,                      // only if printed on the bill, else 0
  "other_charges": number,            // freight / packing etc. NOT already inside taxable_total
  "round_off": number,                // signed, e.g. -0.40 or 0.25
  "grand_total": number,              // final amount payable as printed
  "amount_in_words": string,
  "narration": string,                // one short line describing what was bought / sold
  "confidence": number between 0 and 1,
  "warnings": [string]                // anything unreadable, inconsistent or doubtful
}

Rules:
- Numbers are plain numbers: no commas, no currency symbols. Use 0 when absent.
- Dates: convert any printed format (DD/MM/YYYY, 05-Jan-26 ...) to YYYY-MM-DD. Indian bills are day-first.
- gst_rate is the TOTAL rate (e.g. 18 for 9% CGST + 9% SGST).
- If CGST/SGST are shown, igst is 0, and vice versa.
- Check that taxable_total + cgst + sgst + igst + cess + other_charges + round_off ≈ grand_total.
  If not, still report what is printed and explain in warnings.
- Never invent values. If something is unreadable, use "" or 0 and add a warning.
"""


def _b64(jpg):
    return base64.b64encode(jpg).decode("ascii")


# ── pacing / rate limits ────────────────────────────────────────────────────
# Free Gemini keys allow only a few requests per minute (5 for flash today).
# We pace our own requests under that limit instead of hitting it and failing.
GEMINI_RPM = 5            # set from Settings by scanner.py; 0 = no pacing (paid keys)
MAX_WAIT = 90             # longest we will sleep for one rate-limit pause (seconds)
_pace_lock = threading.Lock()
_recent = deque()         # timestamps of recent Gemini requests
STATUS_HOOK = None        # optional callable(str) so the UI can show "waiting 40s…"


def _status(msg):
    if STATUS_HOOK:
        try:
            STATUS_HOOK(msg)
        except Exception:
            pass


def _pace(url):
    if "generativelanguage.googleapis.com" not in url or not GEMINI_RPM:
        return
    with _pace_lock:
        while True:
            now = time.time()
            while _recent and now - _recent[0] >= 60:
                _recent.popleft()
            if len(_recent) < GEMINI_RPM:
                _recent.append(now)
                return
            wait = 60 - (now - _recent[0]) + 0.5
            _status(f"Free-tier limit: waiting {wait:.0f}s before the next bill")
            time.sleep(wait)


def _retry_delay(r, body):
    """Seconds Google asks us to wait, from RetryInfo, the message, or Retry-After."""
    for d in ((body.get("error") or {}).get("details") or []):
        rd = d.get("retryDelay") if isinstance(d, dict) else None
        if rd:
            m = re.match(r"([\d.]+)s", str(rd))
            if m:
                return float(m.group(1))
    m = re.search(r"retry in ([\d.]+)\s*s", json.dumps(body), re.I)
    if m:
        return float(m.group(1))
    ra = r.headers.get("retry-after")
    if ra and ra.replace(".", "", 1).isdigit():
        return float(ra)
    return None


def _is_daily_quota(body):
    t = json.dumps(body).lower()
    return "per_day" in t or "perday" in t or "per day" in t


def _post_with_retry(url, headers, payload):
    last = None
    for attempt in range(RETRIES + 1):
        _pace(url)
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            last = f"network error: {e}"
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        try:
            body = r.json()
            msg = (body.get("error") or {}).get("message") or json.dumps(body)[:400]
        except ValueError:
            body, msg = {}, r.text[:400]
        if r.status_code == 429:
            if _is_daily_quota(body):
                raise AIError("Daily free limit of this Gemini key is used up. It resets at midnight US Pacific "
                              "time (about 1:30 pm IST). To keep going now: enable billing on the key, use another "
                              f"key, or switch to Claude in Settings. ({msg[:200]})", status=429)
            wait = _retry_delay(r, body) or 2 ** (attempt + 2)
            if attempt < RETRIES and wait <= MAX_WAIT:
                _status(f"Rate limited by the AI: waiting {wait:.0f}s and retrying")
                time.sleep(wait + 1)
                last = "rate limited"
                continue
            raise AIError("The AI is still rate-limiting this key. Free Gemini allows only a few bills per minute: "
                          "lower 'Requests per minute' in Settings, wait a minute and press Re-scan. "
                          f"({msg[:200]})", status=429)
        if r.status_code in (500, 502, 503, 504, 529) and attempt < RETRIES:
            last = f"HTTP {r.status_code}"
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise AIError(f"API key rejected ({r.status_code}): {msg}", status=r.status_code)
        raise AIError(f"AI request failed ({r.status_code}): {msg}", status=r.status_code)
    raise AIError(f"AI request failed after retries: {last}")


def _call_gemini(pages, api_key, model, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    parts = [{"text": prompt}]
    for jpg in pages:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": _b64(jpg)}})
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    data = _post_with_retry(url, {"x-goog-api-key": api_key, "Content-Type": "application/json"}, payload)
    try:
        cand = data["candidates"][0]
        return "".join(p.get("text", "") for p in cand["content"]["parts"])
    except (KeyError, IndexError):
        reason = (data.get("promptFeedback") or {}).get("blockReason") or json.dumps(data)[:300]
        raise AIError(f"Gemini returned no text ({reason})")


def _call_claude(pages, api_key, model, prompt):
    content = []
    for jpg in pages:
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(jpg)}})
    content.append({"type": "text", "text": prompt})
    payload = {
        "model": model,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    data = _post_with_retry("https://api.anthropic.com/v1/messages", headers, payload)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if not text:
        raise AIError(f"Claude returned no text (stop_reason={data.get('stop_reason')})")
    return text


def parse_json_reply(text):
    """Pull the JSON object out of a model reply, tolerating fences and prose."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        return json.loads(t)
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except ValueError:
            pass
    raise AIError("AI reply was not valid JSON")


# ── normalisation ────────────────────────────────────────────────────────────
NUM_KEYS = ["taxable_total", "cgst", "sgst", "igst", "cess", "tds", "other_charges",
            "round_off", "grand_total"]
STR_KEYS = ["doc_type", "supplier_name", "supplier_gstin", "supplier_state", "supplier_address",
            "buyer_name", "buyer_gstin", "buyer_state", "invoice_number", "invoice_date",
            "place_of_supply", "currency", "amount_in_words", "narration"]
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")


def num(v):
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).replace(",", "").replace("₹", "").replace("Rs.", "").replace("Rs", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return 0.0
    val = float(m.group(0))
    return round(-val if neg else val, 2)


def norm_date(s):
    s = (s or "").strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y",
                "%d-%b-%Y", "%d-%b-%y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%d/%b/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def normalise(d):
    out = {k: str(d.get(k) or "").strip() for k in STR_KEYS}
    for k in NUM_KEYS:
        out[k] = num(d.get(k))
    out["invoice_date"] = norm_date(out["invoice_date"])
    for k in ("supplier_gstin", "buyer_gstin"):
        out[k] = re.sub(r"\s", "", out[k]).upper()
    out["currency"] = out["currency"] or "INR"
    if out["doc_type"] not in ("purchase_invoice", "sales_invoice", "expense_receipt",
                               "credit_note", "debit_note", "other"):
        out["doc_type"] = "purchase_invoice"
    items = []
    for it in d.get("items") or []:
        if not isinstance(it, dict):
            continue
        items.append({
            "description": str(it.get("description") or "").strip(),
            "hsn_sac": str(it.get("hsn_sac") or "").strip(),
            "quantity": num(it.get("quantity")),
            "unit": str(it.get("unit") or "").strip(),
            "rate": num(it.get("rate")),
            "discount": num(it.get("discount")),
            "taxable_value": num(it.get("taxable_value")),
            "gst_rate": num(it.get("gst_rate")),
        })
    out["items"] = items
    tb = []
    for t in d.get("tax_breakup") or []:
        if isinstance(t, dict):
            tb.append({k: num(t.get(k)) for k in ("gst_rate", "taxable_value", "cgst", "sgst", "igst")})
    out["tax_breakup"] = tb
    try:
        out["confidence"] = max(0.0, min(1.0, float(d.get("confidence") or 0)))
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    warns = [str(w) for w in (d.get("warnings") or []) if str(w).strip()]

    # Our own sanity checks on top of the model's
    if not out["taxable_total"] and items:
        out["taxable_total"] = round(sum(i["taxable_value"] for i in items), 2)
    calc = round(out["taxable_total"] + out["cgst"] + out["sgst"] + out["igst"] + out["cess"]
                 + out["other_charges"] + out["round_off"], 2)
    if out["grand_total"] and abs(calc - out["grand_total"]) > 1.0:
        warns.append(f"Totals do not add up: computed {calc:.2f} vs printed {out['grand_total']:.2f}")
    if not out["grand_total"]:
        out["grand_total"] = calc
    for k in ("supplier_gstin", "buyer_gstin"):
        if out[k] and not GSTIN_RE.match(out[k]):
            warns.append(f"{k.replace('_', ' ')} '{out[k]}' does not look like a valid GSTIN")
    if not out["invoice_date"]:
        warns.append("Invoice date not read")
    if not out["invoice_number"]:
        warns.append("Invoice number not read")
    out["warnings"] = warns
    return out


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def gemini_models(api_key):
    """Models this key can call with generateContent, best first (newest flash)."""
    try:
        r = requests.get(f"{GEMINI_BASE}/models", headers={"x-goog-api-key": api_key},
                         params={"pageSize": 1000}, timeout=20)
    except requests.RequestException as e:
        raise AIError(f"Could not list Gemini models: {e}")
    if r.status_code != 200:
        raise AIError(f"Could not list Gemini models (HTTP {r.status_code})", status=r.status_code)
    names = [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    return sorted(names, key=_gemini_rank)


def _gemini_rank(name):
    """Sort key: stable 'flash' models first, newest version first."""
    n = name.lower()
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
    ver = float(m.group(1)) if m else 0.0
    unstable = any(w in n for w in ("preview", "exp", "experimental", "latest", "tts", "image",
                                    "audio", "live", "embedding", "thinking", "computer"))
    kind = 0 if "flash" in n and "lite" not in n else 1 if "flash" in n else 2 if "pro" in n else 3
    return (unstable, "gemini" not in n, kind, -ver, n)


def _model_gone(e):
    msg = str(e).lower()
    return getattr(e, "status", None) == 404 or ("model" in msg and ("not found" in msg or "no longer" in msg
                                                                      or "not supported" in msg))


def _suggested_model(e, current):
    """Google's error often names the replacement: '... use models/gemini-x-flash ...'."""
    for m in re.findall(r"models/([A-Za-z0-9][\w.\-]*[A-Za-z0-9])", str(e)):
        if m != current:
            return m
    return None


def extract(pages, provider, api_key, model=None, extra_hint=""):
    """pages: list of JPG bytes for one bill -> normalised dict."""
    if not api_key:
        raise AIError("No API key set. Open Settings and paste your Gemini or Claude key.")
    if provider not in PROVIDERS:
        raise AIError(f"Unknown AI provider '{provider}'")
    model = model or PROVIDERS[provider]["default"]
    prompt = PROMPT + (f"\nContext about the user's company: {extra_hint}\n" if extra_hint else "")
    call = _call_gemini if provider == "gemini" else _call_claude
    tried = []
    while True:
        tried.append(model)
        try:
            raw = call(pages, api_key, model, prompt)
            break
        except AIError as e:
            if provider != "gemini" or not _model_gone(e) or len(tried) >= 4:
                raise
            nxt = _suggested_model(e, model)
            if not nxt or nxt in tried:
                nxt = next((m for m in gemini_models(api_key) if m not in tried), None)
            if not nxt:
                raise AIError(f"{e} (no other Gemini model available for this key)")
            model = nxt
    out = normalise(parse_json_reply(raw))
    out["_model"] = model            # the model that actually answered
    return out
