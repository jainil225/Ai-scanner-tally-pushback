"""A tiny stand-in for TallyPrime's XML server, for tests.

Answers Company / Ledger / Group / VoucherType collections and records every
Import Data request (ledgers and vouchers) so tests can inspect what was sent.
"""
import re
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COMPANY = "Demo Traders"
LEDGERS = [
    ("Sharma Steel Pvt Ltd", "Sundry Creditors", "24AAACS1234F1Z5"),
    ("Purchase @ 18%", "Purchase Accounts", ""),
    ("Purchase @ 12%", "Purchase Accounts", ""),
    ("Sales GST", "Sales Accounts", ""),
    ("Input CGST 9%", "Input GST", ""),
    ("Input SGST 9%", "Input GST", ""),
    ("Input CGST 6%", "Input GST", ""),
    ("Input SGST 6%", "Input GST", ""),
    ("Input IGST", "Input GST", ""),
    ("Output CGST", "Duties & Taxes", ""),
    ("Output SGST", "Duties & Taxes", ""),
    ("Round Off", "Indirect Expenses", ""),
    ("Freight Inward", "Direct Expenses", ""),
    ("Cash", "Cash-in-Hand", ""),
]
# Mirrors real Tally: Duties & Taxes and Sundry Creditors sit under Current Liabilities
GROUPS = {"Input GST": "Duties & Taxes", "Duties & Taxes": "Current Liabilities", "Sundry Creditors": "Current Liabilities",
          "Current Liabilities": "", "Purchase Accounts": "", "Sales Accounts": "",
          "Indirect Expenses": "", "Direct Expenses": "", "Cash-in-Hand": "Current Assets"}


class State:
    def __init__(self):
        self.ledgers = [dict(name=n, parent=p, gstin=g) for n, p, g in LEDGERS]
        self.imports = []          # raw XML bodies of Import Data requests
        self.groups = dict(GROUPS)
        self.units = ["Nos", "Kg"]
        self.items = []
        self.reject_vouchers = False


def _x(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.end_headers()
            self.wfile.write(self.answer(body).encode("utf-8"))

        def answer(self, body):
            if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in body:
                state.imports.append(body)
                root = ET.fromstring(body.replace("&#4;", ""))   # Tally's special char, invalid in XML 1.0
                created, errors, msgs = 0, 0, []
                for g in root.iter("GROUP"):
                    state.groups[g.get("NAME")] = g.findtext("PARENT") or ""
                    created += 1
                for u in root.iter("UNIT"):
                    state.units.append(u.get("NAME"))
                    created += 1
                for it in root.iter("STOCKITEM"):
                    state.items.append({"name": it.get("NAME"), "parent": it.findtext("PARENT") or "",
                                        "unit": it.findtext("BASEUNITS") or ""})
                    created += 1
                for led in root.iter("LEDGER"):
                    state.ledgers.append({"name": led.get("NAME"), "parent": led.findtext("PARENT"),
                                          "gstin": led.findtext("PARTYGSTIN") or ""})
                    created += 1
                names = {l["name"] for l in state.ledgers}
                for v in root.iter("VOUCHER"):
                    missing = [e.findtext("LEDGERNAME") for e in v.iter("ALLLEDGERENTRIES.LIST")
                               if e.findtext("LEDGERNAME") not in names]
                    if state.reject_vouchers or missing:
                        errors += 1
                        msgs.append(f"Ledger '{missing[0]}' does not exist!" if missing else "Voucher totals do not match!")
                    else:
                        created += 1
                errs = "".join(f"<LINEERROR>{_x(m)}</LINEERROR>" for m in msgs)
                return (f"<RESPONSE><CREATED>{created}</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED>"
                        f"<LASTVCHID>{100 + len(state.imports)}</LASTVCHID><LASTMID>0</LASTMID><COMBINED>0</COMBINED>"
                        f"<IGNORED>0</IGNORED><ERRORS>{errors}</ERRORS><CANCELLED>0</CANCELLED>{errs}</RESPONSE>")
            m = re.search(r"<TYPE>(\w+)</TYPE><FETCH>", body)
            typ = m.group(1) if m else ""
            if typ == "Company":
                items = f'<COMPANY NAME="{_x(COMPANY)}"><NAME>{_x(COMPANY)}</NAME></COMPANY>'
            elif typ == "Ledger":
                items = "".join(f'<LEDGER NAME="{_x(l["name"])}"><PARENT>{_x(l["parent"])}</PARENT>'
                                f'<PARTYGSTIN>{_x(l["gstin"])}</PARTYGSTIN></LEDGER>' for l in state.ledgers)
            elif typ == "Group":
                items = "".join(f'<GROUP NAME="{_x(n)}"><PARENT>{_x(p)}</PARENT></GROUP>' for n, p in state.groups.items())
            elif typ == "Unit":
                items = "".join(f'<UNIT NAME="{_x(n)}"><NAME>{_x(n)}</NAME></UNIT>' for n in state.units)
            elif typ == "StockItem":
                items = "".join(f'<STOCKITEM NAME="{_x(i["name"])}"><PARENT>{_x(i["parent"])}</PARENT>'
                                f'<BASEUNITS>{_x(i["unit"])}</BASEUNITS></STOCKITEM>' for i in state.items)
            elif typ == "StockGroup":
                items = '<STOCKGROUP NAME="Steel"><PARENT></PARENT></STOCKGROUP>'
            elif typ == "VoucherType":
                items = "".join(f'<VOUCHERTYPE NAME="{n}"><PARENT>{n}</PARENT></VOUCHERTYPE>'
                                for n in ("Purchase", "Sales", "Journal", "Credit Note", "Debit Note"))
            else:
                items = ""
            return f"<ENVELOPE><BODY><DATA><COLLECTION>{items}</COLLECTION></DATA></BODY></ENVELOPE>"
    return H


def start():
    state = State()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state, f"http://127.0.0.1:{srv.server_address[1]}"
