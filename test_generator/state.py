from typing import TypedDict, Optional


class TestCaseWithSteps(TypedDict):
    id: str
    obj_key: str
    name: str
    description: str
    steps: list[dict]
    generated_code: str
    output_file: str


class TestGeneratorState(TypedDict):
    project_key: str
    developer_code: str
    all_test_cases: list
    detected_language: str
    relevant_test_cases: list[TestCaseWithSteps]
    output_files: list[str]
    error: Optional[str]
