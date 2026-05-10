"""
One-shot patch: read Job_Id from col A, write Indeed URL to col I.
Run once to fix existing rows scraped before map_to_row was corrected.
"""
import sys
import time
from pull_dataset import get_sheet_id_from_url, get_google_service, TAB_NAME

SHEET_URL = sys.argv[1] if len(sys.argv) > 1 else ""


def main():
    if not SHEET_URL:
        print("Usage: python3 patch_indeed_urls.py <sheet_url>")
        sys.exit(1)

    service = get_google_service()
    sheet_id = get_sheet_id_from_url(SHEET_URL)

    print("Reading Job_Ids from col A...")
    resp = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{TAB_NAME}'!A2:A100000",
    ).execute()
    rows = resp.get("values", [])
    print(f"  {len(rows)} rows found.")

    # Build updates: col I = index 8 (0-based) = column I
    updates = []
    for i, r in enumerate(rows):
        job_id = r[0].strip() if r and r[0] else ""
        sheet_row = i + 2  # 1-based, skip header
        url = f"https://indeed.com/viewjob?jk={job_id}" if job_id else ""
        updates.append({
            "range": f"'{TAB_NAME}'!I{sheet_row}",
            "values": [[url]],
        })

    if not updates:
        print("Nothing to patch.")
        return

    print(f"Patching {len(updates)} rows (batches of 500)...")
    BATCH = 500
    for i in range(0, len(updates), BATCH):
        chunk = updates[i:i + BATCH]
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": chunk},
        ).execute()
        print(f"  Patched rows {i+2}–{i+len(chunk)+1}")
        time.sleep(1.2)

    print("Done — col I now contains Indeed job URLs.")


if __name__ == "__main__":
    main()
