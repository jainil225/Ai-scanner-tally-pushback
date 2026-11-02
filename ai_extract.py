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
import time
from datetime import datetime

import requests

PROVIDERS = {
    "gemini": {
        "label": "Google Gemini (free tier available)",
        "models": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"],
        "default": "gemini-2.5-flash",
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
    pass


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


def _post_with_retry(url, headers, payload):
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            last = f"network error: {e}"
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504, 529) and attempt < RETRIES:
            wait = 2 ** (attempt + 1)
            ra = r.headers.get("retry-after")
            if ra and ra.isdigit():
                wait = min(int(ra), 60)
            last = f"HTTP {r.status_code}"
            time.sleep(wait)
            continue
        try:
            detail = r.json()
            msg = (detail.get("error") or {}).get("message") or json.dumps(detail)[:400]
        except ValueError:
            msg = r.text[:400]
        if r.status_code in (401, 403):
            raise AIError(f"API key rejected ({r.status_code}): {msg}")
        if r.status_code == 429:
            raise AIError(f"Rate limit / quota reached. Wait a minute and retry. ({msg})")
        raise AIError(f"AI request failed ({r.status_code}): {msg}")
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


def extract(pages, provider, api_key, model=None, extra_hint=""):
    """pages: list of JPG bytes for one bill -> normalised dict."""
    if not api_key:
        raise AIError("No API key set. Open Settings and paste your Gemini or Claude key.")
    if provider not in PROVIDERS:
        raise AIError(f"Unknown AI provider '{provider}'")
    model = model or PROVIDERS[provider]["default"]
    prompt = PROMPT + (f"\nContext about the user's company: {extra_hint}\n" if extra_hint else "")
    call = _call_gemini if provider == "gemini" else _call_claude
    raw = call(pages, api_key, model, prompt)
    return normalise(parse_json_reply(raw))
