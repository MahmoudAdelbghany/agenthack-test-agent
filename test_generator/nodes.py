import json
import os
import re
import subprocess
import httpx
from dotenv import load_dotenv

from .state import TestGeneratorState

load_dotenv()

UIP_PATH = os.getenv("UIP_PATH", "uip")


def _run_cli(args: list[str]) -> dict:
    cmd = [UIP_PATH] + args
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _get_test_cases(project_key: str) -> list:
    data = _run_cli(["tm", "testcases", "list", "--project-key", project_key, "--output", "json"])
    return data.get("Data", [])


def _get_test_steps(project_key: str, test_case_id: str) -> list:
    data = _run_cli(["tm", "testcases", "list-steps",
                     "--project-key", project_key,
                     "--test-case-id", test_case_id, "--output", "json"])
    steps = data.get("Data", [])
    sorted_steps = sorted(steps, key=lambda x: x.get("OrderNo", 0))
    return [
        {"description": s.get("Description", ""), "expected_result": s.get("ExpectedResult", "")}
        for s in sorted_steps
    ]


async def _call_ai(prompt: str) -> str:
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    api_token = os.getenv("CLOUDFLARE_API_TOKEN")
    if not account_id or not api_token:
        raise ValueError("Missing CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN in environment or Orchestrator Assets")

    model = "@cf/meta/llama-3.1-8b-instruct-fast"
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messages": [
            {"role": "system", "content": "You are a senior QA automation engineer. Return only valid JSON when asked."},
            {"role": "user", "content": prompt}
        ]
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload, timeout=60.0)
        response.raise_for_status()
        data = response.json()
        if data.get("success"):
            if "response" in data["result"]:
                return data["result"]["response"]
            elif "choices" in data["result"] and len(data["result"]["choices"]) > 0:
                return data["result"]["choices"][0]["message"]["content"]
            else:
                return str(data["result"])
        else:
            raise RuntimeError(f"Cloudflare Workers AI failed: {data.get('errors')}")


async def fetch_test_cases(state: TestGeneratorState) -> dict:
    print("\n" + "=" * 60)
    print(" NODE 1: Fetching test cases from UiPath Test Manager")
    print("=" * 60)
    cases = _get_test_cases(state["project_key"])
    print(f"  Found {len(cases)} test cases")
    for tc in cases:
        print(f"    - {tc['Name']}")
    return {"all_test_cases": cases}


async def analyze_and_filter(state: TestGeneratorState) -> dict:
    print("\n" + "=" * 60)
    print(" NODE 2: Detecting language + Filtering test cases with AI")
    print("=" * 60)

    all_cases = state["all_test_cases"]
    if not all_cases:
        raise ValueError("No test cases found in UiPath Test Manager — cannot proceed")

    cases_list = "\n".join([
        f"- ID: {tc['Id']} | Name: {tc['Name']} | Description: {tc.get('Description', 'N/A')}"
        for tc in all_cases
    ])

    prompt = f"""You are a senior QA engineer reviewing a code change.

## Developer Code:
{state['developer_code']}

## Available Test Cases:
{cases_list}

## Your Task:
Identify ALL test cases relevant to the developer code above.

## Rules:
- Filter based on function INTENT (name and docstring), NOT implementation
- The developer code may contain bugs — ignore implementation details
- Be INCLUSIVE — include ALL variants and edge cases for matching functions

Return ONLY a JSON object:
{{"language": "<language>", "relevant_ids": ["<uuid>", ...]}}

No explanation. No markdown. No code blocks."""

    response = await _call_ai(prompt)

    clean = response.strip().strip("```json").strip("```").strip()
    parsed = json.loads(clean)
    language = parsed.get("language", "Unknown")
    relevant_ids = parsed.get("relevant_ids", [])

    print(f"  Detected language: {language}")

    relevant = [
        {
            "id": tc["Id"],
            "obj_key": tc["ObjKey"],
            "name": tc["Name"],
            "description": tc.get("Description", ""),
            "steps": [],
            "generated_code": "",
            "output_file": ""
        }
        for tc in all_cases if tc["Id"] in relevant_ids
    ]

    if not relevant:
        raise ValueError("No relevant test cases found for the given developer code")

    print(f"  Selected {len(relevant)} relevant test cases:")
    for tc in relevant:
        print(f"    - {tc['name']}")

    return {"detected_language": language, "relevant_test_cases": relevant}


