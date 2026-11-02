# AI Bill Scanner → Tally push-back

Reads purchase / sales bills with AI and pushes them **back into TallyPrime** as ready-made vouchers.
By default each voucher goes in as an **Optional voucher**: it sits in Tally as a preview that does not
touch the books until the customer opens it and accepts it.

```
 Bills (JPG / PNG / PDF / ZIP)
        │  PDF pages → JPG, ZIP unpacked             file_prep.py
        ▼
 AI reads each bill (Gemini free tier or Claude)     ai_extract.py
        │  supplier, GSTIN, bill no., date, items, CGST/SGST/IGST, total
        ▼
 Voucher draft using the company's own ledgers      mapping.py  (+ ledgers read from Tally)
        │  party by GSTIN/name, rate-wise purchase & tax ledgers, round-off
        ▼
 Customer checks / edits next to the bill image     index.html
        ▼
 Import into TallyPrime (XML, port 9000)             tally_push.py
        └─ as an Optional voucher → customer accepts it in Tally (Ctrl+L)
```

## Quick start (Windows)

1. Install Python 3.10+ (tick "Add to PATH").
2. In TallyPrime: **F1 → Settings → Connectivity → TallyPrime acts as: Server**, port **9000**, and open the company.
3. Double-click **`Start AI Scanner.bat`**. The browser opens at http://localhost:5090.
4. **Settings** → choose the AI and paste the key:
   * **Google Gemini – free**: get a key at https://aistudio.google.com/apikey (no card needed). The free tier has
     per-minute and per-day limits, so the app scans bills one after another.
   * **Anthropic Claude – paid**: key at https://console.anthropic.com/settings/keys.
5. Pick the company at the top, then **Add bills** (or drag them in). Scanning starts automatically.
6. Check each voucher (warnings are shown in yellow), then **Push to Tally**.

Without Windows: `pip install -r requirements.txt` then `python scanner.py`.

## Accepting the entries in TallyPrime

With **Push as Optional** on (default):

1. In TallyPrime open **Day Book**, or **Display More Reports → Exception Reports → Optional Vouchers**.
2. Open the voucher, check it against the bill.
3. Press **Ctrl+L** (Regular) and save – it now posts to the books. Or delete it if it is wrong.

Turn **Push as Optional** off to post regular vouchers directly.

**Download XML** saves the same entries as a file: in Tally use **Import → Transactions** to load it manually.

## What the voucher looks like

A purchase bill of ₹10,000 + 9% CGST + 9% SGST with a 40-paise round-off becomes:

| Ledger | Dr | Cr |
|---|---:|---:|
| Sharma Steel Pvt Ltd *(bill-wise: New Ref SS/1042)* | | 11,800.40 |
| Purchase @ 18% | 10,000.00 | |
| Input CGST 9% | 900.00 | |
| Input SGST 9% | 900.00 | |
| Round Off | 0.40 | |

* **Party** is matched by GSTIN first, then by name (ignoring "Pvt Ltd", "M/s" etc.). If not found, a new ledger is
  proposed under Sundry Creditors / Debtors with the GSTIN and state, and created on push (you can untick
  **Create missing ledgers** or change the group).
* **Purchase / Sales and GST ledgers** are picked from the company's ledgers – rate-wise ones (e.g. *Input CGST 9%*,
  *Purchase @ 12%*) when they exist, input ledgers for purchases, output for sales. Bills with several GST rates get
  one line per rate. Defaults can be set in Settings.
* **Sales vs purchase**: set your company GSTIN in Settings – if the bill's seller GSTIN is yours it becomes a Sales voucher.
* The supplier's bill number goes into **Reference** and the bill-wise allocation; the voucher number is left to Tally.
* The draft always balances; small gaps (≤ ₹1) go to Round Off, larger ones are flagged.
* A bill already pushed (same company, voucher type, party and bill number) is blocked unless you confirm.

Vouchers are posted in **accounting mode** (ledger lines, no stock items). The item list read from the bill is shown
in the app for reference.

## Files

| File | What it does |
|---|---|
| `scanner.py` | Flask app on 127.0.0.1:5090 – upload, scan, drafts, push, history |
| `file_prep.py` | Images, PDFs (each page rendered to JPG with PyMuPDF) and ZIPs → pages |
| `ai_extract.py` | Gemini / Claude vision call (plain HTTPS), JSON parsing, number/date/GSTIN checks |
| `mapping.py` | Bill → balanced voucher using the company's ledgers |
| `tally_push.py` | Tally XML: read companies, ledgers, groups, voucher types; create ledgers; import vouchers |
| `index.html` | The whole UI |
| `tests/` | Unit + end-to-end tests against a fake Tally server (`python -m unittest discover -s tests -v`) |
| `build.bat` | Builds `dist\AIBillScanner.exe` with PyInstaller |

Data (uploaded pages, drafts, settings incl. API keys, push history) is kept in `data/` next to the scripts, or in
`%LOCALAPPDATA%\AIBillScanner` for the EXE. Bill images are sent only to the AI provider chosen in Settings.

## Notes

* Requests to Tally go one at a time (TallyPrime's server is single-threaded), and ledger reads use no date filters
  (those are known to freeze Tally on large companies).
* AI can misread – that is why every bill is reviewed before it is pushed, and why Optional vouchers are the default.
