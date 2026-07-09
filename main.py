from fastapi import FastAPI, UploadFile, File, HTTPException, status, Query
from bs4 import BeautifulSoup
import re
from typing import List, Dict, Any, Optional
import asyncio
from pydantic import BaseModel, Field

app = FastAPI(
    title="Course Registration API", description="Phase 2 API", version="1.0.0"
)

# In-memory storage for Phase 1
catalog = {}


def normalize_course_code(course_code: str) -> str:
    """
    Normalizes course codes so COSC 3506, cosc3506, and COSC3506
    can be treated consistently.
    """
    if not course_code:
        return ""

    return re.sub(r"[\s-]+", "", course_code.strip().upper())


def extract_course_codes(text: str) -> list[str]:
    """
    Extracts course codes from prerequisite and cross-listed text.

    Supports formats such as:
    - COSC 2006
    - COSC-2006
    - COSC2006
    - Requires COSC-1046 and either COSC 1047 or ITEC-1047
    """
    if not text:
        return []

    cleaned_text = text.strip()

    if cleaned_text.lower() in {"none", "n/a", ""}:
        return []

    pattern = r"\b[A-Z]{3,4}[\s-]*\d{4}\b"
    matches = re.findall(pattern, cleaned_text.upper())

    normalized_codes = []

    for match in matches:
        normalized_code = normalize_course_code(match)

        if normalized_code not in normalized_codes:
            normalized_codes.append(normalized_code)

    return normalized_codes


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
            "cross_listed": extract_course_codes(cross_listed_raw),
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

        for row in rows[header_row_index + 1 :]:
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


def term_sort_key(term: str) -> tuple[int, int]:
    # """
    # Converts terms into sortable values.

    # Required order:
    # W < SP < S < F

    # Examples:
    # 23F  -> (23, 4)
    # 24W  -> (24, 1)
    # 26SP -> (26, 2)
    # 26S  -> (26, 3)
    # 26F  -> (26, 4)
    # """
    if not term:
        return (999, 999)

    term = term.upper().strip()

    match = re.match(r"^(\d{2})(W|SP|S|F)$", term)
    if not match:
        return (999, 999)

    year = int(match.group(1))
    season = match.group(2)

    season_order = {
        "W": 1,
        "SP": 2,
        "S": 3,
        "F": 4,
    }

    return (year, season_order[season])


def is_strictly_earlier(term_a: str, term_b: str) -> bool:
    """
    Returns True if term_a happens before term_b.
    """
    return term_sort_key(term_a) < term_sort_key(term_b)


def clean_course_list(value) -> list[str]:
    """
    Normalizes prerequisites/cross-listed values into a list.

    This protects you in case your Phase 1 parser stored:
    - a list
    - a comma-separated string
    - None
    """
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    if isinstance(value, str):
        if not value.strip() or value.strip().lower() == "none":
            return []

        parts = re.split(r",|;|/|\band\b", value, flags=re.IGNORECASE)
        return [part.strip() for part in parts if part.strip()]

    return []


def get_course_value(
    course: Any,
    field_name: str,
    default: Any = None,
) -> Any:
    """
    Reads a value from either:
    - a Pydantic model, such as HistoryCourse or PlannedCourse
    - a dictionary

    This keeps the Phase 3 logic compatible with the Phase 2 storage format.
    """
    if isinstance(course, dict):
        return course.get(field_name, default)

    return getattr(course, field_name, default)


def get_completed_courses_by_code(
    history: List[HistoryCourse],
) -> Dict[str, HistoryCourse]:
    """
    Returns one completed course per normalized course code.

    Non-completed attempts do not count.
    If a completed course appears more than once, the latest completed
    occurrence replaces the earlier one.
    """
    completed: Dict[str, HistoryCourse] = {}

    sorted_history = sorted(
        history,
        key=lambda item: term_sort_key(get_course_value(item, "term", "")),
    )

    for item in sorted_history:
        course_status = get_course_value(item, "status", "")

        if course_status != "Completed":
            continue

        course_code = get_course_value(item, "course_code", "")
        normalized_code = normalize_course_code(course_code)

        completed[normalized_code] = item

    return completed


