from fastapi import FastAPI, UploadFile, File, HTTPException, status
from fastapi.responses import JSONResponse
from bs4 import BeautifulSoup
import re
from typing import List, Dict, Any, Optional
import asyncio
from pydantic import BaseModel, Field

app = FastAPI(
        title="Course Registration API",
        description="Phase 2 API",
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

class HistoryCourse(BaseModel):
    course_code: str
    term: str
    credits_earned: int
    status: str


class HistoryUpdateRequest(BaseModel):
    history: List[HistoryCourse]


class PlannedCourse(BaseModel):
    course_code: str
    term: str


class PlanRequest(BaseModel):
    planned_courses: List[PlannedCourse]


class StudentRecord(BaseModel):
    history: List[HistoryCourse] = Field(default_factory=list)
    plan: List[PlannedCourse] = Field(default_factory=list)

app.state.students = {}
app.state.students_lock = asyncio.Lock()

VALID_HISTORY_STATUSES = {"Completed", "In-Progress", "Attempted"}


def clean_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    return " ".join(value.replace("\xa0", " ").split()).strip()


def parse_credits(value: str) -> int:
    """
    Convert transcript credits to int.
    Requirement: blank/non-numeric credits should become 0.
    """
    value = clean_text(value)
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def grade_rank(grade: str) -> int:
    """
    Higher is better.
    Numeric grade beats letter grade beats P/blank.
    """
    grade = clean_text(grade)

    if not grade:
        return 0

    if grade.upper() == "P":
        return 1

    try:
        float(grade)
        return 3
    except ValueError:
        pass

    # Letter grades such as A+, A, B, C, F, etc.
    if re.fullmatch(r"[A-F][+-]?", grade.upper()):
        return 2

    # Any other non-empty grade is still more informative than blank/P.
    return 1


def get_cell_texts(row) -> List[str]:
    cells = row.find_all(["td", "th"])
    return [clean_text(cell.get_text(" ", strip=True)) for cell in cells]


def header_index_map(headers: List[str]) -> Optional[Dict[str, int]]:
    """
    Finds tables whose header includes Status, Course, Grade, Term, Credits.
    The transcript may include a title column between Course and Grade.
    """
    normalized = [h.lower() for h in headers]

    required = ["status", "course", "grade", "term", "credits"]
    if not all(req in normalized for req in required):
        return None

    return {
        "status": normalized.index("status"),
        "course": normalized.index("course"),
        "grade": normalized.index("grade"),
        "term": normalized.index("term"),
        "credits": normalized.index("credits"),
    }


def parse_transcript_html(html: str) -> List[HistoryCourse]:
    soup = BeautifulSoup(html, "html.parser")

    # key: (course_code, term)
    # value: tuple(score_tuple, HistoryCourse)
    deduped: Dict[tuple[str, str], tuple[tuple[int, int], HistoryCourse]] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue

        header_map = None
        header_row_index = None

        # Search for the actual header row inside each table.
        for index, row in enumerate(rows):
            texts = get_cell_texts(row)
            maybe_map = header_index_map(texts)
            if maybe_map:
                header_map = maybe_map
                header_row_index = index
                break

        if header_map is None or header_row_index is None:
            continue

        for row in rows[header_row_index + 1:]:
            cells = get_cell_texts(row)

            needed_index = max(header_map.values())
            if len(cells) <= needed_index:
                continue

            status_text = cells[header_map["status"]]
            course_code = cells[header_map["course"]]
            grade = cells[header_map["grade"]]
            term = cells[header_map["term"]]
            credits = parse_credits(cells[header_map["credits"]])

            if status_text not in VALID_HISTORY_STATUSES:
                continue

            if not term:
                continue

            if not course_code:
                continue

            record = HistoryCourse(
                course_code=course_code,
                term=term,
                credits_earned=credits,
                status=status_text,
            )

            key = (course_code, term)
            score = (grade_rank(grade), credits)

            existing = deduped.get(key)
            if existing is None or score > existing[0]:
                deduped[key] = (score, record)

    return [item[1] for item in deduped.values()]


def get_student_or_404(student_id: str) -> Dict[str, Any]:
    student = app.state.students.get(student_id)
    if student is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found",
        )
    return student


def serialize_history(history: List[HistoryCourse]) -> List[Dict[str, Any]]:
    return [course.model_dump() for course in history]


def serialize_plan(plan: List[PlannedCourse]) -> List[Dict[str, Any]]:
    return [course.model_dump() for course in plan]

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


@app.post(
    "/api/v1/students/{student_id}/history/import",
    status_code=status.HTTP_201_CREATED,
)
async def import_student_history(student_id: str, file: UploadFile = File(...)):
    content = await file.read()
    html = content.decode("utf-8", errors="ignore")

    parsed_history = parse_transcript_html(html)

    async with app.state.students_lock:
        app.state.students[student_id] = {
            "history": parsed_history,
            "plan": [],
        }

    return {
        "status": "success",
        "past_courses_imported": len(parsed_history),
    }


@app.put("/api/v1/students/{student_id}/history")
async def update_student_history(student_id: str, body: HistoryUpdateRequest):
    async with app.state.students_lock:
        student = get_student_or_404(student_id)
        student["history"] = body.history

    return {
        "status": "success",
        "message": "Academic history updated successfully",
    }


@app.delete("/api/v1/students/{student_id}/history")
async def delete_student_history(student_id: str):
    async with app.state.students_lock:
        student = get_student_or_404(student_id)
        student["history"] = []

    return {
        "status": "success",
        "message": "Academic history cleared successfully",
    }


@app.post("/api/v1/students/{student_id}/plan")
async def create_student_plan(student_id: str, body: PlanRequest):
    async with app.state.students_lock:
        student = get_student_or_404(student_id)
        student["plan"] = body.planned_courses

    return {
        "status": "success",
        "planned_courses_saved": len(body.planned_courses),
    }


@app.put("/api/v1/students/{student_id}/plan")
async def replace_student_plan(student_id: str, body: PlanRequest):
    async with app.state.students_lock:
        student = get_student_or_404(student_id)
        student["plan"] = body.planned_courses

    return {
        "status": "success",
        "planned_courses_saved": len(body.planned_courses),
    }


@app.delete("/api/v1/students/{student_id}/plan")
async def delete_student_plan(student_id: str):
    async with app.state.students_lock:
        student = get_student_or_404(student_id)
        student["plan"] = []

    return {
        "status": "success",
        "message": "Plan cleared successfully",
    }


@app.get("/api/v1/students/{student_id}/profile")
async def get_student_profile(student_id: str):
    student = get_student_or_404(student_id)

    return {
        "student_id": student_id,
        "history": serialize_history(student["history"]),
        "plan": serialize_plan(student["plan"]),
    }