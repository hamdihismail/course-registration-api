from pathlib import Path

from fastapi.testclient import TestClient

from main import app, catalog, extract_course_codes

client = TestClient(app)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def setup_function() -> None:
    catalog.clear()
    app.state.students.clear()


def test_root_endpoint() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json()["message"] == ("Course Registration API is running")


def test_extract_course_codes_supports_hyphens() -> None:
    result = extract_course_codes(
        "Requires COSC-3127 and either COSC 3506 or ITEC-3506"
    )

    assert result == [
        "COSC3127",
        "COSC3506",
        "ITEC3506",
    ]


def test_extract_cross_listed_hyphenated_code() -> None:
    result = extract_course_codes("Cross-listed with COSC-3506")

    assert result == ["COSC3506"]


def test_catalog_import_and_lookup() -> None:
    catalog_path = PROJECT_ROOT / "sample_catalog.html"

    with catalog_path.open("rb") as catalog_file:
        response = client.post(
            "/api/v1/admin/catalog/import",
            files={
                "file": (
                    "sample_catalog.html",
                    catalog_file,
                    "text/html",
                )
            },
        )

    assert response.status_code == 200
    assert response.json()["courses_imported"] == 5

    lookup_response = client.get("/api/v1/catalog/courses/COSC-3506")

    assert lookup_response.status_code == 200

    body = lookup_response.json()

    assert body["course_code"] == "COSC3506"
    assert body["credits"] == 3
    assert body["prerequisites"] == ["COSC2007"]
    assert body["cross_listed"] == ["ITEC3506"]


def test_catalog_lookup_returns_404() -> None:
    response = client.get("/api/v1/catalog/courses/DOES-9999")

    assert response.status_code == 404


def test_student_history_import_and_profile() -> None:
    transcript_path = PROJECT_ROOT / "student-example.html"

    with transcript_path.open("rb") as transcript_file:
        response = client.post(
            "/api/v1/students/111/history/import",
            files={
                "file": (
                    "student-example.html",
                    transcript_file,
                    "text/html",
                )
            },
        )

    assert response.status_code == 201
    assert response.json()["past_courses_imported"] > 0

    profile_response = client.get("/api/v1/students/111/profile")

    assert profile_response.status_code == 200

    profile = profile_response.json()

    assert profile["student_id"] == "111"
    assert isinstance(profile["history"], list)
    assert profile["plan"] == []


def test_plan_lifecycle() -> None:
    app.state.students["111"] = {
        "history": [],
        "plan": [],
    }

    plan_body = {
        "planned_courses": [
            {
                "course_code": "COSC-3506",
                "term": "26F",
            }
        ]
    }

    create_response = client.post(
        "/api/v1/students/111/plan",
        json=plan_body,
    )

    assert create_response.status_code == 200
    assert create_response.json()["planned_courses_saved"] == 1

    profile_response = client.get("/api/v1/students/111/profile")

    assert profile_response.json()["plan"] == [
        {
            "course_code": "COSC-3506",
            "term": "26F",
        }
    ]

    replace_response = client.put(
        "/api/v1/students/111/plan",
        json={
            "planned_courses": [
                {
                    "course_code": "COSC-2007",
                    "term": "25F",
                }
            ]
        },
    )

    assert replace_response.status_code == 200

    delete_response = client.delete("/api/v1/students/111/plan")

    assert delete_response.status_code == 200

    final_profile = client.get("/api/v1/students/111/profile").json()

    assert final_profile["plan"] == []


def test_history_update_and_delete() -> None:
    app.state.students["111"] = {
        "history": [],
        "plan": [],
    }

    update_response = client.put(
        "/api/v1/students/111/history",
        json={
            "history": [
                {
                    "course_code": "COSC-2007",
                    "term": "25F",
                    "credits_earned": 3,
                    "status": "Completed",
                }
            ]
        },
    )

    assert update_response.status_code == 200

    profile = client.get("/api/v1/students/111/profile").json()

    assert len(profile["history"]) == 1

    delete_response = client.delete("/api/v1/students/111/history")

    assert delete_response.status_code == 200

    final_profile = client.get("/api/v1/students/111/profile").json()

    assert final_profile["history"] == []


def test_unknown_student_operations_return_404() -> None:
    assert client.get("/api/v1/students/unknown/profile").status_code == 404

    assert (
        client.post(
            "/api/v1/students/unknown/plan",
            json={"planned_courses": []},
        ).status_code
        == 404
    )

    assert (
        client.put(
            "/api/v1/students/unknown/history",
            json={"history": []},
        ).status_code
        == 404
    )


def test_catalog_import_parses_hyphenated_relationships() -> None:
    html = """
    <html>
    <body>
        <table>
            <tr>
                <th>Course Code</th>
                <th>Title</th>
                <th>Credits</th>
                <th>Prerequisites</th>
                <th>Cross-listed</th>
            </tr>
            <tr>
                <td>COSC-4426</td>
                <td>Advanced Software</td>
                <td>3</td>
                <td>COSC-3127</td>
                <td>ITEC-4426</td>
            </tr>
        </table>
    </body>
    </html>
    """

    response = client.post(
        "/api/v1/admin/catalog/import",
        files={
            "file": (
                "catalog.html",
                html.encode("utf-8"),
                "text/html",
            )
        },
    )

    assert response.status_code == 200

    course_response = client.get("/api/v1/catalog/courses/COSC-4426")

    assert course_response.status_code == 200

    course = course_response.json()

    assert course["prerequisites"] == ["COSC3127"]
    assert course["cross_listed"] == ["ITEC4426"]