def _parse_steps_from_description(description: str) -> list[dict]:
    import re
    lines = description.split("\n")
    steps = []
    in_steps = False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("steps:"):
            in_steps = True
            continue
        if in_steps:
            match = re.match(r"^\d+\.\s*(.+)", stripped)
            if match:
                steps.append({"description": match.group(1), "expected_result": ""})
            elif stripped and not stripped.startswith("Steps"):
                if steps:
                    steps[-1]["description"] += " " + stripped
    return steps


async def fetch_steps(state: TestGeneratorState) -> dict:
    print("\n" + "=" * 60)
    print(" NODE 3: Fetching steps for each relevant test case")
    print("=" * 60)

    updated = []
    for tc in state["relevant_test_cases"]:
        print(f"  Fetching steps for: {tc['name']}")
        steps = _get_test_steps(state["project_key"], tc["id"])

        if not steps and tc.get("description"):
            steps = _parse_steps_from_description(tc["description"])
            if steps:
                print(f"    Parsed {len(steps)} steps from description")

        tc["steps"] = steps
        print(f"    Got {len(steps)} steps")
        for i, s in enumerate(steps):
            print(f"      {i+1}. {s['description']}")
        updated.append(tc)

    return {"relevant_test_cases": updated}


async def generate_tests(state: TestGeneratorState) -> dict:
    print("\n" + "=" * 60)
    print(" NODE 4: Generating test code with AI")
    print("=" * 60)

    language = state["detected_language"]
    developer_code = state["developer_code"]
    updated = []

    for tc in state["relevant_test_cases"]:
        print(f"  Generating test for: {tc['name']}")

        if tc["steps"]:
            steps_text = "\n".join([
                f"  Step {i+1}: {s['description']}"
                + (f"\n           Expected: {s['expected_result']}" if s.get('expected_result') else "")
                for i, s in enumerate(tc["steps"])
            ])
        else:
            steps_text = tc.get("description", "No steps provided.")

        prompt = f"""Generate an executable test in {language} for this test case.

## Developer Code:
{developer_code}

## Test Case: {tc['name']}

## Test Instructions:
{steps_text}

## Requirements:
- No testing frameworks (no pytest, Jest, JUnit)
- No import statements
- Plain function with simple assertions
- Name the function based on the test case name
- Call the function at the end so it executes directly
- If the developer code has a bug (e.g. add uses * instead of +), the test should still test the INTENDED behavior (add should return a + b)

Return ONLY the function code. No imports. No markdown."""

        code = await _call_ai(prompt)
        code = code.strip()
        if code.startswith("```"):
            lines = code.split("\n")
            code = "\n".join(lines[1:-1])

        tc["generated_code"] = code
        print(f"    Generated ({len(code)} chars)")
        updated.append(tc)

    return {"relevant_test_cases": updated}


async def save_results(state: TestGeneratorState) -> dict:
    print("\n" + "=" * 60)
    print(" NODE 5: Saving generated test files")
    print("=" * 60)

    ext_map = {
        "python": "py", "javascript": "js", "typescript": "ts",
        "java": "java", "c#": "cs", "csharp": "cs",
        "go": "go", "ruby": "rb", "kotlin": "kt",
    }

    language = state["detected_language"].lower()
    ext = ext_map.get(language, "txt")

    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "generated_tests")
    os.makedirs(output_dir, exist_ok=True)

    saved = []
    for tc in state["relevant_test_cases"]:
        if not tc.get("generated_code"):
            continue

        safe_name = re.sub(r'[^a-z0-9]+', '_', tc["name"].lower()).strip('_')
        filename = os.path.join(output_dir, f"test_{safe_name}.{ext}")

        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# Test Case: {tc['name']}\n")
            f.write(f"# Generated by UiPath Test Generator Agent\n")
            f.write(f"# Language: {state['detected_language']}\n\n")
            f.write(tc["generated_code"])

        tc["output_file"] = filename
        saved.append(filename)
        print(f"  Saved: {filename}")

    return {"relevant_test_cases": state["relevant_test_cases"], "output_files": saved}
