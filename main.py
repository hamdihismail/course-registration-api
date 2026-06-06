from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup
import re

app = FastAPI(
        title="Course Registration API",
        description="Phase 1 API for catalog ingestion and course lookup",
        version="1.0.0"
)

# In-memory storage for Phase 1
catalog = {}

def normalize_course_code(course_code: str) -> str:
    """
    Normalizes course codes so COSC 3506, cosc3506, and COSC3506
    can be treated consistently.
    """
    return re.sub(r"\s+", "", course_code.strip().upper())

def extract_course_codes(text: str) -> list[str]:
    """
    Extracts course codes from messy prerequisite or cross-listed text.

    Examples:
    - "COSC 2006"
    - "Requires COSC 1046 and either COSC 1047 or ITEC 1047"
    - "COSC 3506, ITEC 3506"
    """
    if not text:
        return []

    if text.strip().lower() in ["none", "n/a", ""]:
        return []

    pattern = r"\b[A-Z]{3,4}\s*\d{4}\b"
    matches = re.findall(pattern, text.upper())

    return [normalize_course_code(match) for match in matches]


def parse_catalog_html(html_content: str) -> dict:
    """
    Parses the uploaded catalog HTML and returns a dictionary
    of normalized course codes mapped to course data.
    """
    soup = BeautifulSoup(html_content, "html.parser")

    table = soup.find("table")
    if table is None:
        raise ValueError("No table found in uploaded HTML file.")

    rows = table.find_all("tr")

    if len(rows) < 2:
        raise ValueError("Catalog table does not contain course rows.")

    parsed_courses = {}

    # Skip header row
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])

        # Expected columns:
        # Course Code, Title, Credits, Prerequisites, Cross-listed
        if len(cells) < 5:
            continue

        course_code_raw = cells[0].get_text(strip=True)
        title = cells[1].get_text(strip=True)
        credits_raw = cells[2].get_text(strip=True)
        prerequisites_raw = cells[3].get_text(" ", strip=True)
        cross_listed_raw = cells[4].get_text(" ", strip=True)

        if not course_code_raw:
            continue

        normalized_code = normalize_course_code(course_code_raw)

        try:
            credits = int(credits_raw)
        except ValueError:
            try:
                credits = float(credits_raw)
            except ValueError:
                credits = credits_raw

        parsed_courses[normalized_code] = {
            "course_code": normalized_code,
            "title": title,
            "credits": credits,
            "prerequisites": extract_course_codes(prerequisites_raw),
            "cross_listed": extract_course_codes(cross_listed_raw)
        }

    if not parsed_courses:
        raise ValueError("No valid courses could be parsed from the uploaded file.")

    return parsed_courses

@app.get("/")
def root():
    return {
        "message": "Course Registration API is running",
        "docs": "/docs"
    }

@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    """
    Uploads and imports an HTML course catalog.
    """
    if not file.filename.lower().endswith((".html", ".htm")):
        raise HTTPException(
            status_code=400,
            detail="Only HTML files are accepted."
        )

    try:
        contents = await file.read()
        html_content = contents.decode("utf-8")

        parsed_courses = parse_catalog_html(html_content)

        # Replace current in-memory catalog with imported data
        catalog.clear()
        catalog.update(parsed_courses)

        return {
            "message": "Catalog imported successfully.",
            "courses_imported": len(catalog)
        }

    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file must be valid UTF-8 HTML."
        )

    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
        )

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error while importing catalog: {str(error)}"
        )
    
@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str):
    """
    Returns a single course by course code.
    """
    normalized_code = normalize_course_code(course_code)

    course = catalog.get(normalized_code)

    if course is None:
        raise HTTPException(
            status_code=404,
            detail="Course not found."
        )

    return course