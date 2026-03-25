"""
Upload exported resume data + files to the production server.

Usage:
    python upload_to_server.py

Reads resume_export.json and uploads each resume (metadata + PDF file)
to the server's /import-with-file endpoint.
If the PDF file doesn't exist locally, falls back to metadata-only import.
"""

import json
import os
import sys
import time
import requests

# ── Configuration ────────────────────────────────────────────────────────────
SERVER_URL = "https://myepfo.com/api/resume-database"
EXPORT_FILE = os.path.join(os.path.dirname(__file__), "resume_export.json")
# ─────────────────────────────────────────────────────────────────────────────


def upload():
    if not os.path.exists(EXPORT_FILE):
        print(f"ERROR: {EXPORT_FILE} not found. Run 'python export_resumes.py' first.")
        sys.exit(1)

    with open(EXPORT_FILE, "r", encoding="utf-8") as f:
        records = json.load(f)

    print(f"Loaded {len(records)} records from {EXPORT_FILE}")

    imported = 0
    skipped = 0
    errors = 0
    no_file = 0

    for i, record in enumerate(records):
        email = record.get("candidate_email", "unknown")
        file_path = record.get("resume_file_path")

        # Progress
        if (i + 1) % 50 == 0 or i == 0:
            print(f"\n[{i+1}/{len(records)}] Processing... (imported={imported}, skipped={skipped}, errors={errors})")

        try:
            if file_path and os.path.exists(file_path):
                # Upload with file
                filename = os.path.basename(file_path)
                with open(file_path, "rb") as f:
                    resp = requests.post(
                        f"{SERVER_URL}/import-with-file",
                        data={"metadata": json.dumps(record)},
                        files={"file": (filename, f, "application/pdf")},
                        timeout=60,
                    )
                resp.raise_for_status()
                result = resp.json()
                if result.get("status") == "imported":
                    imported += 1
                else:
                    skipped += 1
            else:
                # No file available — skip or do metadata-only
                no_file += 1
                # Fallback: use the /import endpoint for metadata only
                resp = requests.post(
                    f"{SERVER_URL}/import",
                    json=[record],
                    headers={"Content-Type": "application/json"},
                    timeout=30,
                )
                resp.raise_for_status()
                result = resp.json()
                imported += result.get("imported", 0)
                skipped += result.get("skipped", 0)

        except requests.exceptions.RequestException as e:
            print(f"  FAILED ({email}): {e}")
            errors += 1

        # Small delay to not overwhelm the server
        if (i + 1) % 10 == 0:
            time.sleep(0.1)

    print(f"\n{'='*50}")
    print(f"DONE!")
    print(f"  Imported:    {imported}")
    print(f"  Skipped:     {skipped}")
    print(f"  Errors:      {errors}")
    print(f"  No file:     {no_file} (metadata only)")


if __name__ == "__main__":
    upload()