def validate_prerequisites(
    plan: List[PlannedCourse],
    history: List[HistoryCourse],
    catalog: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Checks that every prerequisite was completed in a strictly earlier term.
    """
    errors_by_term: Dict[str, List[Dict[str, str]]] = {}

    completed_history = [
        item for item in history if get_course_value(item, "status", "") == "Completed"
    ]

    for planned_course in plan:
        planned_code = get_course_value(
            planned_course,
            "course_code",
            "",
        )
        planned_term = get_course_value(
            planned_course,
            "term",
            "",
        )

        planned_normalized = normalize_course_code(planned_code)
        catalog_course = catalog.get(planned_normalized)

        if catalog_course is None:
            continue

        prerequisites = clean_course_list(catalog_course.get("prerequisites"))

        for prerequisite_code in prerequisites:
            prerequisite_normalized = normalize_course_code(prerequisite_code)

            prerequisite_completed_before = False

            for completed_course in completed_history:
                completed_code = get_course_value(
                    completed_course,
                    "course_code",
                    "",
                )
                completed_term = get_course_value(
                    completed_course,
                    "term",
                    "",
                )

                completed_normalized = normalize_course_code(completed_code)

                if (
                    completed_normalized == prerequisite_normalized
                    and is_strictly_earlier(
                        completed_term,
                        planned_term,
                    )
                ):
                    prerequisite_completed_before = True
                    break

            if not prerequisite_completed_before:
                errors_by_term.setdefault(
                    planned_term,
                    [],
                ).append(
                    {
                        "course_code": planned_code,
                        "type": "MISSING_PREREQUISITE",
                        "message": (f"Missing prerequisite: " f"{prerequisite_code}"),
                    }
                )

    timeline_validation: List[Dict[str, Any]] = []

    for term in sorted(
        errors_by_term.keys(),
        key=term_sort_key,
    ):
        timeline_validation.append(
            {
                "term": term,
                "errors": errors_by_term[term],
            }
        )

    return timeline_validation


def validate_cross_listings(
    plan: List[PlannedCourse],
    history: List[HistoryCourse],
    catalog: Dict[str, Dict[str, Any]],
) -> List[Dict[str, str]]:
    """
    Checks whether a planned course is cross-listed with a course
    the student already completed.
    """
    violations: List[Dict[str, str]] = []

    completed_by_code = get_completed_courses_by_code(history)
    completed_codes = set(completed_by_code.keys())

    for planned_course in plan:
        planned_code = get_course_value(
            planned_course,
            "course_code",
            "",
        )

        planned_normalized = normalize_course_code(planned_code)
        catalog_course = catalog.get(planned_normalized)

        if catalog_course is None:
            continue

        cross_listed_courses = clean_course_list(catalog_course.get("cross_listed"))

        for cross_listed_code in cross_listed_courses:
            cross_normalized = normalize_course_code(cross_listed_code)

            if cross_normalized not in completed_codes:
                continue

            completed_course = completed_by_code[cross_normalized]

            completed_display_code = get_course_value(
                completed_course,
                "course_code",
                cross_listed_code,
            )

            violations.append(
                {
                    "course_code": planned_code,
                    "type": "CROSS_LIST_CONFLICT",
                    "message": (
                        "Cross-listed with completed course "
                        f"{completed_display_code}"
                    ),
                }
            )

    return violations


GRADUATION_TARGET = 120


def calculate_credit_summary(
    plan: List[PlannedCourse],
    history: List[HistoryCourse],
    catalog: Dict[str, Dict[str, Any]],
) -> Dict[str, int]:
    """
    Calculates earned, planned, and remaining graduation credits.
    """
    completed_by_code = get_completed_courses_by_code(history)

    total_earned = 0

    for completed_course in completed_by_code.values():
        credits_earned = get_course_value(
            completed_course,
            "credits_earned",
            0,
        )

        try:
            total_earned += int(credits_earned)
        except (TypeError, ValueError):
            continue

    total_planned = 0

    for planned_course in plan:
        planned_code = get_course_value(
            planned_course,
            "course_code",
            "",
        )

        planned_normalized = normalize_course_code(planned_code)
        catalog_course = catalog.get(planned_normalized)

        if catalog_course is None:
            continue

        try:
            total_planned += int(catalog_course.get("credits", 0))
        except (TypeError, ValueError):
            continue

    total_remaining = max(
        0,
        GRADUATION_TARGET - total_earned - total_planned,
    )

    return {
        "total_earned": total_earned,
        "total_planned": total_planned,
        "total_remaining_for_graduation": total_remaining,
    }


@app.get("/")
def root():
    return {"message": "Course Registration API is running", "docs": "/docs"}


@app.post("/api/v1/admin/catalog/import")
async def import_catalog(file: UploadFile = File(...)):
    """
    Uploads and imports an HTML course catalog.
    """
    if not file.filename.lower().endswith((".html", ".htm")):
        raise HTTPException(status_code=400, detail="Only HTML files are accepted.")

    try:
        contents = await file.read()
        html_content = contents.decode("utf-8")

        parsed_courses = parse_catalog_html(html_content)

        # Replace current in-memory catalog with imported data
        catalog.clear()
        catalog.update(parsed_courses)

        return {
            "message": "Catalog imported successfully.",
            "courses_imported": len(catalog),
        }

    except UnicodeDecodeError:
        raise HTTPException(
            status_code=400, detail="Uploaded file must be valid UTF-8 HTML."
        )

    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error while importing catalog: {str(error)}",
        )


@app.get("/api/v1/catalog/courses/{course_code}")
def get_course(course_code: str):
    """
    Returns a single course by course code.
    """
    normalized_code = normalize_course_code(course_code)

    course = catalog.get(normalized_code)

    if course is None:
        raise HTTPException(status_code=404, detail="Course not found.")

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


@app.get("/api/v1/students/{student_id}/audit-report")
async def get_audit_report(
    student_id: str,
    strict: bool = Query(default=False),
):
    # if student_id not in get_student_or_404(student_id):
    #     raise HTTPException(status_code=404, detail="Student not found")

    student = get_student_or_404(student_id)

    history = student.get("history", [])
    plan = student.get("plan", [])

    timeline_validation = validate_prerequisites(
        plan=plan,
        history=history,
        catalog=catalog,
    )

    cross_list_violations = validate_cross_listings(
        plan=plan,
        history=history,
        catalog=catalog,
    )

    credit_summary = calculate_credit_summary(
        plan=plan,
        history=history,
        catalog=catalog,
    )

    has_issues = bool(timeline_validation or cross_list_violations)

    if not has_issues:
        audit_status = "ok"
    elif strict:
        audit_status = "failed"
    else:
        audit_status = "warning"

    return {
        "student_id": student_id,
        "status": audit_status,
        "timeline_validation": timeline_validation,
        "cross_list_violations": cross_list_violations,
        "credit_summary": credit_summary,
    }
