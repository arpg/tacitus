### Travel reimbursement

**TOKEN EFFICIENCY RULE**: NEVER search the full inbox for receipts. The
obligations ledger has pre-compiled receipt lists. Read from state first;
only run TARGETED searches (by message_id or exact subject) when a specific
item is flagged as pending in the ledger.

#### Step 1 — Collect receipts (preprocessor has done most of this)
1. Read the trip entry: `python3 /home/ea-bot/ea-system/preprocessor/ledger.py search --query "<trip name>"`.
   Check `receipts_found` and `receipts_pending`. Do NOT re-search the full inbox.
2. For any items still in `receipts_pending`, run ONE targeted search:
   - Outlook: `python3 /home/ea-bot/ea-system/preprocessor/graph_actions.py search-mail --query "subject:hotel confirmation" --folder inbox`
   - Gmail MCP: `subject:(Trip.com OR hotel OR confirmation) after:YYYY-MM-DD`
3. For email-format receipts (HTML body, no PDF attachment): convert to PDF with
   `python3 /home/ea-bot/ea-system/preprocessor/email_to_pdf.py --message-id <id> --output ~/.ea/receipts/<trip>/<name>.pdf`.
   A receipt in a Gmail account adds `--gmail <account>` and takes the Gmail
   message id (`gmail_client.py search`). The PDF is the original email;
   never retype a receipt, and never ask the user to forward it.
4. Save all receipt files under `~/.ea/receipts/<trip_name>/`

#### Step 2 — Confirm with user via chat (ask-first)
Post to the user's EA chat channel (from `user-profile.json`):
```
🧾 Reimbursement ready for [Trip Name] (DA: XXXXXXXXX)

Receipts collected:
  • Lodging: hotel-receipt.pdf, hotel-itemized.pdf
  • Conference registration: registration1.pdf, registration2.pdf
  • Rental car: rental-agreement.pdf
  • Rental car gas: car-gas.png

Missing / uncertain:
  • [anything still in receipts_pending]

Reply **submit** to file it on the department's travel form, **skip** to
close, or attach any missing receipts here.
```

#### Step 3 — Build trip JSON and submit
On "submit", create `~/.ea/receipts/<trip_name>/trip.json`:
```json
{
  "da_number": "...",
  "travel_card_used": false,
  "has_airfare": true,
  "airfare_file": "~/.ea/receipts/<trip>/airfare.pdf",
  "reimbursement_details": "Free-text summary for finance team",
  "meal_option": "full_per_diem",
  "meals_provided": true,
  "meals_provided_description": "Lunch Mon-Wed and Breakfast Mon-Wed provided by program/hotel",
  "expense_categories": {
    "lodging": ["path/to/hotel.pdf"],
    "conference_registration": ["path/to/reg.pdf"],
    "rental_car": ["path/to/ra.pdf"],
    "rental_car_gas": ["path/to/gas.png"]
  }
}
```
`travel_card_used` answers the payment attestations a form asks (who paid
the vendor), so set it from the facts of the trip.

The form itself belongs to the user's department, so the core has no
submitter for it. An institution plugin ships one: when its skill is
loaded in this conversation, it names the submit command and the form's
own fields (for example, an institution plugin's `reimburse_submit.py`).
Run its dry run first when it has one, and review the screenshot before
the real submission. Submitter defaults are in
`~/.ea/config/reimbursement-config.json`.

Without a plugin adapter, the packet is the deliverable: post the receipts
folder, the trip.json summary and the `form_url` from
`reimbursement-config.json`, and tell the user the packet is ready to file
by hand. Never say a reimbursement was submitted unless a submit command
reported success.

If any form or portal presents a CAPTCHA, a 2captcha.com key is available:
`TWOCAPTCHA_API_KEY` in `~/.ea/.env` and `two_captcha_api_key` in
`reimbursement-config.json`. Nothing consumes it automatically; use it when
a workflow needs it.

Post result to chat: "✅ Reimbursement submitted for [trip]. Confirmation screenshot saved."

### Receipt collection via chat
When the user posts a file/image in the user's EA chat channel (from `user-profile.json`) with context like
"receipt for the Portland trip":
- Save the file to `~/.ea/receipts/<trip_name>/`
- Update the obligations ledger entry
- Confirm: "Added to the Portland workshop reimbursement. Now have 5/est.7 receipts."
- Do NOT re-scan the inbox. The ledger is the source of truth.
