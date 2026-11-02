"""Import vouchers from Excel (.xlsx) or CSV - no AI needed.

Two layouts are accepted, detected from the column headings (any order, case
and common spellings are ignored):

1. Bill register - one row per bill (or per GST rate of a bill):
     Date | Voucher Type | Party | Party GSTIN | Bill No | Taxable | GST % |
     CGST | SGST | IGST | Cess | Round Off | Total | Narration | Ledger
   Rows with the same Voucher Type + Party + Bill No are one bill with several
   GST rates. CGST/SGST/IGST may be left blank: they are worked out from GST %
   and the party's state code (same state as your GSTIN -> CGST+SGST, else IGST).
   'Ledger' (optional) forces the purchase / sales ledger for that row.

2. Ledger lines - one row per ledger line, for journals, payments, receipts:
     Voucher No | Date | Voucher Type | Ledger | Debit | Credit | Narration | Bill Ref
   Rows with the same Voucher No form one voucher. Without a Voucher No, rows
   are collected until debits equal credits.

Each bill / voucher becomes a draft that the user reviews and pushes like a
scanned bill.
"""
import csv
import io
import re
from datetime import date, datetime

import ai_extract
import mapping

ALIASES = {
    "date": ["date", "bill date", "invoice date", "voucher date", "vch date", "inv date"],
    "vtype": ["voucher type", "vch type", "type", "voucher"],
    "party": ["party", "party name", "supplier", "customer", "party ledger", "name", "supplier name",
              "customer name"],
    "gstin": ["party gstin", "gstin", "gst no", "gstin/uin", "gst number"],
    "state": ["state", "party state", "place of supply"],
    "bill_no": ["bill no", "invoice no", "bill number", "invoice number", "inv no", "reference", "ref no",
                "supplier invoice no"],
    "taxable": ["taxable", "taxable value", "taxable amount", "amount before tax", "basic amount", "value"],
    "rate": ["gst %", "gst rate", "rate", "tax rate", "gst%", "gst"],
    "cgst": ["cgst", "cgst amount"],
    "sgst": ["sgst", "sgst amount", "utgst", "sgst/utgst"],
    "igst": ["igst", "igst amount"],
    "cess": ["cess", "cess amount"],
    "round_off": ["round off", "roundoff", "rounding"],
    "total": ["total", "grand total", "invoice value", "bill amount", "invoice amount", "net amount"],
    "narration": ["narration", "remarks", "description", "particulars"],
    "ledger": ["ledger", "ledger name", "account", "purchase ledger", "sales ledger"],
    "debit": ["debit", "dr", "debit amount", "dr amount"],
    "credit": ["credit", "cr", "credit amount", "cr amount"],
    "vch_no": ["voucher no", "vch no", "voucher number", "entry no", "group"],
    "bill_ref": ["bill ref", "bill reference", "against ref", "agst ref"],
}


class ImportError_(ValueError):
    pass


def _norm(h):
    return re.sub(r"[^a-z0-9%/]+", " ", str(h or "").lower()).strip()


def map_headers(headers):
    out = {}
    normed = [_norm(h) for h in headers]
    for key, names in ALIASES.items():
        for i, h in enumerate(normed):
            if h in names and i not in out.values():
                out[key] = i
                break
    return out


def read_rows(filename, data):
    """-> list of rows (lists), first row = headings."""
    name = filename.lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        text = data.decode("utf-8-sig", errors="replace")
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t") if text.strip() else csv.excel
        return [r for r in csv.reader(io.StringIO(text), dialect)]
    if name.endswith(".xls"):
        raise ImportError_("Old .xls files are not supported. In Excel use File > Save As > Excel Workbook (.xlsx).")
    try:
        import openpyxl
    except ImportError:
        raise ImportError_("Excel support needs the 'openpyxl' package: pip install openpyxl")
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as e:
        raise ImportError_(f"Could not open the Excel file: {e}")
    # First sheet that has a recognisable heading row
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        for i, r in enumerate(rows[:15]):          # heading may sit below a title
            if len(map_headers(r)) >= 3:
                return rows[i:]
    raise ImportError_("No heading row found. Use the template (Excel Import > Download template).")


def _cell(row, idx):
    if idx is None or idx >= len(row):
        return None
    v = row[idx]
    return v.strip() if isinstance(v, str) else v


def _date(v):
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)) and 20000 < v < 80000:        # Excel serial date
        from datetime import timedelta
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).strftime("%Y-%m-%d")
    return ai_extract.norm_date(str(v or ""))


