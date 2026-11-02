"""Turn an AI-read bill into a ready-to-post Tally voucher draft, using the
company's own ledgers fetched from Tally.

* Party: matched by GSTIN first, then by fuzzy name. No match -> a new ledger
  is proposed (created on push if the user allows it).
* Purchase / Sales ledger: per GST rate where the company has rate-wise
  ledgers ("Purchase @18%", "GST Sales 12%" ...), else the default from Settings.
* CGST / SGST / IGST: input ledgers for purchases, output ledgers for sales,
  rate-wise where they exist ("Input CGST 9%").
* Round off, freight, cess, TDS handled when present.
* The draft always balances: tiny differences (< Rs 1) go to Round Off;
  anything larger is flagged for the user.
"""
import re
from collections import OrderedDict
from difflib import SequenceMatcher

SUFFIXES = r"\b(m/s|messrs|pvt|private|ltd|limited|llp|co|company|corp|corporation|inc|the|and|&)\b"

VTYPE_BY_DOC = {
    "purchase_invoice": "Purchase",
    "expense_receipt": "Purchase",
    "sales_invoice": "Sales",
    "credit_note": "Credit Note",
    "debit_note": "Debit Note",
    "other": "Journal",
}

# Words that identify each GST ledger, tried in order ("Cgst", "Input CGST 9%", "Central Tax"...)
TAX_ALIASES = {
    "cgst": ["cgst", "central tax", "central gst", "c gst"],
    "sgst": ["sgst", "utgst", "state tax", "state gst", "s gst"],
    "igst": ["igst", "integrated tax", "integrated gst", "i gst"],
    "cess": ["cess"],
}

# Which side the party sits on, per voucher base type
PARTY_SIDE = {"Purchase": "Cr", "Sales": "Dr", "Credit Note": "Cr", "Debit Note": "Dr", "Journal": "Cr"}


def clean_name(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9& ]+", " ", s)
    s = re.sub(SUFFIXES, " ", s)
    return re.sub(r"\s+", " ", s).strip()


def primary_group(parent, groups, _depth=0):
    """Walk up the group tree to the primary group ('Sundry Creditors', 'Duties & Taxes' ...)."""
    if not parent or _depth > 20:
        return parent
    up = groups.get(parent)
    if not up or up in ("Primary", "\x04 Primary", parent):
        return parent
    return primary_group(up, groups, _depth + 1)


class LedgerBook:
    def __init__(self, ledgers, groups=None):
        groups = groups or {}
        self.all = []
        for l in ledgers:
            l = dict(l)
            l["primary"] = primary_group(l.get("parent", ""), groups)
            self.all.append(l)
        self.names = {l["name"].lower(): l for l in self.all}

    def exists(self, name):
        return (name or "").lower() in self.names

    def get(self, name):
        return self.names.get((name or "").lower())

    def in_groups(self, *prim):
        return [l for l in self.all if l["primary"] in prim or l.get("parent") in prim]

    def find_party(self, name, gstin):
        if gstin:
            for l in self.all:
                if l.get("gstin") == gstin:
                    return l["name"], 1.0
        target = clean_name(name)
        if not target:
            return None, 0
        best, score = None, 0.0
        pool = self.in_groups("Sundry Creditors", "Sundry Debtors") or self.all
        for l in pool:
            c = clean_name(l["name"])
            if not c:
                continue
            s = 1.0 if c == target else SequenceMatcher(None, c, target).ratio()
            if s > score:
                best, score = l["name"], s
        return (best, score) if score >= 0.86 else (None, score)

    def pick(self, candidates, must=(), prefer=(), avoid=(), rate=None):
        """Best ledger name from candidates by keyword scoring."""
        best, best_score = None, -1
        for l in candidates:
            n = l["name"].lower()
            if any(m not in n for m in must):
                continue
            s = sum(2 for p in prefer if p in n) - sum(3 for a in avoid if a in n)
            if rate is not None:
                rates = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%?", n)]
                if rates:
                    s += 4 if any(abs(r - rate) < 0.01 for r in rates) else -5
            if s > best_score:
                best, best_score = l["name"], s
        return best


def _dmy(iso):
    """'2026-04-05' -> '05-04-2026' (how Indian users read dates)."""
    p = iso.split("-")
    return f"{p[2]}-{p[1]}-{p[0]}" if len(p) == 3 else iso


def _fmt_rate(r):
    return f"{r:g}"


