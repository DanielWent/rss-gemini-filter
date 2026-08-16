import os
import json
import datetime
from datetime import timezone
from typing import List, Dict, Any, Literal
import requests
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from feedgen.feed import FeedGenerator

# Google Drive Target Folders
ROOT_FOLDER_IDS = [
    "1bxY6FSjJMrfPEGxAiq6fRhC3DGun9Ni5",
    "1Wycq7k8Wsh4bzZLociKkc_tMJmiIGsEX",
]

ARCHIVE_FILE = "bwcc_archive.json"
FEED_FILE = "bwcc_feed.xml"
FEED_TITLE = "Bearsden West Community Council"
FEED_LINK = "https://drive.google.com/drive/folders/1bxY6FSjJMrfPEGxAiq6fRhC3DGun9Ni5"
FEED_DESCRIPTION = "Automated AI summaries of Bearsden West Community Council minutes, budgets, and public documents."

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
DRIVE_API_KEY = os.environ.get("DRIVE_API_KEY") or GEMINI_API_KEY


class DocumentSummary(BaseModel):
    doc_type: Literal["minutes", "budget", "other"] = Field(
        description="The classification of the document."
    )
    title: str = Field(
        description="Strictly formatted title adhering to naming rules."
    )
    summary_html: str = Field(
        description="Comprehensive summary formatted in clean HTML (using <h3>, <p>, <ul>, <li>, <strong>)."
    )


def load_archive() -> Dict[str, Any]:
    if os.path.exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_archive(archive: Dict[str, Any]) -> None:
    with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(archive, f, indent=2, ensure_ascii=False)


def list_drive_folder(folder_id: str, api_key: str) -> List[Dict[str, Any]]:
    """List all immediate items within a Google Drive folder."""
    items = []
    page_token = None
    base_url = "https://www.googleapis.com/drive/v3/files"

    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)",
            "pageSize": 100,
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(base_url, params=params)
        resp.raise_for_status()
        data = resp.json()

        items.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return items


def scan_drive_folders_recursively(folder_ids: List[str], api_key: str) -> List[Dict[str, Any]]:
    """Recursively search for all PDF files in given Google Drive folders and subfolders."""
    discovered_pdfs = []
    folders_to_scan = list(folder_ids)
    scanned_folders = set()

    while folders_to_scan:
        current_folder = folders_to_scan.pop(0)
        if current_folder in scanned_folders:
            continue
        scanned_folders.add(current_folder)

        try:
            children = list_drive_folder(current_folder, api_key)
            for child in children:
                mime_type = child.get("mimeType", "")
                name = child.get("name", "")

                if mime_type == "application/vnd.google-apps.folder":
                    folders_to_scan.append(child["id"])
                elif mime_type == "application/pdf" or name.lower().endswith(".pdf"):
                    discovered_pdfs.append(child)
        except Exception as e:
            print(f"Error scanning folder {current_folder}: {e}")

    return discovered_pdfs


def download_drive_file(file_id: str, api_key: str) -> bytes:
    """Download binary content of a Google Drive file."""
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media&key={api_key}"
    resp = requests.get(url)
    if resp.status_code == 200:
        return resp.content

    fallback_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    resp_fallback = requests.get(fallback_url)
    resp_fallback.raise_for_status()
    return resp_fallback.content


def summarize_pdf_with_gemini(pdf_bytes: bytes, filename: str) -> DocumentSummary:
    """Analyze PDF content using Google GenAI SDK with structured Pydantic schema."""
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are analyzing a public document from the Bearsden West Community Council ("{filename}").

STRICT TITLE CONVENTIONS:
1. Meeting Minutes: MUST be formatted exactly as: "Bearsden West CC Minutes - <Month YYYY>"
   (e.g., "Bearsden West CC Minutes - June 2025")
2. Budget / Accounts / Financial statements: MUST be formatted exactly as: "Bearsden West CC Budget - <YYYY/YY>"
   (e.g., "Bearsden West CC Budget - 2025/26")
3. Other documents: MUST be formatted as: "Bearsden West CC - <Document Topic> - <Month YYYY or Date>"

CONTENT SUMMARY REQUIREMENTS:
- Provide a detailed, comprehensive, and well-structured HTML summary using <h3>, <p>, <ul>, <li>, and <strong> tags.
- Detail key discussions, decisions, planning applications, local council updates, police reports, and financial expenditures.
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_bytes(
                data=pdf_bytes,
                mime_type="application/pdf",
            ),
            prompt,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=DocumentSummary,
        ),
    )

    return response.parsed


def update_rss_feed(archive: Dict[str, Any]) -> None:
    """Generate or overwrite bwcc_feed.xml from all archived items."""
    fg = FeedGenerator()
    fg.title(FEED_TITLE)
    fg.link(href=FEED_LINK, rel="alternate")
    fg.description(FEED_DESCRIPTION)
    fg.language("en-gb")

    sorted_items = sorted(
        archive.values(),
        key=lambda x: x.get("processed_at", ""),
        reverse=True,
    )

    for item in sorted_items:
        fe = fg.add_entry()
        fe.id(item.get("webViewLink") or f"bwcc-{item['id']}")
        fe.title(item["title"])
        fe.link(href=item.get("webViewLink") or FEED_LINK)
        fe.description(item["summary_html"])

        if item.get("processed_at"):
            pub_date = datetime.datetime.fromisoformat(item["processed_at"])
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
            fe.pubDate(pub_date)

    fg.rss_file(FEED_FILE, pretty=True)


def main():
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY is not configured.")

    archive = load_archive()
    print(f"Archive loaded: {len(archive)} items previously processed.")

    print("Querying Google Drive folders recursively...")
    all_pdfs = scan_drive_folders_recursively(ROOT_FOLDER_IDS, DRIVE_API_KEY)
    print(f"Found {len(all_pdfs)} total PDFs across target directories.")

    new_count = 0

    for pdf in all_pdfs:
        file_id = pdf["id"]
        filename = pdf.get("name", "Document.pdf")

        if file_id in archive:
            continue

        print(f"Processing new file: {filename} ({file_id})")
        try:
            pdf_bytes = download_drive_file(file_id, DRIVE_API_KEY)
            parsed_summary: DocumentSummary = summarize_pdf_with_gemini(pdf_bytes, filename)

            archive[file_id] = {
                "id": file_id,
                "name": filename,
                "title": parsed_summary.title,
                "doc_type": parsed_summary.doc_type,
                "summary_html": parsed_summary.summary_html,
                "webViewLink": pdf.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view"),
                "modifiedTime": pdf.get("modifiedTime"),
                "processed_at": datetime.datetime.now(timezone.utc).isoformat(),
            }
            new_count += 1
            print(f"Added: {parsed_summary.title}")
        except Exception as e:
            print(f"Failed to process {filename}: {e}")

    # Always ensure the archive and feed files exist on disk for Git tracking
    if new_count > 0 or not os.path.exists(ARCHIVE_FILE):
        save_archive(archive)

    if new_count > 0 or not os.path.exists(FEED_FILE):
        update_rss_feed(archive)

    if new_count > 0:
        print(f"Successfully processed {new_count} new document(s) and refreshed {FEED_FILE}.")
    else:
        print("No new documents detected. State and feed files are up to date.")


if __name__ == "__main__":
    main()