def _num(v):
    return ai_extract.num(v)


def _vtype(v, default):
    t = str(v or "").strip()
    if not t:
        return default
    low = t.lower()
    for key in ("purchase", "sales", "credit note", "debit note", "journal", "payment", "receipt", "contra"):
        if low.startswith(key) or low == key.replace(" ", ""):
            return " ".join(w.capitalize() for w in key.split())
    if low in ("sale", "sales invoice", "tax invoice"):
        return "Sales"
    return t                                                     # custom voucher type name


DOC_TYPE = {"Purchase": "purchase_invoice", "Sales": "sales_invoice", "Credit Note": "credit_note",
            "Debit Note": "debit_note"}


def parse(filename, data, settings, book):
    """-> (list of {name, rows, bill?, draft}, list of skipped messages)."""
    rows = read_rows(filename, data)
    if not rows:
        raise ImportError_("The file is empty")
    cols = map_headers(rows[0])
    body = [r for r in rows[1:] if any(c not in (None, "") for c in r)]
    if not body:
        raise ImportError_("No data rows below the headings")
    headers = [str(h or "") for h in rows[0]]
    if "ledger" in cols and ("debit" in cols or "credit" in cols):
        return _parse_ledger_lines(body, cols, headers, settings, book)
    if "party" in cols and ("taxable" in cols or "total" in cols):
        return _parse_bills(body, cols, headers, settings, book)
    raise ImportError_("Columns not recognised. A bill register needs Party + Taxable (or Total); "
                       "ledger lines need Ledger + Debit/Credit. Download the template to see the layout.")


def _show(row, headers):
    return {h: (v.strftime("%d-%m-%Y") if isinstance(v, (datetime, date)) else v)
            for h, v in zip(headers, row) if h and v not in (None, "")}


def _parse_bills(body, cols, headers, settings, book):
    own = (settings.get("own_gstin") or "").upper()
    groups, order, skipped = {}, [], []
    for n, r in enumerate(body, start=2):
        party = str(_cell(r, cols.get("party")) or "").strip()
        if not party:
            skipped.append(f"Row {n}: no party")
            continue
        vt = _vtype(_cell(r, cols.get("vtype")), "Purchase")
        key = (vt.lower(), party.lower(), str(_cell(r, cols.get("bill_no")) or "").strip().lower() or f"row{n}")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((n, r))

    out = []
    for key in order:
        rws = groups[key]
        first = rws[0][1]
        vt = _vtype(_cell(first, cols.get("vtype")), "Purchase")
        party = str(_cell(first, cols.get("party"))).strip()
        gstin = str(_cell(first, cols.get("gstin")) or "").replace(" ", "").upper()
        state = str(_cell(first, cols.get("state")) or "").strip()
        inter = bool(gstin and own and gstin[:2] != own[:2])
        breakup, ledgers, round_off, total, narr = [], {}, 0.0, 0.0, ""
        for n, r in rws:
            taxable = _num(_cell(r, cols.get("taxable")))
            rate = _num(_cell(r, cols.get("rate")))
            if 0 < rate < 1:                                      # 0.18 typed for 18%
                rate = round(rate * 100, 2)
            cg, sg, ig = (_num(_cell(r, cols.get(k))) for k in ("cgst", "sgst", "igst"))
            row_total = _num(_cell(r, cols.get("total")))
            if not taxable and row_total and rate:
                taxable = round(row_total / (1 + rate / 100), 2)
            if not (cg or sg or ig) and rate and taxable:
                tax = round(taxable * rate / 100, 2)
                if inter:
                    ig = tax
                else:
                    cg = sg = round(tax / 2, 2)
            if not rate and taxable and (cg or sg or ig):
                rate = round((cg + sg + ig) * 100 / taxable)
            breakup.append({"gst_rate": rate, "taxable_value": taxable, "cgst": cg, "sgst": sg, "igst": ig})
            round_off += _num(_cell(r, cols.get("round_off")))
            total += row_total
            led = str(_cell(r, cols.get("ledger")) or "").strip()
            if led:
                ledgers[rate] = led
            narr = narr or str(_cell(r, cols.get("narration")) or "").strip()
        cess = sum(_num(_cell(r, cols.get("cess"))) for _, r in rws)
        taxable_total = round(sum(b["taxable_value"] for b in breakup), 2)
        tax = sum(b["cgst"] + b["sgst"] + b["igst"] for b in breakup) + cess
        computed = round(taxable_total + tax + round_off, 2)
        if not total:
            total = float(round(computed))                         # round like a bill would
            round_off = round(round_off + total - computed, 2)
        bill_no = str(_cell(first, cols.get("bill_no")) or "").strip()
        is_sale = vt in ("Sales", "Credit Note")            # party is the customer
        raw = {
            "doc_type": DOC_TYPE.get(vt, "purchase_invoice"),
            "supplier_name": settings.get("company", "") if is_sale else party,
            "supplier_gstin": own if is_sale else gstin,
            "supplier_state": "" if is_sale else state,
            "buyer_name": party if is_sale else settings.get("company", ""),
            "buyer_gstin": gstin if is_sale else own,
            "buyer_state": state if is_sale else "",
            "invoice_number": bill_no, "invoice_date": _date(_cell(first, cols.get("date"))),
            "tax_breakup": breakup, "taxable_total": taxable_total,
            "cgst": sum(b["cgst"] for b in breakup), "sgst": sum(b["sgst"] for b in breakup),
            "igst": sum(b["igst"] for b in breakup), "cess": cess, "round_off": round_off,
            "grand_total": total, "narration": narr, "confidence": 1, "warnings": [],
        }
        bill = ai_extract.normalise(raw)
        draft = mapping.build_draft(bill, book, settings, as_typed=True)
        if vt not in DOC_TYPE:                                   # custom purchase-type voucher name
            draft["voucher_type"] = vt
        for e in draft["entries"]:                               # forced purchase / sales ledgers
            if e.get("kind") == "goods":
                m = re.search(r"([\d.]+)%", e.get("label", ""))
                rate = float(m.group(1)) if m else 0
                if rate in ledgers:
                    e["ledger"] = ledgers[rate]
                elif 0 in ledgers and len(ledgers) == 1:
                    e["ledger"] = ledgers[0]
        draft["new_ledgers"] = [n for n in draft["new_ledgers"]
                                if any(e["ledger"].lower() == n["name"].lower() for e in draft["entries"])]
        if not bill["invoice_date"]:
            draft["notes"].append("Date missing or not understood in the Excel row")
        out.append({"name": f"{party} {bill_no}".strip(), "rows": [_show(r, headers) for _, r in rws],
                    "row_numbers": [n for n, _ in rws], "bill": bill, "draft": draft})
    return out, []