def tax_rows(bill):
    """-> list of {rate, taxable, cgst, sgst, igst}, grouped by GST rate."""
    rows = OrderedDict()
    if bill.get("tax_breakup"):
        for t in bill["tax_breakup"]:
            r = rows.setdefault(t["gst_rate"], {"rate": t["gst_rate"], "taxable": 0, "cgst": 0, "sgst": 0, "igst": 0})
            for k_src, k_dst in (("taxable_value", "taxable"), ("cgst", "cgst"), ("sgst", "sgst"), ("igst", "igst")):
                r[k_dst] += t[k_src]
    elif bill.get("items") and len({i["gst_rate"] for i in bill["items"]}) > 1:
        total_tax = bill["cgst"] + bill["sgst"] + bill["igst"]
        base = sum(i["taxable_value"] for i in bill["items"]) or 1
        inter = bill["igst"] > 0
        for i in bill["items"]:
            r = rows.setdefault(i["gst_rate"], {"rate": i["gst_rate"], "taxable": 0, "cgst": 0, "sgst": 0, "igst": 0})
            r["taxable"] += i["taxable_value"]
            tax = i["taxable_value"] * i["gst_rate"] / 100
            if inter:
                r["igst"] += tax
            else:
                r["cgst"] += tax / 2
                r["sgst"] += tax / 2
        # keep printed tax totals authoritative: scale computed tax to them
        calc = sum(r["cgst"] + r["sgst"] + r["igst"] for r in rows.values())
        if calc and total_tax:
            f = total_tax / calc
            for r in rows.values():
                for k in ("cgst", "sgst", "igst"):
                    r[k] *= f
        if abs(base - bill["taxable_total"]) > 1 and bill["taxable_total"]:
            f = bill["taxable_total"] / base
            for r in rows.values():
                r["taxable"] *= f
    else:
        taxable = bill["taxable_total"]
        tax = bill["cgst"] + bill["sgst"] + bill["igst"]
        rate = round(tax * 100 / taxable) if taxable else 0
        if bill.get("items") and bill["items"][0]["gst_rate"]:
            rate = bill["items"][0]["gst_rate"]
        rows[rate] = {"rate": rate, "taxable": taxable, "cgst": bill["cgst"], "sgst": bill["sgst"], "igst": bill["igst"]}
    for r in rows.values():
        for k in ("taxable", "cgst", "sgst", "igst"):
            r[k] = round(r[k], 2)
    return list(rows.values())


