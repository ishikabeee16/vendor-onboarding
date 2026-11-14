# Test cases

All data is fake. Every document is stamped "SAMPLE - FOR TESTING ONLY".
To rebuild everything: `python tools/make_test_data.py` (from the `vendor-onboarding` folder).

| File | Vendor | What's special | Expected result |
|---|---|---|---|
| happy_path | Sharma Packaging Pvt Ltd (KA) | Clean. Bank says "PRIVATE LIMITED", form says "Pvt Ltd" | Approved |
| E1_bank_name_mismatch | Green Leaf Foods Pvt Ltd (TN) | Account holder is "ARJUN MEHTA", a person (+ Gmail: info only) | Pending – internal review (C9) |
| E2_hidden_inconsistency | Bright Steel Industries LLP (KA) | Valid GSTIN starting 27 (Maharashtra); PAN field 1 char off | Pending – vendor (C7, C8, C12) |
| E3_foreign_vendor | Northwind Supplies Inc (US) | EIN, ABA routing + SWIFT, US documents | Approved under US rules |
| E4_resubmission_v1 | Kiran Textiles (proprietor, GJ) | PAN card uploaded in the cheque slot | Pending – vendor (C11) |
| E4_resubmission_v2 | Kiran Textiles (proprietor, GJ) | Correct cheque; version 2 of v1 | Approved |
| backup_duplicate_bank | Nova Traders (MH) | Bank account belongs to existing vendor VND-0007 | Rejected (C13) |
| backup_blurry_doc | Sunrise Electricals Pvt Ltd (TS) | Cheque is a blurry, rotated photo | Pending – vendor, re-upload (C11) |

## What one submission looks like (JSON)

- `company`: legal name, trade name, business type, country, address
- `contact`: name, email, phone
- `tax`: India → `pan`, `gstin`. US → `tax_id_type`, `tax_id`
- `bank`: India → `ifsc`. US → `routing_number` + `swift`
- `documents`: a list of `{slot, file}`. The *slot* is what the vendor says the file is.
  The AI later checks what the file really is (check C11).
- `expected`: what the process should decide, and why. This is our answer key.
  The app ignores it; we use it to test that the checks behave correctly.

## Details worth knowing for the interview

- **PAN structure.** The 4th letter shows the holder type: C = company, F = firm/LLP,
  P = person. The 5th letter is the first letter of the name. All test PANs follow this.
- **GSTIN** = state code (2) + PAN (10) + entity number (1) + "Z" + check digit (1).
  Every test GSTIN has a correct check digit, so the format check passes and only
  the cross-checks can catch E2.
- **Proprietors.** For a sole proprietor the *legal* name is the person (Kiran Desai), and the
  business name is the *trade* name (Kiran Textiles). The bank account is in the trade name,
  so the name check compares against legal name **or** trade name.
- **US banks don't use IBAN.** They use a 9-digit ABA routing number (which has its own
  check digit) plus SWIFT for international transfers.
