"""Talk to TallyPrime over its XML/HTTP server (default http://localhost:9000).

Reads:   open companies, ledgers (name, group, GSTIN, state), voucher types.
Writes:  ledgers (optional, for parties/tax ledgers that do not exist yet) and
         vouchers. By default vouchers go in as **Optional vouchers** - they show
         in Tally but do not touch the books until the customer opens one and
         marks it Regular (Ctrl+L). That is the "preview in Tally" step.

Tally XML amount convention for vouchers:
    Debit  -> ISDEEMEDPOSITIVE = Yes, AMOUNT negative
    Credit -> ISDEEMEDPOSITIVE = No,  AMOUNT positive
"""
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

DEFAULT_URL = "http://localhost:9000"

# TallyPrime's HTTP server handles one request at a time; overlapping requests
# freeze its UI. Every call in this process goes through this lock.
_LOCK = threading.Semaphore(1)


class TallyError(RuntimeError):
    pass


def xe(t):
    """Escape text for XML element content and attributes."""
    return (str(t if t is not None else "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def fix_enc(raw):
    """Tally sometimes answers in Windows-1252 with invalid char references."""
    raw = re.sub(rb"<\?xml[^?]*\?>\s*", b"", raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")

    def _san(m):
        try:
            cp = int(m.group(1)) if m.group(1) else int(m.group(2), 16)
            return m.group(0) if (cp in (9, 10, 13) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD) else ""
        except ValueError:
            return ""
    text = re.sub(r"&#(\d+);|&#[xX]([0-9A-Fa-f]+);", _san, text)
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)
    # Tally may emit bare '&' in names on some builds
    text = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)", "&amp;", text)
    return text.encode("utf-8")


class Tally:
    def __init__(self, url=DEFAULT_URL, timeout=120):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.timeout = timeout

    # ── transport ───────────────────────────────────────────────────────────
    def post(self, body, timeout=None):
        if not _LOCK.acquire(timeout=180):
            raise TallyError("Tally is busy with another request. Please retry.")
        try:
            r = requests.post(self.url, data=body.encode("utf-8"),
                              headers={"Content-Type": "text/xml; charset=utf-8"},
                              timeout=(5, timeout or self.timeout))
            r.raise_for_status()
            return r.content
        except requests.ConnectionError:
            raise TallyError(f"Cannot reach Tally at {self.url}. Is TallyPrime open with "
                             "F1 > Settings > Connectivity > 'TallyPrime acts as Server' on port 9000?")
        except requests.Timeout:
            raise TallyError("Tally did not answer in time.")
        except requests.HTTPError as e:
            raise TallyError(f"Tally HTTP error: {e}")
        finally:
            _LOCK.release()

    def ping(self):
        try:
            requests.post(self.url, data=b"<ENVELOPE/>", timeout=3)
            return True
        except requests.RequestException:
            return False

    def _collection(self, coll_id, typ, fetch, company=None, timeout=None):
        sv = f"<SVCURRENTCOMPANY>{xe(company)}</SVCURRENTCOMPANY>" if company else ""
        body = (f"<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
                f"<TYPE>Collection</TYPE><ID>{coll_id}</ID></HEADER><BODY><DESC><STATICVARIABLES>"
                f"<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>{sv}</STATICVARIABLES><TDL><TDLMESSAGE>"
                f"<COLLECTION NAME=\"{coll_id}\" ISMODIFY=\"No\"><TYPE>{typ}</TYPE>"
                f"<FETCH>{fetch}</FETCH></COLLECTION></TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>")
        raw = self.post(body, timeout)
        try:
            return ET.fromstring(fix_enc(raw))
        except ET.ParseError as e:
            raise TallyError(f"Could not read Tally's reply: {e}")

    # ── reads ───────────────────────────────────────────────────────────────
    def companies(self):
        root = self._collection("AIScanCompanies", "Company", "Name", timeout=15)
        out = []
        for el in root.iter("COMPANY"):
            nm = (el.get("NAME") or el.findtext("NAME") or "").strip()
            if nm and nm not in out:
                out.append(nm)
        return out

    def ledgers(self, company):
        """-> [{name, parent, gstin, state}] (no date filters: those freeze Tally)."""
        root = self._collection("AIScanLedgers", "Ledger",
                                "Name, Parent, PartyGSTIN, LedStateName, StateName, "
                                "LedGSTRegDetails.GSTIN, LedMailingDetails.State",
                                company=company)
        out = []
        for el in root.iter("LEDGER"):
            name = (el.get("NAME") or el.findtext("NAME") or "").strip()
            if not name:
                continue
            gstin = (el.findtext("PARTYGSTIN") or "").strip()
            if not gstin:
                g = el.find(".//LEDGSTREGDETAILS.LIST/GSTIN")
                gstin = (g.text or "").strip() if g is not None and g.text else ""
            state = (el.findtext("LEDSTATENAME") or el.findtext("STATENAME") or "").strip()
            if not state:
                s = el.find(".//LEDMAILINGDETAILS.LIST/STATE")
                state = (s.text or "").strip() if s is not None and s.text else ""
            out.append({"name": name, "parent": (el.findtext("PARENT") or "").strip(),
                        "gstin": gstin.upper(), "state": state})
        return out

    def groups(self, company):
        """-> {group_name: parent_group} so we can tell which primary group a ledger sits in."""
        root = self._collection("AIScanGroups", "Group", "Name, Parent", company=company)
        out = {}
        for el in root.iter("GROUP"):
            name = (el.get("NAME") or el.findtext("NAME") or "").strip()
            if name:
                out[name] = (el.findtext("PARENT") or "").strip()
        return out

    def voucher_types(self, company):
        root = self._collection("AIScanVchTypes", "VoucherType", "Name, Parent", company=company)
        out = []
        for el in root.iter("VOUCHERTYPE"):
            name = (el.get("NAME") or el.findtext("NAME") or "").strip()
            if name:
                out.append({"name": name, "parent": (el.findtext("PARENT") or "").strip() or name})
        return out

    # ── writes ──────────────────────────────────────────────────────────────
    def import_xml(self, company, report, messages_xml):
        body = build_import_envelope(company, report, messages_xml)
        return parse_import_response(self.post(body, timeout=120))

    def create_ledgers(self, company, ledgers):
        if not ledgers:
            return {"created": 0, "altered": 0, "errors": 0, "messages": []}
        msgs = "".join(ledger_master_xml(l) for l in ledgers)
        return self.import_xml(company, "All Masters", msgs)

    def push_voucher(self, company, voucher, optional=True):
        return self.import_xml(company, "Vouchers", voucher_xml(voucher, optional))


# ── XML builders (pure functions, unit-tested) ──────────────────────────────
def build_import_envelope(company, report, messages_xml):
    sv = f"<SVCURRENTCOMPANY>{xe(company)}</SVCURRENTCOMPANY>" if company else ""
    return (f"<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
            f"<REQUESTDESC><REPORTNAME>{report}</REPORTNAME><STATICVARIABLES>{sv}</STATICVARIABLES>"
            f"</REQUESTDESC><REQUESTDATA>{messages_xml}</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>")


GST_HEADS = {"cgst": "Central Tax", "sgst": "State Tax", "utgst": "State Tax", "igst": "Integrated Tax",
             "cess": "Cess"}


def ledger_master_xml(l):
    """l = {name, parent, gstin?, state?, tax_head?}"""
    name, parent = l["name"], l.get("parent") or "Sundry Creditors"
    parts = [f'<TALLYMESSAGE xmlns:UDF="TallyUDF"><LEDGER NAME="{xe(name)}" ACTION="Create">',
             f"<NAME.LIST><NAME>{xe(name)}</NAME></NAME.LIST>",
             f"<PARENT>{xe(parent)}</PARENT>"]
    if parent in ("Sundry Creditors", "Sundry Debtors"):
        parts.append("<ISBILLWISEON>Yes</ISBILLWISEON>")
        if l.get("gstin"):
            parts += ["<GSTREGISTRATIONTYPE>Regular</GSTREGISTRATIONTYPE>",
                      f"<PARTYGSTIN>{xe(l['gstin'])}</PARTYGSTIN>"]
        if l.get("state"):
            parts.append(f"<LEDSTATENAME>{xe(l['state'])}</LEDSTATENAME>")
        parts.append("<COUNTRYOFRESIDENCE>India</COUNTRYOFRESIDENCE>")
    elif parent == "Duties & Taxes":
        head = l.get("tax_head")
        if head in GST_HEADS:
            parts += ["<TAXTYPE>GST</TAXTYPE>", f"<GSTDUTYHEAD>{GST_HEADS[head]}</GSTDUTYHEAD>"]
        elif head == "tds":
            parts.append("<TAXTYPE>TDS</TAXTYPE>")
    elif parent in ("Purchase Accounts", "Sales Accounts"):
        parts.append("<AFFECTSSTOCK>No</AFFECTSSTOCK>")
    parts.append("</LEDGER></TALLYMESSAGE>")
    return "".join(parts)


def tdate(iso):
    """'2026-04-05' -> '20260405'."""
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%Y%m%d")


def fmt_amt(x):
    return f"{x:.2f}"


def voucher_xml(v, optional=True):
    """v = {
        voucher_type, date (YYYY-MM-DD), voucher_number?, reference?, reference_date?,
        party_ledger?, party_gstin?, party_state?, narration?,
        entries: [{ledger, dr_cr: 'Dr'|'Cr', amount, bill_ref?}]
    }"""
    if not v.get("date"):
        raise TallyError("Voucher date is required")
    entries = [e for e in v.get("entries", []) if (e.get("ledger") or "").strip() and float(e.get("amount") or 0)]
    if len(entries) < 2:
        raise TallyError("A voucher needs at least two ledger lines")
    dr = round(sum(float(e["amount"]) for e in entries if e["dr_cr"] == "Dr"), 2)
    cr = round(sum(float(e["amount"]) for e in entries if e["dr_cr"] == "Cr"), 2)
    if abs(dr - cr) > 0.005:
        raise TallyError(f"Debit {dr:.2f} and Credit {cr:.2f} do not match")

    vt = v.get("voucher_type") or "Purchase"
    d = tdate(v["date"])
    out = [f'<TALLYMESSAGE xmlns:UDF="TallyUDF">'
           f'<VOUCHER VCHTYPE="{xe(vt)}" ACTION="Create" OBJVIEW="Accounting Voucher View">',
           f"<DATE>{d}</DATE><EFFECTIVEDATE>{d}</EFFECTIVEDATE>",
           f"<VOUCHERTYPENAME>{xe(vt)}</VOUCHERTYPENAME>",
           "<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>",
           f"<ISOPTIONAL>{'Yes' if optional else 'No'}</ISOPTIONAL>",
           "<ISINVOICE>No</ISINVOICE>"]
    if v.get("voucher_number"):
        out.append(f"<VOUCHERNUMBER>{xe(v['voucher_number'])}</VOUCHERNUMBER>")
    if v.get("reference"):
        out.append(f"<REFERENCE>{xe(v['reference'])}</REFERENCE>")
        if v.get("reference_date"):
            out.append(f"<REFERENCEDATE>{tdate(v['reference_date'])}</REFERENCEDATE>")
    if v.get("party_ledger"):
        out.append(f"<PARTYLEDGERNAME>{xe(v['party_ledger'])}</PARTYLEDGERNAME>")
        out.append(f"<PARTYNAME>{xe(v['party_ledger'])}</PARTYNAME>")
    if v.get("party_gstin"):
        out.append(f"<PARTYGSTIN>{xe(v['party_gstin'])}</PARTYGSTIN>")
    if v.get("party_state"):
        out.append(f"<STATENAME>{xe(v['party_state'])}</STATENAME>")
    if v.get("narration"):
        out.append(f"<NARRATION>{xe(v['narration'])}</NARRATION>")
    for e in entries:
        amt = round(float(e["amount"]), 2)
        is_dr = e["dr_cr"] == "Dr"
        signed = -amt if is_dr else amt
        is_party = (e["ledger"] == v.get("party_ledger"))
        out.append("<ALLLEDGERENTRIES.LIST>")
        out.append(f"<LEDGERNAME>{xe(e['ledger'])}</LEDGERNAME>")
        out.append(f"<ISDEEMEDPOSITIVE>{'Yes' if is_dr else 'No'}</ISDEEMEDPOSITIVE>")
        out.append(f"<ISPARTYLEDGER>{'Yes' if is_party else 'No'}</ISPARTYLEDGER>")
        out.append(f"<AMOUNT>{fmt_amt(signed)}</AMOUNT>")
        if e.get("bill_ref"):
            out.append(f"<BILLALLOCATIONS.LIST><NAME>{xe(e['bill_ref'])}</NAME>"
                       f"<BILLTYPE>New Ref</BILLTYPE><AMOUNT>{fmt_amt(signed)}</AMOUNT>"
                       f"</BILLALLOCATIONS.LIST>")
        out.append("</ALLLEDGERENTRIES.LIST>")
    out.append("</VOUCHER></TALLYMESSAGE>")
    return "".join(out)


def parse_import_response(raw):
    """Tally's import reply -> {created, altered, errors, exceptions, last_vch_id, messages}."""
    try:
        root = ET.fromstring(fix_enc(raw))
    except ET.ParseError:
        txt = raw.decode("utf-8", "replace")[:500]
        raise TallyError(f"Unexpected reply from Tally: {txt}")

    def n(tag):
        el = root.find(f".//{tag}")
        try:
            return int((el.text or "0").strip()) if el is not None else 0
        except ValueError:
            return 0
    msgs = [(el.text or "").strip() for el in root.iter("LINEERROR") if (el.text or "").strip()]
    lv = root.find(".//LASTVCHID")
    res = {"created": n("CREATED"), "altered": n("ALTERED"), "errors": n("ERRORS"),
           "exceptions": n("EXCEPTIONS"), "ignored": n("IGNORED"),
           "last_vch_id": (lv.text or "").strip() if lv is not None and lv.text else "",
           "messages": msgs}
    # Tally answers a failed request with a bare <RESPONSE>message</RESPONSE> on some builds
    if root.tag == "RESPONSE" and not list(root) and (root.text or "").strip():
        res["messages"].append(root.text.strip())
        if not res["created"] and not res["altered"]:
            res["errors"] = max(res["errors"], 1)
    return res
