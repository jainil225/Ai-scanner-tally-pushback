"""Run:  python -m unittest discover -s tests -v   (from the project folder)"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

_tmp = tempfile.mkdtemp(prefix="scanner_test_")
os.environ["SCANNER_DATA"] = _tmp

import ai_extract  # noqa: E402
import fake_tally  # noqa: E402
import file_prep  # noqa: E402
import mapping  # noqa: E402
import tally_push  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    import fitz


def make_pdf(pages=2):
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 72), f"TAX INVOICE page {i + 1}")
    return doc.tobytes()


def make_png():
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30), 1)
    pix.clear_with(200)
    return pix.tobytes("png")


def bill(**kw):
    b = {
        "doc_type": "purchase_invoice", "supplier_name": "Sharma Steel Private Limited",
        "supplier_gstin": "24AAACS1234F1Z5", "supplier_state": "Gujarat",
        "buyer_name": "Demo Traders", "buyer_gstin": "24AAAAA0000A1Z5", "invoice_number": "SS/1042",
        "invoice_date": "05/04/2026", "items": [
            {"description": "MS Plate", "hsn_sac": "7208", "quantity": 10, "rate": 1000,
             "taxable_value": 10000, "gst_rate": 18}],
        "taxable_total": 10000, "cgst": 900, "sgst": 900, "igst": 0, "round_off": 0,
        "grand_total": 11800, "confidence": 0.95, "warnings": [],
    }
    b.update(kw)
    return ai_extract.normalise(b)


def balance(entries):
    dr = sum(e["amount"] for e in entries if e["dr_cr"] == "Dr")
    cr = sum(e["amount"] for e in entries if e["dr_cr"] == "Cr")
    return round(dr - cr, 2)


def book_from_fake():
    t = fake_tally.State()
    return mapping.LedgerBook(t.ledgers, fake_tally.GROUPS)


class FilePrepTests(unittest.TestCase):
    def test_pdf_pages_become_jpgs(self):
        docs, skipped = file_prep.load_file("bill.pdf", make_pdf(3))
        self.assertEqual(len(docs), 1)
        self.assertEqual(len(docs[0].pages), 3)
        self.assertTrue(all(p[:2] == b"\xff\xd8" for p in docs[0].pages))   # JPEG magic
        self.assertEqual(skipped, [])

    def test_png_converted(self):
        docs, _ = file_prep.load_file("a.png", make_png())
        self.assertEqual(docs[0].pages[0][:2], b"\xff\xd8")

    def test_zip_mixed(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("bills/one.pdf", make_pdf(1))
            z.writestr("bills/two.png", make_png())
            z.writestr("bills/readme.txt", "hello")
            z.writestr("__MACOSX/._one.pdf", "junk")
        docs, skipped = file_prep.load_file("batch.zip", buf.getvalue())
        self.assertEqual([d.name for d in docs], ["batch.zip/bills/one.pdf", "batch.zip/bills/two.png"])
        self.assertEqual(skipped, ["bills/readme.txt"])

    def test_bad_file(self):
        with self.assertRaises(file_prep.PrepError):
            file_prep.load_file("x.pdf", b"not a pdf")


class ExtractTests(unittest.TestCase):
    def test_parse_fenced_json(self):
        self.assertEqual(ai_extract.parse_json_reply('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(ai_extract.parse_json_reply('Here you go: {"a": 2} hope it helps'), {"a": 2})

    def test_normalise(self):
        b = ai_extract.normalise({"invoice_date": "05-Apr-26", "grand_total": "₹ 1,180.00",
                                  "taxable_total": "1,000", "cgst": "90", "sgst": "90",
                                  "supplier_gstin": "24aaacs 1234f1z5", "doc_type": "weird"})
        self.assertEqual(b["invoice_date"], "2026-04-05")
        self.assertEqual(b["grand_total"], 1180.0)
        self.assertEqual(b["supplier_gstin"], "24AAACS1234F1Z5")
        self.assertEqual(b["doc_type"], "purchase_invoice")
        self.assertFalse(any("do not add up" in w for w in b["warnings"]))

    def test_totals_warning(self):
        b = ai_extract.normalise({"taxable_total": 1000, "cgst": 90, "sgst": 90, "grand_total": 2000})
        self.assertTrue(any("do not add up" in w for w in b["warnings"]))

    def test_gemini_request_shape(self):
        sent = {}

        def fake(url, headers, payload):
            sent.update(url=url, headers=headers, payload=payload)
            return {"candidates": [{"content": {"parts": [{"text": json.dumps({"grand_total": 5})}]}}]}
        orig = ai_extract._post_with_retry
        ai_extract._post_with_retry = fake
        try:
            out = ai_extract.extract([b"\xff\xd8jpg"], "gemini", "KEY")
        finally:
            ai_extract._post_with_retry = orig
        self.assertIn(ai_extract.PROVIDERS["gemini"]["default"] + ":generateContent", sent["url"])
        self.assertEqual(sent["headers"]["x-goog-api-key"], "KEY")
        self.assertEqual(sent["payload"]["contents"][0]["parts"][1]["inline_data"]["mime_type"], "image/jpeg")
        self.assertEqual(out["grand_total"], 5)

    def test_claude_request_shape(self):
        sent = {}

        def fake(url, headers, payload):
            sent.update(url=url, headers=headers, payload=payload)
            return {"content": [{"type": "text", "text": '{"grand_total": 7}'}]}
        orig = ai_extract._post_with_retry
        ai_extract._post_with_retry = fake
        try:
            out = ai_extract.extract([b"jpg1", b"jpg2"], "claude", "sk")
        finally:
            ai_extract._post_with_retry = orig
        self.assertEqual(sent["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(sent["headers"]["x-api-key"], "sk")
        content = sent["payload"]["messages"][0]["content"]
        self.assertEqual([c["type"] for c in content], ["image", "image", "text"])
        self.assertEqual(out["grand_total"], 7)

    def _with_fake(self, fake, fn):
        orig = ai_extract._post_with_retry
        ai_extract._post_with_retry = fake
        try:
            return fn()
        finally:
            ai_extract._post_with_retry = orig

    def test_retired_model_switches_to_suggested(self):
        urls = []
        ok = {"candidates": [{"content": {"parts": [{"text": '{"grand_total": 9}'}]}}]}

        def fake(url, headers, payload):
            urls.append(url)
            if "gemini-2.5-flash:" in url:
                raise ai_extract.AIError(
                    "AI request failed (404): This model models/gemini-2.5-flash is no longer available to new "
                    "users. Please update your code to use models/gemini-3.8-flash for the latest features.", status=404)
            return ok
        out = self._with_fake(fake, lambda: ai_extract.extract([b"j"], "gemini", "K", "gemini-2.5-flash"))
        self.assertIn("gemini-3.8-flash:generateContent", urls[-1])
        self.assertEqual(out["_model"], "gemini-3.8-flash")
        self.assertEqual(out["grand_total"], 9)

    def test_retired_model_without_hint_uses_model_list(self):
        ok = {"candidates": [{"content": {"parts": [{"text": '{"grand_total": 3}'}]}}]}

        def fake(url, headers, payload):
            if "old-model:" in url:
                raise ai_extract.AIError("AI request failed (404): model not found", status=404)
            return ok
        orig_list = ai_extract.gemini_models
        ai_extract.gemini_models = lambda key: ["gemini-9.0-flash", "gemini-9.0-pro"]
        try:
            out = self._with_fake(fake, lambda: ai_extract.extract([b"j"], "gemini", "K", "old-model"))
        finally:
            ai_extract.gemini_models = orig_list
        self.assertEqual(out["_model"], "gemini-9.0-flash")

    def test_model_ranking_prefers_stable_newest_flash(self):
        names = ["gemini-3.8-pro", "gemini-3.8-flash-lite", "gemini-4.0-flash-preview", "gemini-3.8-flash",
                 "gemini-3.5-flash", "text-embedding-004", "gemini-3.8-flash-image"]
        self.assertEqual(sorted(names, key=ai_extract._gemini_rank)[0], "gemini-3.8-flash")

    def test_other_errors_not_retried(self):
        calls = []

        def fake(url, headers, payload):
            calls.append(url)
            raise ai_extract.AIError("API key rejected (403): bad key", status=403)
        with self.assertRaises(ai_extract.AIError):
            self._with_fake(fake, lambda: ai_extract.extract([b"j"], "gemini", "K"))
        self.assertEqual(len(calls), 1)

    def test_no_key(self):
        with self.assertRaises(ai_extract.AIError):
            ai_extract.extract([b"x"], "gemini", "")


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class RateLimitTests(unittest.TestCase):
    OK = {"candidates": [{"content": {"parts": [{"text": '{"grand_total": 1}'}]}}]}
    URL = "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent"

    def setUp(self):
        self.clock = [1000.0]
        self.sleeps = []
        self.orig = (ai_extract.requests.post, ai_extract.time.sleep, ai_extract.time.time, ai_extract.GEMINI_RPM)
        ai_extract.time.sleep = lambda s: (self.sleeps.append(round(s, 1)), self.clock.__setitem__(0, self.clock[0] + s))
        ai_extract.time.time = lambda: self.clock[0]
        ai_extract._recent.clear()

    def tearDown(self):
        ai_extract.requests.post, ai_extract.time.sleep, ai_extract.time.time, ai_extract.GEMINI_RPM = self.orig
        ai_extract._recent.clear()

    def per_minute_429(self):
        return FakeResp(429, {"error": {"code": 429, "message":
            "You exceeded your current quota. Quota exceeded for metric: generativelanguage.googleapis.com/"
            "generate_content_free_tier_requests, limit: 5, model: gemini-3.8-flash. Please retry in 58.137388134s.",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "58s"}]}})

    def test_waits_for_google_retry_delay_then_succeeds(self):
        ai_extract.GEMINI_RPM = 0
        replies = [self.per_minute_429(), FakeResp(200, self.OK)]
        ai_extract.requests.post = lambda *a, **k: replies.pop(0)
        self.assertEqual(ai_extract._post_with_retry(self.URL, {}, {}), self.OK)
        self.assertEqual(self.sleeps, [59.0])

    def test_daily_quota_fails_fast_with_clear_message(self):
        ai_extract.GEMINI_RPM = 0
        calls = []
        body = {"error": {"code": 429, "message": "Quota exceeded for metric: "
                          "generativelanguage.googleapis.com/generate_content_free_tier_requests_per_day, limit: 20"}}
        ai_extract.requests.post = lambda *a, **k: (calls.append(1), FakeResp(429, body))[1]
        with self.assertRaises(ai_extract.AIError) as cm:
            ai_extract._post_with_retry(self.URL, {}, {})
        self.assertIn("Daily free limit", str(cm.exception))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_paces_under_requests_per_minute(self):
        ai_extract.GEMINI_RPM = 5
        ai_extract.requests.post = lambda *a, **k: FakeResp(200, self.OK)
        for _ in range(6):
            ai_extract._post_with_retry(self.URL, {}, {})
        self.assertEqual(len(self.sleeps), 1)          # 6th request waited for the minute to roll over
        self.assertGreaterEqual(self.sleeps[0], 60)

    def test_claude_not_paced(self):
        ai_extract.GEMINI_RPM = 1
        ai_extract.requests.post = lambda *a, **k: FakeResp(200, {"content": []})
        for _ in range(3):
            ai_extract._post_with_retry("https://api.anthropic.com/v1/messages", {}, {})
        self.assertEqual(self.sleeps, [])


class OverloadTests(unittest.TestCase):
    OK = {"candidates": [{"content": {"parts": [{"text": '{"grand_total": 4}'}]}}]}
    BUSY = {"error": {"code": 503, "message": "This model is currently experiencing high demand. Spikes in demand "
                      "are usually temporary. Please try again later.", "status": "UNAVAILABLE"}}

    def setUp(self):
        self.orig = (ai_extract.requests.post, ai_extract.time.sleep, ai_extract.gemini_models, ai_extract.GEMINI_RPM)
        self.sleeps, self.urls = [], []
        ai_extract.time.sleep = lambda x: self.sleeps.append(x)
        ai_extract.gemini_models = lambda key: ["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.8-flash-lite"]
        ai_extract.GEMINI_RPM = 0

    def tearDown(self):
        ai_extract.requests.post, ai_extract.time.sleep, ai_extract.gemini_models, ai_extract.GEMINI_RPM = self.orig

    def serve(self, busy_models):
        def post(url, **k):
            self.urls.append(url)
            if "anthropic" in url:
                return FakeResp(200, {"content": [{"type": "text", "text": '{"grand_total": 8}'}]})
            busy = any(f"/{m}:" in url for m in busy_models)
            return FakeResp(503, self.BUSY) if busy else FakeResp(200, self.OK)
        ai_extract.requests.post = post

    def test_busy_model_retries_then_switches_model(self):
        self.serve(["gemini-3.8-flash"])
        out = ai_extract.extract([b"j"], "gemini", "K", "gemini-3.8-flash")
        self.assertEqual(out["_model"], "gemini-3.5-flash")
        self.assertEqual(out["_switched"], "busy")
        self.assertEqual(sum("gemini-3.8-flash:" in u for u in self.urls), 3)   # 1 try + 2 retries
        self.assertEqual(len(self.sleeps), 2)

    def test_all_gemini_busy_falls_back_to_claude(self):
        import scanner
        self.serve(["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.8-flash-lite"])
        s = dict(scanner.DEFAULT_SETTINGS, gemini_key="G", claude_key="C")
        bill = scanner.read_bill([b"j"], s, "gemini", "G", "")
        self.assertEqual(bill["grand_total"], 8)
        self.assertIn("Read by Claude because Gemini was busy", bill["warnings"][0])
        self.assertNotEqual(scanner.load_settings().get("model"), "gemini-3.5-flash")   # busy switch not saved

    def test_all_busy_without_backup_key_gives_clear_message(self):
        import scanner
        self.serve(["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.8-flash-lite"])
        s = dict(scanner.DEFAULT_SETTINGS, gemini_key="G")
        with self.assertRaises(ai_extract.AIError) as cm:
            scanner.read_bill([b"j"], s, "gemini", "G", "")
        self.assertIn("overloaded right now", str(cm.exception))


class DailyQuotaTests(OverloadTests):
    DAILY = {"error": {"code": 429, "message": "You exceeded your current quota. Quota exceeded for metric: "
                       "generativelanguage.googleapis.com/generate_content_free_tier_requests_per_day, limit: 20"}}

    def setUp(self):
        super().setUp()
        ai_extract._exhausted.clear()

    def tearDown(self):
        super().tearDown()
        ai_extract._exhausted.clear()

    def serve_quota(self, used_up):
        def post(url, **k):
            self.urls.append(url)
            if "anthropic" in url:
                return FakeResp(200, {"content": [{"type": "text", "text": '{"grand_total": 8}'}]})
            if any(f"/{m}:" in url for m in used_up):
                return FakeResp(429, self.DAILY)
            return FakeResp(200, self.OK)
        ai_extract.requests.post = post

    def test_daily_quota_moves_to_next_model_and_remembers(self):
        self.serve_quota(["gemini-3.8-flash"])
        out = ai_extract.extract([b"j"], "gemini", "KEY12345", "gemini-3.8-flash")
        self.assertEqual(out["_model"], "gemini-3.5-flash")
        self.assertEqual(out["_switched"], "quota")
        self.assertEqual(self.sleeps, [])                       # no pointless waiting
        self.urls.clear()
        ai_extract.extract([b"j"], "gemini", "KEY12345", "gemini-3.8-flash")
        self.assertEqual(len(self.urls), 1)                      # went straight to the model with allowance
        self.assertIn("gemini-3.5-flash:", self.urls[0])

    def test_all_models_used_up_falls_back_to_claude(self):
        import scanner
        self.serve_quota(["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.8-flash-lite"])
        s = dict(scanner.DEFAULT_SETTINGS, gemini_key="G", claude_key="C")
        bill = scanner.read_bill([b"j"], s, "gemini", "G", "")
        self.assertEqual(bill["grand_total"], 8)
        self.assertIn("out of free quota", bill["warnings"][0])

    def test_all_models_used_up_without_backup(self):
        import scanner
        self.serve_quota(["gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.8-flash-lite"])
        s = dict(scanner.DEFAULT_SETTINGS, gemini_key="G")
        with self.assertRaises(ai_extract.AIError) as cm:
            scanner.read_bill([b"j"], s, "gemini", "G", "")
        self.assertIn("every Gemini model", str(cm.exception))
        self.assertIn("gemini-3.8-flash-lite", str(cm.exception))


class MappingTests(unittest.TestCase):
    def test_intra_state_purchase(self):
        d = mapping.build_draft(bill(), book_from_fake())
        led = {e["ledger"]: (e["dr_cr"], e["amount"]) for e in d["entries"]}
        self.assertEqual(d["voucher_type"], "Purchase")
        self.assertEqual(d["party_ledger"], "Sharma Steel Pvt Ltd")        # matched by GSTIN
        self.assertEqual(led["Sharma Steel Pvt Ltd"], ("Cr", 11800))
        self.assertEqual(led["Purchase @ 18%"], ("Dr", 10000))
        self.assertEqual(led["Input CGST 9%"], ("Dr", 900))
        self.assertEqual(led["Input SGST 9%"], ("Dr", 900))
        self.assertEqual(balance(d["entries"]), 0)
        self.assertEqual(d["new_ledgers"], [])
        self.assertEqual(d["reference"], "SS/1042")
        self.assertEqual(d["date"], "2026-04-05")

    def test_multi_rate_with_round_off(self):
        b = bill(items=[
            {"description": "A", "taxable_value": 1000, "gst_rate": 18},
            {"description": "B", "taxable_value": 500, "gst_rate": 12}],
            tax_breakup=[{"gst_rate": 18, "taxable_value": 1000, "cgst": 90, "sgst": 90},
                         {"gst_rate": 12, "taxable_value": 500, "cgst": 30, "sgst": 30}],
            taxable_total=1500, cgst=120, sgst=120, round_off=0.0, grand_total=1740.0)
        b["grand_total"] = 1740.40            # printed total with an unprinted 0.40 round-off
        d = mapping.build_draft(b, book_from_fake())
        led = {e["ledger"]: (e["dr_cr"], e["amount"]) for e in d["entries"]}
        self.assertEqual(led["Purchase @ 12%"], ("Dr", 500))
        self.assertEqual(led["Input CGST 6%"], ("Dr", 30))
        self.assertEqual(led["Round Off"], ("Dr", 0.40))
        self.assertEqual(balance(d["entries"]), 0)

    def test_igst_and_new_party(self):
        b = bill(supplier_name="New Supplier LLP", supplier_gstin="27ABCDE1234F1Z9", supplier_state="Maharashtra",
                 cgst=0, sgst=0, igst=1800)
        d = mapping.build_draft(b, book_from_fake())
        led = {e["ledger"]: (e["dr_cr"], e["amount"]) for e in d["entries"]}
        self.assertEqual(led["Input IGST"], ("Dr", 1800))
        self.assertEqual(led["New Supplier LLP"], ("Cr", 11800))
        self.assertEqual(d["new_ledgers"][0]["name"], "New Supplier LLP")
        self.assertEqual(d["new_ledgers"][0]["parent"], "Sundry Creditors")
        self.assertEqual(d["new_ledgers"][0]["gstin"], "27ABCDE1234F1Z9")

    def test_sales_mirror_and_own_gstin(self):
        # AI said purchase, but the seller GSTIN is ours -> Sales
        b = bill(supplier_name="Demo Traders", supplier_gstin="24AAAAA0000A1Z5",
                 buyer_name="Sharma Steel Pvt Ltd", buyer_gstin="24AAACS1234F1Z5")
        d = mapping.build_draft(b, book_from_fake(), {"own_gstin": "24AAAAA0000A1Z5"})
        led = {e["ledger"]: (e["dr_cr"], e["amount"]) for e in d["entries"]}
        self.assertEqual(d["voucher_type"], "Sales")
        self.assertEqual(led["Sharma Steel Pvt Ltd"], ("Dr", 11800))
        self.assertEqual(led["Sales GST"], ("Cr", 10000))
        self.assertEqual(led["Output CGST"], ("Cr", 900))
        self.assertEqual(d["voucher_number"], "SS/1042")
        self.assertEqual(balance(d["entries"]), 0)

    def test_plain_cgst_sgst_ledgers_both_matched(self):
        # Seen on a real company: "Cgst" matched but SGST went to a new ledger instead of "Sgst"
        book = mapping.LedgerBook([
            {"name": "Sharma Steel Pvt Ltd", "parent": "Sundry Creditors", "gstin": "24AAACS1234F1Z5"},
            {"name": "GST PURCHASE", "parent": "Purchase Accounts", "gstin": ""},
            {"name": "Cgst", "parent": "Duties & Taxes", "gstin": ""},
            {"name": "Sgst", "parent": "Duties & Taxes", "gstin": ""},
            {"name": "Igst", "parent": "Duties & Taxes", "gstin": ""},
            {"name": "Round Off", "parent": "Indirect Expenses", "gstin": ""}])
        d = mapping.build_draft(bill(), book)
        names = [e["ledger"] for e in d["entries"]]
        self.assertEqual(names[:4], ["Sharma Steel Pvt Ltd", "GST PURCHASE", "Cgst", "Sgst"])
        self.assertEqual(d["new_ledgers"], [])
        self.assertIn("dt 05-04-2026", d["narration"])

    def test_tax_ledgers_named_by_tax_head(self):
        book = mapping.LedgerBook([
            {"name": "Purchase", "parent": "Purchase Accounts", "gstin": ""},
            {"name": "Central Tax (Input)", "parent": "Duties & Taxes", "gstin": ""},
            {"name": "State Tax (Input)", "parent": "Duties & Taxes", "gstin": ""},
            {"name": "Integrated Tax (Input)", "parent": "Duties & Taxes", "gstin": ""}])
        d = mapping.build_draft(bill(), book)
        names = {e["ledger"] for e in d["entries"]}
        self.assertTrue({"Central Tax (Input)", "State Tax (Input)"} <= names)
        d = mapping.build_draft(bill(cgst=0, sgst=0, igst=1800), book)
        self.assertIn("Integrated Tax (Input)", {e["ledger"] for e in d["entries"]})

    def test_supplier_credit_note_becomes_debit_note(self):
        b = bill(doc_type="credit_note", cgst=0, sgst=0, igst=1800,
                 supplier_name="Kaveri Electricals & Co.", supplier_gstin="27AAKFK5521M1Z3")
        d = mapping.build_draft(b, book_from_fake(), {"own_gstin": "24AAAAA0000A1Z5"})
        led = {e["ledger"]: e["dr_cr"] for e in d["entries"]}
        self.assertEqual(d["voucher_type"], "Debit Note")
        self.assertEqual(d["party_ledger"], "Kaveri Electricals & Co.")
        self.assertEqual(led["Kaveri Electricals & Co."], "Dr")      # supplier owes us
        self.assertEqual(led["Purchase @ 18%"], "Cr")
        self.assertEqual(led["Input IGST"], "Cr")
        self.assertEqual(balance(d["entries"]), 0)

    def test_our_debit_note_to_supplier(self):
        b = bill(doc_type="debit_note", supplier_name="Demo Traders", supplier_gstin="24AAAAA0000A1Z5",
                 buyer_name="Sharma Steel Pvt Ltd", buyer_gstin="24AAACS1234F1Z5")
        d = mapping.build_draft(b, book_from_fake(), {"own_gstin": "24AAAAA0000A1Z5"})
        self.assertEqual(d["voucher_type"], "Debit Note")
        self.assertEqual(d["party_ledger"], "Sharma Steel Pvt Ltd")
        self.assertEqual({e["ledger"]: e["dr_cr"] for e in d["entries"]}["Sharma Steel Pvt Ltd"], "Dr")

    def test_empty_book_proposes_ledgers(self):
        d = mapping.build_draft(bill(), mapping.LedgerBook([]))
        names = {n["name"] for n in d["new_ledgers"]}
        self.assertIn("Input CGST 9%", names)
        self.assertEqual(balance(d["entries"]), 0)


class XmlTests(unittest.TestCase):
    def test_voucher_xml_signs_and_optional(self):
        d = mapping.build_draft(bill(), book_from_fake())
        x = tally_push.voucher_xml(d, optional=True)
        v = ET.fromstring(x).find("VOUCHER")
        self.assertEqual(v.findtext("ISOPTIONAL"), "Yes")
        self.assertEqual(v.findtext("DATE"), "20260405")
        self.assertEqual(v.findtext("REFERENCE"), "SS/1042")
        rows = {e.findtext("LEDGERNAME"): (e.findtext("ISDEEMEDPOSITIVE"), e.findtext("AMOUNT"))
                for e in v.iter("ALLLEDGERENTRIES.LIST")}
        self.assertEqual(rows["Sharma Steel Pvt Ltd"], ("No", "11800.00"))
        self.assertEqual(rows["Purchase @ 18%"], ("Yes", "-10000.00"))
        bill_alloc = v.find("ALLLEDGERENTRIES.LIST/BILLALLOCATIONS.LIST")
        self.assertEqual(bill_alloc.findtext("NAME"), "SS/1042")
        self.assertEqual(bill_alloc.findtext("BILLTYPE"), "New Ref")

    def test_unbalanced_rejected(self):
        with self.assertRaises(tally_push.TallyError):
            tally_push.voucher_xml({"date": "2026-04-01", "entries": [
                {"ledger": "A", "dr_cr": "Dr", "amount": 10}, {"ledger": "B", "dr_cr": "Cr", "amount": 9}]})

    def test_escaping(self):
        x = tally_push.voucher_xml({"date": "2026-04-01", "party_ledger": "A & B <Co>", "entries": [
            {"ledger": "A & B <Co>", "dr_cr": "Cr", "amount": 10}, {"ledger": "Cash", "dr_cr": "Dr", "amount": 10}]})
        self.assertEqual(ET.fromstring(x).find("VOUCHER/PARTYLEDGERNAME").text, "A & B <Co>")

    def test_parse_response(self):
        r = tally_push.parse_import_response(
            b"<RESPONSE><CREATED>0</CREATED><ERRORS>1</ERRORS><LINEERROR>Ledger 'X' does not exist!</LINEERROR></RESPONSE>")
        self.assertEqual(r["errors"], 1)
        self.assertEqual(r["messages"], ["Ledger 'X' does not exist!"])

    def test_ledger_master_xml(self):
        x = tally_push.ledger_master_xml({"name": "Input CGST 9%", "parent": "Duties & Taxes", "tax_head": "cgst"})
        led = ET.fromstring(x).find("LEDGER")
        self.assertEqual(led.findtext("GSTDUTYHEAD"), "Central Tax")


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.state, cls.url = fake_tally.start()
        import scanner
        cls.scanner = scanner
        cls.client = scanner.app.test_client()
        cls.client.post("/api/settings", json={"tally_url": cls.url, "company": fake_tally.COMPANY,
                                               "gemini_key": "test-key", "provider": "gemini"})
        cls._orig = ai_extract.extract

    @classmethod
    def tearDownClass(cls):
        ai_extract.extract = cls._orig
        cls.srv.shutdown()
        shutil.rmtree(_tmp, ignore_errors=True)

    def setUp(self):
        if os.path.exists(self.scanner.HISTORY_FILE):
            os.remove(self.scanner.HISTORY_FILE)          # each test starts with no push history
        self.state.imports.clear()
        self.state.reject_vouchers = False
        self.scanner._ledger_cache.clear()

    def upload_and_scan(self, b):
        ai_extract.extract = lambda pages, *a, **k: b
        r = self.client.post("/api/upload", data={"files": (io.BytesIO(make_pdf(2)), "inv.pdf")},
                             content_type="multipart/form-data").get_json()
        doc_id = r["documents"][0]["id"]
        self.assertEqual(r["documents"][0]["pages"], 2)
        page = self.client.get(f"/api/docs/{doc_id}/page/1")
        self.assertEqual(page.mimetype, "image/jpeg")
        page.close()
        r = self.client.post(f"/api/docs/{doc_id}/scan").get_json()
        self.assertTrue(r["ok"], r)
        return doc_id, r["document"]

    def test_full_flow_optional_voucher_and_duplicate_guard(self):
        status = self.client.get("/api/tally/status").get_json()
        self.assertEqual(status["companies"], [fake_tally.COMPANY])
        doc_id, doc = self.upload_and_scan(bill())
        self.assertEqual(doc["draft"]["party_ledger"], "Sharma Steel Pvt Ltd")

        r = self.client.post("/api/push", json={"doc_ids": [doc_id]}).get_json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(self.state.imports), 1)
        v = ET.fromstring(self.state.imports[0]).find(".//VOUCHER")
        self.assertEqual(v.findtext("ISOPTIONAL"), "Yes")
        self.assertIn("<SVCURRENTCOMPANY>Demo Traders</SVCURRENTCOMPANY>", self.state.imports[0])
        self.assertEqual(self.client.get(f"/api/docs/{doc_id}").get_json()["document"]["status"], "pushed")

        # Same bill again -> blocked unless forced
        r = self.client.post("/api/push", json={"doc_ids": [doc_id]}).get_json()
        self.assertFalse(r["ok"])
        self.assertTrue(r["results"][0]["duplicate"])
        r = self.client.post("/api/push", json={"doc_ids": [doc_id], "force": True}).get_json()
        self.assertTrue(r["ok"])
        hist = self.client.get("/api/history").get_json()["history"]
        self.assertEqual(hist[0]["reference"], "SS/1042")

    def test_new_party_created_then_voucher(self):
        b = bill(supplier_name="Fresh Metals", supplier_gstin="24FRESH1234F1Z1", invoice_number="FM-9")
        doc_id, doc = self.upload_and_scan(b)
        self.assertEqual(doc["draft"]["new_ledgers"][0]["name"], "Fresh Metals")
        r = self.client.post("/api/push", json={"doc_ids": [doc_id], "optional": False,
                                                "create_missing_ledgers": True}).get_json()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["results"][0]["ledgers_created"], ["Fresh Metals"])
        self.assertEqual(len(self.state.imports), 2)                 # masters, then voucher
        self.assertIn("<LEDGER NAME=\"Fresh Metals\"", self.state.imports[0])
        self.assertIn("<ISOPTIONAL>No</ISOPTIONAL>", self.state.imports[1])

    def test_missing_ledger_without_permission(self):
        b = bill(supplier_name="Nobody Co", supplier_gstin="", invoice_number="N-1")
        doc_id, _ = self.upload_and_scan(b)
        r = self.client.post("/api/push", json={"doc_ids": [doc_id], "create_missing_ledgers": False}).get_json()
        self.assertFalse(r["ok"])
        self.assertIn("does not exist", r["results"][0]["error"])
        self.assertEqual(self.state.imports, [])

    def test_tally_rejection_reported(self):
        doc_id, _ = self.upload_and_scan(bill(invoice_number="REJ-1"))
        self.state.reject_vouchers = True
        r = self.client.post("/api/push", json={"doc_ids": [doc_id]}).get_json()
        self.assertFalse(r["ok"])
        self.assertIn("Voucher totals do not match", r["results"][0]["error"])

    def test_xml_download(self):
        doc_id, doc = self.upload_and_scan(bill(invoice_number="X-1"))
        resp = self.client.post("/api/xml", json={"drafts": [doc["draft"]]})
        root = ET.fromstring(resp.data)
        self.assertEqual(root.findtext(".//REPORTNAME"), "Vouchers")
        self.assertIsNotNone(root.find(".//VOUCHER"))

    def test_create_masters_from_app(self):
        r = self.client.post("/api/masters", json={"items": [
            {"kind": "group", "data": {"name": "Creditors - Yarn", "parent": "Sundry Creditors"}},
            {"kind": "unit", "data": {"name": "Mtr"}},
            {"kind": "stock_item", "data": {"name": "Cotton Yarn 30s", "unit": "Kg", "hsn": "5205", "gst_rate": 5}},
        ]}).get_json()
        self.assertTrue(r["ok"], r)
        r = self.client.post("/api/masters", json={"kind": "ledger", "data": {
            "name": "Surat Yarn Mills", "parent": "Creditors - Yarn", "gstin": "24AAPFS1234K1Z9",
            "state": "Gujarat", "address": "Ring Road\nSurat", "pan": "AAPFS1234K"}}).get_json()
        self.assertTrue(r["ok"], r)
        xml = self.state.imports[-1]
        self.assertIn("<ISBILLWISEON>Yes</ISBILLWISEON>", xml)          # sub-group of Sundry Creditors
        self.assertIn("<PARTYGSTIN>24AAPFS1234K1Z9</PARTYGSTIN>", xml)
        led = self.client.get("/api/tally/ledgers").get_json()
        self.assertIn("Surat Yarn Mills", [l["name"] for l in led["ledgers"]])
        self.assertIn("Creditors - Yarn", led["groups"])
        m = self.client.get("/api/tally/masters").get_json()
        self.assertIn("Cotton Yarn 30s", [i["name"] for i in m["stock_items"]])
        self.assertIn("Mtr", [u["name"] for u in m["units"]])
        # Creating an existing ledger is a no-op, not an error
        r = self.client.post("/api/masters", json={"kind": "ledger", "data": {"name": "surat yarn mills"}}).get_json()
        self.assertTrue(r["results"][0].get("existed"))

    def test_excel_bill_register_import_and_push(self):
        import excel_import
        data = excel_import.template_xlsx("bills")
        r = self.client.post("/api/import/excel", data={"files": (io.BytesIO(data), "bills.xlsx")},
                             content_type="multipart/form-data").get_json()
        self.assertTrue(r["ok"], r)
        docs = r["documents"]
        self.assertEqual(len(docs), 3)                       # Kaveri's two rows are one bill
        kav = next(d for d in docs if "Kaveri" in d["name"])
        self.assertEqual(kav["source"], "excel")
        self.assertEqual(len(kav["rows"]), 2)
        self.assertEqual(kav["bill"]["grand_total"], 75241)
        self.assertEqual(balance(kav["draft"]["entries"]), 0)
        sharma = next(d for d in docs if "Sharma" in d["name"])
        self.assertEqual(sharma["draft"]["party_ledger"], "Sharma Steel Pvt Ltd")
        self.assertEqual(balance(sharma["draft"]["entries"]), 0)
        r = self.client.post("/api/push", json={"doc_ids": [sharma["id"]], "force": True}).get_json()
        self.assertTrue(r["ok"], r)

    def test_excel_ledger_lines_import(self):
        import excel_import
        data = excel_import.template_xlsx("ledger")
        r = self.client.post("/api/import/excel", data={"files": (io.BytesIO(data), "jv.xlsx")},
                             content_type="multipart/form-data").get_json()
        self.assertTrue(r["ok"], r)
        self.assertEqual([d["draft"]["voucher_type"] for d in r["documents"]], ["Payment", "Journal", "Receipt"])
        pay = r["documents"][0]["draft"]
        self.assertEqual(pay["party_ledger"], "Sharma Steel Pvt Ltd")
        self.assertEqual(pay["entries"][0]["bill_type"], "Agst Ref")
        self.assertEqual(pay["date"], "2026-05-10")

    def test_csv_with_own_headings(self):
        csv_data = ("Invoice Date,Supplier,GSTIN,Invoice No,Taxable Value,GST Rate,Invoice Value\n"
                    "01/06/2026,Sharma Steel Pvt Ltd,24AAACS1234F1Z5,SS/2001,1000,18%,1180\n").encode()
        r = self.client.post("/api/import/excel", data={"files": (io.BytesIO(csv_data), "my.csv")},
                             content_type="multipart/form-data").get_json()
        self.assertTrue(r["ok"], r)
        d = r["documents"][0]["draft"]
        self.assertEqual(d["date"], "2026-06-01")
        led = {e["ledger"]: e["amount"] for e in d["entries"]}
        self.assertEqual(led["Input CGST 9%"], 90)

    def test_bad_excel_reports_reason(self):
        r = self.client.post("/api/import/excel", data={"files": (io.BytesIO(b"a,b\n1,2\n"), "x.csv")},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("template", r.get_json()["error"].lower())

    def test_load_samples(self):
        r = self.client.post("/api/samples/load").get_json()
        self.assertTrue(r["ok"], r)
        imgs = [d for d in r["documents"] if d["source"] == "scan"]
        xls = [d for d in r["documents"] if d["source"] == "excel"]
        self.assertEqual(len(imgs), 9)                          # 9 bill images / PDFs
        self.assertGreaterEqual(len(xls), 9)                    # bills + ledger-line vouchers from both sheets
        self.assertEqual(r["skipped"], [])
        for d in xls:
            self.assertEqual(balance(d["draft"]["entries"]), 0, d["name"])

    def test_settings_mask_keys(self):
        s = self.client.get("/api/settings").get_json()
        self.assertNotIn("test-key", json.dumps(s))
        self.assertTrue(s["gemini_key_set"])
        # Sending the masked value back must not overwrite the real key
        self.client.post("/api/settings", json={"gemini_key": s["gemini_key"]})
        self.assertEqual(self.scanner.load_settings()["gemini_key"], "test-key")


if __name__ == "__main__":
    unittest.main()
