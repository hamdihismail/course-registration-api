from fastapi.testclient import TestClient

from main import (
    HistoryCourse,
    PlannedCourse,
    app,
    catalog,
    normalize_course_code,
)

client = TestClient(app)


def setup_function() -> None:
    """
    Reset all in-memory data before every test.

    This prevents one test from affecting another.
    """
    catalog.clear()
    app.state.students.clear()


def add_catalog_course(
    course_code: str,
    credits: int = 3,
    prerequisites: list[str] | None = None,
    cross_listed: list[str] | None = None,
) -> None:
    """
    Adds a course using the same normalized structure as the catalog importer.
    """
    normalized_code = normalize_course_code(course_code)

    catalog[normalized_code] = {
        "course_code": normalized_code,
        "title": f"Test course {course_code}",
        "credits": credits,
        "prerequisites": [
            normalize_course_code(code) for code in (prerequisites or [])
        ],
        "cross_listed": [normalize_course_code(code) for code in (cross_listed or [])],
    }


def add_student(
    student_id: str,
    history: list[HistoryCourse] | None = None,
    plan: list[PlannedCourse] | None = None,
) -> None:
    """
    Stores the same Pydantic objects used by the Phase 2 endpoints.
    """
    app.state.students[student_id] = {
        "history": history or [],
        "plan": plan or [],
    }


def test_course_code_normalization() -> None:
    assert normalize_course_code("COSC 3506") == "COSC3506"
    assert normalize_course_code("COSC-3506") == "COSC3506"
    assert normalize_course_code("cosc3506") == "COSC3506"


def test_audit_report_returns_404_for_unknown_student() -> None:
    response = client.get("/api/v1/students/unknown/audit-report")

    assert response.status_code == 404
    assert response.json()["detail"] == "Student not found"


def test_audit_report_ok_when_prerequisite_is_completed_earlier() -> None:
    add_catalog_course("COSC 2007")
    add_catalog_course(
        "COSC 3506",
        prerequisites=["COSC 2007"],
    )

    add_student(
        "111",
        history=[
            HistoryCourse(
                course_code="COSC-2007",
                term="25F",
                credits_earned=3,
                status="Completed",
            )
        ],
        plan=[
            PlannedCourse(
                course_code="COSC-3506",
                term="26F",
            )
        ],
    )

    response = client.get("/api/v1/students/111/audit-report")

    assert response.status_code == 200

    body = response.json()

    assert body["student_id"] == "111"
    assert body["status"] == "ok"
    assert body["timeline_validation"] == []
    assert body["cross_list_violations"] == []

    assert body["credit_summary"] == {
        "total_earned": 3,
        "total_planned": 3,
        "total_remaining_for_graduation": 114,
    }


def test_missing_prerequisite_returns_warning() -> None:
    add_catalog_course("COSC 2007")
    add_catalog_course(
        "COSC 3506",
        prerequisites=["COSC 2007"],
    )

    add_student(
        "111",
        history=[],
        plan=[
            PlannedCourse(
                course_code="COSC-3506",
                term="26F",
            )
        ],
    )

    response = client.get("/api/v1/students/111/audit-report")

    assert response.status_code == 200

    body = response.json()

    assert body["status"] == "warning"
    assert len(body["timeline_validation"]) == 1

    term_result = body["timeline_validation"][0]

    assert term_result["term"] == "26F"
    assert len(term_result["errors"]) == 1

    error = term_result["errors"][0]

    assert error["course_code"] == "COSC-3506"
    assert error["type"] == "MISSING_PREREQUISITE"
    assert error["message"] == "Missing prerequisite: COSC2007"


def test_strict_mode_returns_failed_when_issue_exists() -> None:
    add_catalog_course(
        "COSC 3506",
        prerequisites=["COSC 2007"],
    )

    add_student(
        "111",
        plan=[
            PlannedCourse(
                course_code="COSC-3506",
                term="26F",
            )
        ],
    )

    response = client.get("/api/v1/students/111/audit-report?strict=true")

    assert response.status_code == 200
    assert response.json()["status"] == "failed"


def test_strict_mode_remains_ok_when_no_issue_exists() -> None:
    add_catalog_course("COSC 3506")

    add_student(
        "111",
        plan=[
            PlannedCourse(
                course_code="COSC-3506",
                term="26F",
            )
        ],
    )

    response = client.get("/api/v1/students/111/audit-report?strict=true")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_prerequisite_completed_in_same_term_does_not_count() -> None:
    add_catalog_course("COSC 2007")
    add_catalog_course(
        "COSC 3506",
        prerequisites=["COSC 2007"],
    )

    add_student(
        "111",
        history=[
            HistoryCourse(
                course_code="COSC-2007",
                term="26F",
                credits_earned=3,
                status="Completed",
            )
        ],
        plan=[
            PlannedCourse(
                course_code="COSC-3506",
                term="26F",
            )
        ],
    )

    response = client.get("/api/v1/students/111/audit-report")

    body = response.json()

    assert body["status"] == "warning"
    assert body["timeline_validation"][0]["errors"][0]["type"] == "MISSING_PREREQUISITE"