def _parse_ledger_lines(body, cols, headers, settings, book):
    vouchers, cur, skipped = [], None, []

    def close(v):
        if v and v["lines"]:
            vouchers.append(v)
    for n, r in enumerate(body, start=2):
        ledger = str(_cell(r, cols.get("ledger")) or "").strip()
        dr, cr = _num(_cell(r, cols.get("debit"))), _num(_cell(r, cols.get("credit")))
        if not ledger or not (dr or cr):
            skipped.append(f"Row {n}: needs a ledger and a debit or credit amount")
            continue
        vno = str(_cell(r, cols.get("vch_no")) or "").strip()
        new_group = (cur is None or (vno and vno != cur["vno"]) or
                     (not vno and cur["balanced"] and cur["lines"]))
        if new_group:
            close(cur)
            cur = {"vno": vno, "lines": [], "rows": [], "row_numbers": [], "dr": 0.0, "cr": 0.0, "balanced": False,
                   "date": _date(_cell(r, cols.get("date"))),
                   "vtype": _vtype(_cell(r, cols.get("vtype")), "Journal"),
                   "narration": str(_cell(r, cols.get("narration")) or "").strip()}
        cur["lines"].append({"ledger": ledger, "dr_cr": "Dr" if dr else "Cr", "amount": dr or cr,
                             "bill_ref": str(_cell(r, cols.get("bill_ref")) or "").strip(), "kind": "manual",
                             "label": "",
                             # paying / receiving against an existing bill
                             "bill_type": "Agst Ref" if cur["vtype"] in ("Payment", "Receipt") else "New Ref"})
        cur["rows"].append(_show(r, headers))
        cur["row_numbers"].append(n)
        cur["dr"] += dr
        cur["cr"] += cr
        cur["balanced"] = abs(cur["dr"] - cur["cr"]) < 0.005
        cur["narration"] = cur["narration"] or str(_cell(r, cols.get("narration")) or "").strip()
    close(cur)

    out = []
    for v in vouchers:
        party = next((l["ledger"] for l in v["lines"]
                      if (book.get(l["ledger"]) or {}).get("primary") in ("Sundry Creditors", "Sundry Debtors")), "")
        notes = [] if v["balanced"] else [f"Debit {v['dr']:.2f} and Credit {v['cr']:.2f} do not match"]
        if not v["date"]:
            notes.append("Date missing or not understood in the Excel row")
        draft = {"voucher_type": v["vtype"], "voucher_base": v["vtype"], "date": v["date"],
                 "voucher_number": "", "reference": v["vno"], "reference_date": "",
                 "party_ledger": party, "party_gstin": "", "party_state": "", "narration": v["narration"],
                 "entries": v["lines"], "new_ledgers": [], "notes": notes}
        title = f"{v['vtype']} {v['vno']}".strip() + f" · {v['lines'][0]['ledger']}"
        out.append({"name": title, "rows": v["rows"], "row_numbers": v["row_numbers"], "bill": None, "draft": draft})
    return out, skipped


