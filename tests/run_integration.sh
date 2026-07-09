#!/usr/bin/env bash

set -euo pipefail

BASE_URL="http://127.0.0.1:8000"
OUTPUT_DIR="tests/integration_output"

mkdir -p "${OUTPUT_DIR}"

echo "Checking API health..."

curl --fail --silent \
  "${BASE_URL}/" \
  > "${OUTPUT_DIR}/root-response.json"

echo "Importing sample catalog..."

curl --fail --silent \
  -X POST \
  -F "file=@sample_catalog.html;type=text/html" \
  "${BASE_URL}/api/v1/admin/catalog/import" \
  > "${OUTPUT_DIR}/catalog-response.json"

echo "Importing sample transcript..."

curl --fail --silent \
  -X POST \
  -F "file=@student-example.html;type=text/html" \
  "${BASE_URL}/api/v1/students/111/history/import" \
  > "${OUTPUT_DIR}/history-response.json"

echo "Saving student plan..."

curl --fail --silent \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{
        "planned_courses": [
          {
            "course_code": "COSC-3506",
            "term": "26F"
          }
        ]
      }' \
  "${BASE_URL}/api/v1/students/111/plan" \
  > "${OUTPUT_DIR}/plan-response.json"

echo "Requesting audit report..."

curl --fail --silent \
  "${BASE_URL}/api/v1/students/111/audit-report" \
  > "${OUTPUT_DIR}/audit-response.json"

python - <<'PY'
import json
from pathlib import Path

output_dir = Path("tests/integration_output")

catalog_result = json.loads(
    (output_dir / "catalog-response.json").read_text(encoding="utf-8")
)

history_result = json.loads(
    (output_dir / "history-response.json").read_text(encoding="utf-8")
)

plan_result = json.loads(
    (output_dir / "plan-response.json").read_text(encoding="utf-8")
)

audit_result = json.loads(
    (output_dir / "audit-response.json").read_text(encoding="utf-8")
)

assert catalog_result["courses_imported"] == 5
assert history_result["past_courses_imported"] > 0
assert plan_result["planned_courses_saved"] == 1

assert audit_result["student_id"] == "111"
assert audit_result["status"] in {"ok", "warning"}
assert "timeline_validation" in audit_result
assert "cross_list_violations" in audit_result
assert "credit_summary" in audit_result

credit_summary = audit_result["credit_summary"]

assert credit_summary["total_earned"] >= 0
assert credit_summary["total_planned"] == 3
assert credit_summary["total_remaining_for_graduation"] >= 0

print("Integration test passed.")
print(json.dumps(audit_result, indent=2))
PY