def test_timeline_validation_is_sorted_chronologically() -> None:
    add_catalog_course(
        "COSC 3001",
        prerequisites=["COSC 1001"],
    )
    add_catalog_course(
        "COSC 3002",
        prerequisites=["COSC 1002"],
    )
    add_catalog_course(
        "COSC 3003",
        prerequisites=["COSC 1003"],
    )

    add_student(
        "111",
        plan=[
            PlannedCourse(
                course_code="COSC-3003",
                term="26F",
            ),
            PlannedCourse(
                course_code="COSC-3001",
                term="26W",
            ),
            PlannedCourse(
                course_code="COSC-3002",
                term="26SP",
            ),
        ],
    )

    response = client.get("/api/v1/students/111/audit-report")

    terms = [result["term"] for result in response.json()["timeline_validation"]]

    assert terms == ["26W", "26SP", "26F"]


def test_cross_listed_completed_course_creates_conflict() -> None:
    add_catalog_course(
        "ITEC 3506",
        cross_listed=["COSC 3506"],
    )

    add_student(
        "111",
        history=[
            HistoryCourse(
                course_code="COSC-3506",
                term="25F",
                credits_earned=3,
                status="Completed",
            )
        ],
        plan=[
            PlannedCourse(
                course_code="ITEC-3506",
                term="26F",
            )
        ],
    )

    response = client.get("/api/v1/students/111/audit-report")

    body = response.json()

    assert body["status"] == "warning"
    assert len(body["cross_list_violations"]) == 1

    violation = body["cross_list_violations"][0]

    assert violation["course_code"] == "ITEC-3506"
    assert violation["type"] == "CROSS_LIST_CONFLICT"
    assert violation["message"] == "Cross-listed with completed course COSC-3506"


def test_attempted_course_does_not_count_as_earned() -> None:
    add_student(
        "111",
        history=[
            HistoryCourse(
                course_code="COSC-2007",
                term="24F",
                credits_earned=3,
                status="Attempted",
            )
        ],
    )

    response = client.get("/api/v1/students/111/audit-report")

    summary = response.json()["credit_summary"]

    assert summary["total_earned"] == 0
    assert summary["total_remaining_for_graduation"] == 120


def test_retake_is_not_double_counted() -> None:
    add_student(
        "111",
        history=[
            HistoryCourse(
                course_code="COSC-2007",
                term="24F",
                credits_earned=0,
                status="Attempted",
            ),
            HistoryCourse(
                course_code="COSC 2007",
                term="25W",
                credits_earned=3,
                status="Completed",
            ),
            HistoryCourse(
                course_code="COSC-2007",
                term="25F",
                credits_earned=3,
                status="Completed",
            ),
        ],
    )

    response = client.get("/api/v1/students/111/audit-report")

    summary = response.json()["credit_summary"]

    assert summary["total_earned"] == 3
    assert summary["total_remaining_for_graduation"] == 117


def test_credit_summary_uses_catalog_credits_for_plan() -> None:
    add_catalog_course(
        "COSC 3001",
        credits=3,
    )
    add_catalog_course(
        "COSC 3002",
        credits=4,
    )

    add_student(
        "111",
        history=[
            HistoryCourse(
                course_code="COSC-2007",
                term="25F",
                credits_earned=3,
                status="Completed",
            )
        ],
        plan=[
            PlannedCourse(
                course_code="COSC-3001",
                term="26W",
            ),
            PlannedCourse(
                course_code="COSC-3002",
                term="26F",
            ),
        ],
    )

    response = client.get("/api/v1/students/111/audit-report")

    summary = response.json()["credit_summary"]

    assert summary == {
        "total_earned": 3,
        "total_planned": 7,
        "total_remaining_for_graduation": 110,
    }


def test_remaining_credits_never_go_below_zero() -> None:
    history = [
        HistoryCourse(
            course_code=f"TEST-{number:04d}",
            term="25F",
            credits_earned=3,
            status="Completed",
        )
        for number in range(1, 42)
    ]

    add_student(
        "111",
        history=history,
    )

    response = client.get("/api/v1/students/111/audit-report")

    summary = response.json()["credit_summary"]

    assert summary["total_earned"] == 123
    assert summary["total_remaining_for_graduation"] == 0