# ── templates ───────────────────────────────────────────────────────────────
TEMPLATES = {
    "bills": (["Date", "Voucher Type", "Party", "Party GSTIN", "Bill No", "Taxable", "GST %", "CGST", "SGST",
               "IGST", "Round Off", "Total", "Ledger", "Narration"],
              [["05-04-2026", "Purchase", "Sharma Steel Pvt Ltd", "24AAACS1234F1Z5", "SS/1042", 65218.06, 18,
                "", "", "", -0.30, 76957, "", "MS plates and angles"],
               ["18-04-2026", "Purchase", "Kaveri Electricals & Co.", "27AAKFK5521M1Z3", "KEC/0387", 19250, 12,
                "", "", 2310, "", "", "", "LED panels"],
               ["18-04-2026", "Purchase", "Kaveri Electricals & Co.", "27AAKFK5521M1Z3", "KEC/0387", 45492.50, 18,
                "", "", 8188.65, -0.15, 75241, "", "Second GST rate of the same bill"],
               ["21-05-2026", "Sales", "Patel Hardware Stores", "24ABQPP9087L1Z2", "DT/S/0915", 25075, 18,
                "", "", "", -0.50, 29588, "", ""]]),
    "ledger": (["Voucher No", "Date", "Voucher Type", "Ledger", "Debit", "Credit", "Bill Ref", "Narration"],
               [["P-1", "10-05-2026", "Payment", "Sharma Steel Pvt Ltd", 76957, "", "SS/1042", "Paid by NEFT"],
                ["P-1", "10-05-2026", "Payment", "HDFC Bank", "", 76957, "", ""],
                ["J-1", "31-05-2026", "Journal", "Salary", 85000, "", "", "May salary provision"],
                ["J-1", "31-05-2026", "Journal", "Salary Payable", "", 85000, "", ""],
                ["R-1", "12-05-2026", "Receipt", "Cash", 29588, "", "", "Patel Hardware - bill DT/S/0915"],
                ["R-1", "12-05-2026", "Receipt", "Patel Hardware Stores", "", 29588, "DT/S/0915", ""]]),
}


def template_xlsx(kind):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    heads, rows = TEMPLATES[kind]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bills" if kind == "bills" else "Vouchers"
    ws.append(heads)
    for r in rows:
        ws.append(r)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2457D6")
        c.alignment = Alignment(vertical="center")
    for i, h in enumerate(heads, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = max(12, len(h) + 4)
    ws.column_dimensions["C" if kind == "bills" else "D"].width = 30
    ws.freeze_panes = "A2"
    notes = wb.create_sheet("How to fill")
    tips = (["One row per bill. A bill with two GST rates = two rows with the same Party + Bill No.",
             "Leave CGST / SGST / IGST blank to have them worked out from GST % and the GSTIN state code.",
             "Total blank = taxable + tax, rounded to the rupee.",
             "Ledger (optional) = purchase / sales ledger to use for that row. Blank = picked automatically.",
             "Voucher Type: Purchase, Sales, Credit Note, Debit Note or your own voucher type name."]
            if kind == "bills" else
            ["One row per ledger line. Rows with the same Voucher No make one voucher.",
             "Each voucher's Debit total must equal its Credit total.",
             "Voucher Type: Journal, Payment, Receipt, Contra, or any voucher type in your Tally.",
             "Bill Ref (optional) = bill-wise reference for party ledgers (Agst Ref / New Ref name)."])
    for t in tips:
        notes.append([t])
    notes.column_dimensions["A"].width = 110
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