def build_draft(bill, book, settings=None):
    """bill: normalised dict from ai_extract. book: LedgerBook. -> voucher draft dict."""
    st = settings or {}
    vt_base = VTYPE_BY_DOC.get(bill["doc_type"], "Purchase")
    if vt_base == "Journal":
        vt_base = "Purchase"                       # unknown docs are usually expenses
    own_gstin = (st.get("own_gstin") or "").upper()
    # If the AI guessed the direction wrong, our own GSTIN settles it
    if own_gstin:
        if bill["supplier_gstin"] == own_gstin and vt_base == "Purchase":
            vt_base = "Sales"
        elif bill["buyer_gstin"] == own_gstin and vt_base == "Sales":
            vt_base = "Purchase"
    is_purchase_side = vt_base in ("Purchase", "Debit Note")
    goods_side = "Purchase" if is_purchase_side else "Sales"
    vt_name = (st.get("vtype_map") or {}).get(vt_base) or vt_base

    if is_purchase_side:
        p_name, p_gstin, p_state = bill["supplier_name"], bill["supplier_gstin"], bill["supplier_state"]
    else:
        p_name, p_gstin, p_state = bill["buyer_name"], bill["buyer_gstin"], bill["buyer_state"]

    notes = []
    new_ledgers = []
    party, score = book.find_party(p_name, p_gstin)
    if not party:
        party = (p_name or "Unknown Party").strip()[:90]
        if not book.exists(party):
            new_ledgers.append({"name": party, "parent": "Sundry Creditors" if is_purchase_side else "Sundry Debtors",
                                "gstin": p_gstin, "state": p_state, "role": "party"})
            notes.append(f"Party '{party}' not found in Tally - will be created")
    elif score < 1.0:
        notes.append(f"Party matched by name to '{party}' ({score:.0%})")

    # Side of each kind of line: party on one side, everything else on the other
    party_side = PARTY_SIDE[vt_base]
    other_side = "Dr" if party_side == "Cr" else "Cr"

    def ledger_for(kind, rate=None):
        """Find or propose a ledger for a kind of line."""
        default_key = {"goods": f"default_{goods_side.lower()}_ledger",
                       "roundoff": "default_roundoff_ledger", "freight": "default_freight_ledger"}.get(kind)
        if kind == "goods":
            pool = book.in_groups(f"{goods_side} Accounts")
            if bill["doc_type"] == "expense_receipt" and is_purchase_side:
                pool = book.in_groups("Indirect Expenses", "Direct Expenses") + pool
            prefer = ["gst", "inter" if bill["igst"] else "local"]
            avoid = ["exempt", "nil", "import", "export"] if rate else []
            name = book.pick(pool, rate=rate, prefer=prefer, avoid=avoid)
            # A rate-specific ledger for a different rate would be wrong
            if name and re.search(r"\d+(?:\.\d+)?\s*%", name) and (not rate or _fmt_rate(rate) + "%" not in name.replace(" ", "")):
                name = None
            if not name:
                name = st.get(default_key) or (pool[0]["name"] if pool else None) \
                    or (f"{goods_side} @ {_fmt_rate(rate)}%" if rate else goods_side)
            return name, f"{goods_side} Accounts", None
        if kind in ("cgst", "sgst", "igst", "cess"):
            pool = book.in_groups("Duties & Taxes")
            io_pref, io_avoid = (["input"], ["output"]) if is_purchase_side else (["output"], ["input"])
            # Every alias must appear in the name: "sgst" never matches a "Cgst" ledger
            name = None
            for alias in TAX_ALIASES[kind]:
                name = book.pick(pool, must=alias.split(), prefer=io_pref, avoid=io_avoid, rate=rate)
                if name:
                    break
            if not name:
                pre = "Input" if is_purchase_side else "Output"
                name = f"{pre} {kind.upper()}" + (f" {_fmt_rate(rate)}%" if rate else "")
            return name, "Duties & Taxes", kind
        if kind == "tds":
            pool = book.in_groups("Duties & Taxes", "Current Liabilities")
            return book.pick(pool, must=["tds"]) or "TDS Payable", "Duties & Taxes", "tds"
        if kind == "roundoff":
            return (st.get(default_key) or book.pick(book.all, must=["round"]) or "Round Off"), "Indirect Expenses", None
        if kind == "freight":
            pool = book.in_groups("Indirect Expenses", "Direct Expenses", "Indirect Incomes")
            return (st.get(default_key) or book.pick(pool, must=["freight"]) or book.pick(pool, must=["transport"])
                    or "Freight Charges"), "Indirect Expenses", None
        raise ValueError(kind)

    entries = []

    def add(kind, amount, side, rate=None, label=""):
        amount = round(amount, 2)
        if abs(amount) < 0.005:
            return
        name, parent, head = ledger_for(kind, rate)
        if amount < 0:                      # negative line flips side
            amount, side = -amount, ("Cr" if side == "Dr" else "Dr")
        entries.append({"ledger": name, "dr_cr": side, "amount": amount, "kind": kind, "label": label})
        if not book.exists(name) and not any(n["name"].lower() == name.lower() for n in new_ledgers):
            new_ledgers.append({"name": name, "parent": parent, "tax_head": head, "role": kind})

    for r in tax_rows(bill):
        rate = r["rate"] or None
        add("goods", r["taxable"], other_side, rate, f"Taxable {_fmt_rate(r['rate'])}%")
        half = (r["rate"] / 2) if r["rate"] else None
        add("cgst", r["cgst"], other_side, half, "CGST")
        add("sgst", r["sgst"], other_side, half, "SGST")
        add("igst", r["igst"], other_side, r["rate"] or None, "IGST")
    add("cess", bill["cess"], other_side, None, "Cess")
    add("freight", bill["other_charges"], other_side, None, "Other charges")

    party_amt = round(bill["grand_total"] - bill.get("tds", 0), 2)
    if bill.get("tds"):
        add("tds", bill["tds"], party_side, None, "TDS")
    bill_ref = bill["invoice_number"] if is_purchase_side else (bill["invoice_number"] or "")
    entries.insert(0, {"ledger": party, "dr_cr": party_side, "amount": party_amt, "kind": "party",
                       "label": "Party", "bill_ref": bill_ref})

    # Balance: the party line carries the printed grand total, so whatever is
    # left after taxable + taxes + charges is the round-off (printed or not).
    def diff():
        dr = sum(e["amount"] for e in entries if e["dr_cr"] == "Dr")
        cr = sum(e["amount"] for e in entries if e["dr_cr"] == "Cr")
        return round(dr - cr, 2)
    gap = diff()
    if gap and (abs(gap) <= 1.0 or abs(abs(gap) - abs(bill["round_off"])) < 0.02):
        add("roundoff", abs(gap), "Cr" if gap > 0 else "Dr", None, "Round off")
        gap = diff()
    if gap:
        notes.append(f"Voucher is out of balance by {gap:+.2f} - please check the amounts")

    # Only propose ledgers that are still used
    used = {e["ledger"].lower() for e in entries}
    new_ledgers = [n for n in new_ledgers if n["name"].lower() in used]
    for n in new_ledgers:
        n["create"] = True

    narration = bill.get("narration") or ""
    if bill["invoice_number"]:
        narration = (f"Bill No. {bill['invoice_number']}"
                     + (f" dt {_dmy(bill['invoice_date'])}" if bill["invoice_date"] else "")
                     + (f" - {narration}" if narration else ""))
    return {
        "voucher_type": vt_name,
        "voucher_base": vt_base,
        "date": bill["invoice_date"],
        "voucher_number": "" if is_purchase_side else bill["invoice_number"],
        "reference": bill["invoice_number"] if is_purchase_side else "",
        "reference_date": bill["invoice_date"] if is_purchase_side else "",
        "party_ledger": party,
        "party_gstin": p_gstin,
        "party_state": p_state,
        "narration": narration[:500],
        "entries": entries,
        "new_ledgers": new_ledgers,
        "notes": notes,
    }
