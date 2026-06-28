import asyncio
import os
import re
import subprocess
import json
import base64
import glob as globmod
import httpx
from pydantic import BaseModel, Field
from typing import List, Optional
from langgraph.graph import START, StateGraph, END
from langgraph.types import Command, interrupt
from uipath.platform import UiPath
from uipath.platform.connections import ActivityMetadata, ActivityParameterLocationInfo
from uipath.eval.mocks import mockable

from test_generator.nodes import (
    _get_test_cases, _get_test_steps, _parse_steps_from_description,
    _call_ai as call_ai,
)

@mockable()
def get_user_approval(test_name: str, explanation: str, proposed_fix: str) -> dict:
    return interrupt({
        "prompt": "Review proposed self-healing fix",
        "test_case": test_name,
        "explanation": explanation,
        "proposed_fix": proposed_fix
    })

SLACK_SEND_MESSAGE = ActivityMetadata(
    object_path="/send_message_to_channel_v2",
    method_name="POST",
    content_type="application/json",
    parameter_location_info=ActivityParameterLocationInfo(
        query_params=["send_as"],
        body_fields=["channel", "messageToSend", "attachment", "buttons"],
    ),
)

class GraphInput(BaseModel):
    developer_code: str = Field(description="Source code from the developer's PR")
    project_key: str = Field(description="UiPath Test Manager project key")
    test_set_key: str = Field(description="UiPath Test Manager test set key to run")
    slack_channel: Optional[str] = Field(default=None, description="Slack channel for approval notifications")
    branch: Optional[str] = Field(default=None, description="Git branch to push the fix to (defaults to main)")
    approved: Optional[bool] = Field(default=None, description="Pre-approved flag (used for resume/bypass)")

class GraphState(BaseModel):
    developer_code: str
    project_key: str
    test_set_key: str
    slack_channel: Optional[str] = None
    branch: Optional[str] = None
    approved: Optional[bool] = None

    all_test_cases: List[dict] = []
    detected_language: str = ""
    relevant_test_cases: List[dict] = []
    output_files: List[str] = []

    execution_id: Optional[str] = None
    test_run_status: str = ""

    failed_tests: List[dict] = []
    current_test_index: int = 0
    status: str = "initialized"
    explanation: str = ""
    applied_fix: Optional[str] = None

class GraphOutput(BaseModel):
    status: str
    explanation: str
    applied_fix: Optional[str] = None

# ── Helpers ──────────────────────────────────────────────────────────────

def _run_cli(args: list[str]) -> dict:
    cmd = ["uip"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)

async def call_cloudflare_ai(prompt: str, system_prompt: str = "You are a helpful coding assistant.") -> str:
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    api_token = os.getenv("CLOUDFLARE_API_TOKEN")
    if not account_id or not api_token:
        sdk = UiPath()
        if not account_id:
            account_id = sdk.assets.retrieve("CLOUDFLARE_ACCOUNT_ID").value
        if not api_token:
            api_token = sdk.assets.retrieve("CLOUDFLARE_API_TOKEN").value
    if not account_id or not api_token:
        raise ValueError("Missing CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN")

    model = "@cf/meta/llama-3.1-8b-instruct-fast"
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    payload = {"messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt}
    ]}
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
        raise RuntimeError(f"Cloudflare Workers AI failed: {data.get('errors')}")

async def call_cloudflare_ai_json(prompt: str, system_prompt: str) -> dict:
    raw = await call_cloudflare_ai(prompt, system_prompt)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n", "", cleaned)
        cleaned = re.sub(r"\n```$", "", cleaned)
        cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        em = re.search(r'"explanation"\s*:\s*"([^"]+)"', cleaned)
        fm = re.search(r'"fixed_content"\s*:\s*"(.*)"', cleaned, re.DOTALL)
        exp = em.group(1) if em else "Could not parse explanation."
        fix = fm.group(1).replace('\\"', '"').replace('\\n', '\n') if fm else ""
        return {"explanation": exp, "fixed_content": fix}

async def post_to_slack(channel: str, message: str, attachment: dict = None) -> bool:
    sdk = UiPath()
    slack_connection_id = "ad204f94-960f-42ea-afac-05e6889bf3b6"
    try:
        slack_conn = sdk.connections.retrieve(slack_connection_id)
    except Exception:
        slack_conn = sdk.connections.retrieve("slack-triage")
    sdk.connections.invoke_activity(
        activity_metadata=SLACK_SEND_MESSAGE,
        connection_id=slack_conn.id,
        activity_input={"channel": channel, "messageToSend": message,
                        "attachment": attachment, "send_as": "bot"}
    )
    return True

async def update_github_file(owner: str, repo: str, path: str, content: str, token: str, branch: str = "main") -> bool:
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json",
               "User-Agent": "uipath-self-healing-agent"}
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    async with httpx.AsyncClient() as client:
        res = await client.get(url, headers=headers)
        res.raise_for_status()
        sha = res.json().get("sha")
        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
        put = await client.put(url, headers=headers, json={
            "message": "Auto-heal: Fix test failure", "content": encoded, "sha": sha, "branch": branch
        })
        put.raise_for_status()
        print(f"[GitHub] Updated {path}")
        return True

# ══════════════════════════════════════════════════════════════════════════
#  PHASE 1 — Test Generation (from FatmaMahmoudBadr/uipath-test-agent)
# ══════════════════════════════════════════════════════════════════════════

async def tg_fetch_test_cases(state: GraphState) -> Command:
    print("\n" + "=" * 60)
    print(" [TEST GEN] Fetching test cases from UiPath Test Manager")
    print("=" * 60)
    cases = _get_test_cases(state.project_key)
    print(f"  Found {len(cases)} test cases")
    for tc in cases:
        print(f"    - {tc['Name']}")
    return Command(update={"all_test_cases": cases})

async def tg_analyze_and_filter(state: GraphState) -> Command:
    print("\n" + "=" * 60)
    print(" [TEST GEN] Detecting language + Filtering test cases with AI")
    print("=" * 60)
    all_cases = state.all_test_cases
    if not all_cases:
        return Command(update={"detected_language": "Unknown", "relevant_test_cases": [], "status": "no_test_cases"})

    cases_list = "\n".join([
        f"- ID: {tc['Id']} | Name: {tc['Name']} | Description: {tc.get('Description', 'N/A')}"
        for tc in all_cases
    ])
    prompt = f"""You are a senior QA engineer reviewing a code change.

## Developer Code:
{state.developer_code}

## Available Test Cases:
{cases_list}

## Your Task:
Identify ALL test cases relevant to the developer code.

## Rules:
- Filter by function INTENT (name/docstring), NOT implementation
- Code may contain bugs — ignore implementation details
- Be INCLUSIVE — include ALL variants and edge cases

Return ONLY: {{"language": "<lang>", "relevant_ids": ["<uuid>", ...]}}
No explanation. No markdown."""

    response = await call_ai(prompt)
    clean = response.strip().strip("```json").strip("```").strip()
    parsed = json.loads(clean)
    language = parsed.get("language", "Unknown")
    relevant_ids = parsed.get("relevant_ids", [])

    print(f"  Detected language: {language}")

    relevant = [
        {"id": tc["Id"], "obj_key": tc["ObjKey"], "name": tc["Name"],
         "description": tc.get("Description", ""), "steps": [],
         "generated_code": "", "output_file": ""}
        for tc in all_cases if tc["Id"] in relevant_ids
    ]

    if not relevant:
        print("  No relevant test cases found — nothing to generate.")
        return Command(update={"detected_language": language, "relevant_test_cases": [], "status": "no_relevant_cases"})

    print(f"  Selected {len(relevant)} relevant test cases:")
    for tc in relevant:
        print(f"    - {tc['name']}")

    return Command(update={"detected_language": language, "relevant_test_cases": relevant})

async def tg_fetch_steps(state: GraphState) -> Command:
    print("\n" + "=" * 60)
    print(" [TEST GEN] Fetching steps for each relevant test case")
    print("=" * 60)
    updated = []
    for tc in state.relevant_test_cases:
        print(f"  Fetching steps for: {tc['name']}")
        steps = _get_test_steps(state.project_key, tc["id"])
        if not steps and tc.get("description"):
            steps = _parse_steps_from_description(tc["description"])
            if steps:
                print(f"    Parsed {len(steps)} steps from description")
        tc["steps"] = steps
        print(f"    Got {len(steps)} steps")
        for i, s in enumerate(steps):
            print(f"      {i+1}. {s['description']}")
        updated.append(tc)
    return Command(update={"relevant_test_cases": updated})

async def tg_generate_tests(state: GraphState) -> Command:
    print("\n" + "=" * 60)
    print(" [TEST GEN] Generating test code with AI")
    print("=" * 60)
    updated = []
    for tc in state.relevant_test_cases:
        print(f"  Generating test for: {tc['name']}")
        if tc["steps"]:
            steps_text = "\n".join([
                f"  Step {i+1}: {s['description']}"
                + (f"\n           Expected: {s['expected_result']}" if s.get('expected_result') else "")
                for i, s in enumerate(tc["steps"])
            ])
        else:
            steps_text = tc.get("description", "No steps provided.")

        prompt = f"""Generate an executable test in {state.detected_language} for this test case.

## Developer Code:
{state.developer_code}

## Test Case: {tc['name']}

## Test Instructions:
{steps_text}

## Requirements:
- No testing frameworks (no pytest, Jest, JUnit)
- No import statements
- Plain function with simple assertions
- Name the function based on the test case name
- Call the function at the end so it executes directly
- If the developer code has a bug, the test should still test the INTENDED behavior

Return ONLY the function code. No imports. No markdown."""

        code = await call_ai(prompt)
        code = code.strip()
        if code.startswith("```"):
            lines = code.split("\n")
            code = "\n".join(lines[1:-1])
        tc["generated_code"] = code
        print(f"    Generated ({len(code)} chars)")
        updated.append(tc)
    return Command(update={"relevant_test_cases": updated})

async def tg_save_results(state: GraphState) -> Command:
    print("\n" + "=" * 60)
    print(" [TEST GEN] Saving generated test files")
    print("=" * 60)
    ext_map = {"python": "py", "javascript": "js", "typescript": "ts",
               "java": "java", "c#": "cs", "csharp": "cs", "go": "go", "ruby": "rb"}
    ext = ext_map.get(state.detected_language.lower(), "txt")
    output_dir = os.path.join(os.path.dirname(__file__), "generated_tests")
    os.makedirs(output_dir, exist_ok=True)
    saved = []
    for tc in state.relevant_test_cases:
        if not tc.get("generated_code"):
            continue
        safe_name = re.sub(r'[^a-z0-9]+', '_', tc["name"].lower()).strip('_')
        filename = os.path.join(output_dir, f"test_{safe_name}.{ext}")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"# Test Case: {tc['name']}\n")
            f.write(f"# Generated by UiPath Test Generator Agent\n")
            f.write(f"# Language: {state.detected_language}\n\n")
            f.write(tc["generated_code"])
        tc["output_file"] = filename
        saved.append(filename)
        print(f"  Saved: {filename}")
    return Command(update={"relevant_test_cases": state.relevant_test_cases, "output_files": saved, "status": "tests_generated"})

# ══════════════════════════════════════════════════════════════════════════
#  PHASE 2 — Run Tests in UiPath Test Cloud
# ══════════════════════════════════════════════════════════════════════════

async def trigger_test_execution(state: GraphState) -> Command:
    print("\n" + "=" * 60)
    print(" [EXECUTION] Triggering test run in UiPath Test Cloud")
    print("=" * 60)
    run_result = _run_cli([
        "tm", "testsets", "run",
        "--test-set-key", state.test_set_key,
        "--output", "json"
    ])
    execution_id = (run_result.get("Data", {}).get("Id")
                    or run_result.get("Data", {}).get("ExecutionId")
                    or (run_result["Data"] if isinstance(run_result.get("Data"), str) else None))
    if not execution_id:
        raise RuntimeError(f"Could not extract execution ID from: {json.dumps(run_result)}")
    print(f"  Test execution triggered! Execution ID: {execution_id}")
    return Command(update={"execution_id": execution_id, "status": "execution_triggered"})

async def wait_for_execution(state: GraphState) -> Command:
    print("\n" + "=" * 60)
    print(" [EXECUTION] Waiting for test execution to complete...")
    print("=" * 60)
    wait_result = _run_cli([
        "tm", "wait",
        "--execution-id", state.execution_id,
        "--project-key", state.project_key,
        "--timeout", "600",
        "--output", "json"
    ])
    status = wait_result.get("Data", {}).get("Status", "Failed")
    print(f"  Execution finished. Status: {status}")
    return Command(update={"test_run_status": status, "status": "execution_complete"})

# ══════════════════════════════════════════════════════════════════════════
#  PHASE 3 — Self-Healing (diagnose → Slack → fix)
# ══════════════════════════════════════════════════════════════════════════

async def sh_fetch_failures(state: GraphState) -> Command:
    print("\n" + "=" * 60)
    print(" [SELF-HEAL] Fetching failures from UiPath Test Manager")
    print("=" * 60)
    log_data = _run_cli([
        "tm", "executions", "testcaselogs", "list",
        "--execution-id", state.execution_id,
        "--project-key", state.project_key,
        "--only-failed", "--output", "json"
    ])
    if log_data.get("Result") != "Success" or not log_data.get("Data"):
        print("  No failures found.")
        return Command(update={"failed_tests": [], "status": "no_failures"})

    failed_tests = []
    for log in log_data["Data"]:
        log_id = log.get("Id")
        test_name = log.get("TestCaseName", "Unknown")
        print(f"  Failed: {test_name} (log_id={log_id})")

        assert_data = _run_cli([
            "tm", "testcaselog", "list-assertions",
            "--project-key", state.project_key,
            "--test-case-log-id", log_id, "--output", "json"
        ])
        error_msg, traceback_str = "", ""
        if assert_data.get("Result") == "Success" and assert_data.get("Data"):
            error_msg = assert_data["Data"][0].get("Message", "Assertion failed")
            traceback_str = assert_data["Data"][0].get("StackTrace", "")

        file_path = f"tests/test_{test_name.lower().replace(' ', '_')}.py"
        if not os.path.exists(file_path):
            src_files = globmod.glob("src/**/*.py", recursive=True)
            file_path = src_files[0] if src_files else "src/calculator.py"

        code_context = ""
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                code_context = f.read()

        failed_tests.append({
            "test_name": test_name, "file_path": file_path,
            "error_message": error_msg, "traceback": traceback_str,
            "code_context": code_context, "proposed_fix": None, "explanation": None
        })

    print(f"  Total failures: {len(failed_tests)}")
    return Command(update={"failed_tests": failed_tests, "status": "failures_fetched"})

async def sh_diagnose_failures(state: GraphState) -> Command:
    if not state.failed_tests:
        return Command(update={"status": "no_failures", "explanation": "No failures to diagnose."})

    print("\n" + "=" * 60)
    print(" [SELF-HEAL] Diagnosing with Cloudflare AI")
    print("=" * 60)
    current = state.failed_tests[state.current_test_index]

    prompt = f"""A test is failing because the SOURCE CODE has a bug.
Source File: {current['file_path']}
Test Name: {current['test_name']}
Error: {current['error_message']}
Traceback:
{current['traceback']}

SOURCE file content:
```python
{current['code_context']}
```

The test is CORRECT. Fix the SOURCE code, not the test.
Return JSON: {{"explanation": "...", "fixed_content": "complete fixed file"}}"""

    system_prompt = """You are a self-healing coding assistant. Respond with valid JSON:
{"explanation": "...", "fixed_content": "complete fixed file"}
No markdown. No extra text."""

    diagnosis = await call_cloudflare_ai_json(prompt, system_prompt)
    explanation = diagnosis.get("explanation", "Could not analyze.")
    fixed_content = diagnosis.get("fixed_content", current["code_context"])
    current["explanation"] = explanation
    current["proposed_fix"] = fixed_content
    print(f"  Diagnosis: {explanation}")
    return Command(update={"failed_tests": state.failed_tests, "explanation": explanation, "status": "diagnosed"})

async def sh_slack_notification(state: GraphState) -> Command:
    current = state.failed_tests[state.current_test_index]
    if state.approved:
        return Command(update={"approved": True, "status": "approval_processed"})

    exec_id = state.execution_id or "unknown"
    msg = (f"*Test Failure Alert*\n"
           f"*Test Case:* `{current['test_name']}` in `{current['file_path']}`\n"
           f"*Root Cause:* {current['explanation']}\n\n"
           f"EXEC_ID: {exec_id}\n\n"
           f"I suggest applying the fix. Would you like me to do it?")

    print("\n=== SLACK NOTIFICATION PENDING ===")
    print(msg)
    print("==================================\n")

    if state.slack_channel:
        blocks = {"blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": msg}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Yes, implement the fix"},
                 "style": "primary", "value": f"EXEC_ID: {exec_id}", "action_id": "approve_fix"},
                {"type": "button", "text": {"type": "plain_text", "text": "No, skip"},
                 "style": "danger", "value": f"EXEC_ID: {exec_id}", "action_id": "reject_fix"}
            ]}
        ]}
        await post_to_slack(state.slack_channel, "Test Failure Triage Alert", blocks)

    decision = get_user_approval(current['test_name'], current['explanation'], current['proposed_fix'])
    approved = decision.get("approved", False)
    print(f"  Approval received: {approved}")
    return Command(update={"approved": approved, "status": "approval_processed"})

async def sh_self_healing(state: GraphState) -> Command:
    if not state.approved:
        print("  Self-healing skipped: rejected.")
        return Command(update={"status": "skipped", "explanation": "Rejected by user."})

    current = state.failed_tests[state.current_test_index]
    print(f"\n  Applying fix for: {current['test_name']}")

    with open(current["file_path"], "w") as f:
        f.write(current["proposed_fix"])
    print(f"  Wrote fix to {current['file_path']}")

    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        try:
            sdk = UiPath()
            github_token = sdk.assets.retrieve("GITHUB_TOKEN").value
        except Exception:
            pass

    if github_token:
        print("  Pushing fix to GitHub...")
        owner = os.getenv("GITHUB_OWNER", "MahmoudAdelbghany")
        repo = os.getenv("GITHUB_REPO", "agenthack-test-agent")
        branch = state.branch or os.getenv("GITHUB_BRANCH", "main")
        await update_github_file(owner, repo, current["file_path"], current["proposed_fix"], github_token, branch)

    print("  Verifying fix with pytest...")
    result = subprocess.run([".venv/bin/pytest", current["file_path"], "-v"],
                            capture_output=True, text=True)
    if result.returncode == 0:
        status, explanation = "healed", "Fix applied and verified! All tests pass."
        print(f"  {explanation}")
        if state.slack_channel:
            await post_to_slack(state.slack_channel, f"Self-Healing Successful! Test `{current['test_name']}` now passes.")
    else:
        status = "failed_to_heal"
        explanation = f"Fix applied but tests still failing:\n{result.stdout}"
        print("  Verification failed.")
        if state.slack_channel:
            await post_to_slack(state.slack_channel, f"Self-Healing Failed! Test `{current['test_name']}` still failing.")

    return Command(update={"status": status, "explanation": explanation, "applied_fix": current["proposed_fix"]})

# ── Routers ──────────────────────────────────────────────────────────────

def after_generate(state: GraphState):
    if state.status in ("no_test_cases", "no_relevant_cases"):
        return END
    return "tg_fetch_steps"

def after_wait(state: GraphState):
    if state.test_run_status.lower() not in ("failed", "error"):
        return "all_passed"
    return "sh_fetch_failures"

def after_diagnose(state: GraphState):
    if state.status == "no_failures":
        return "all_passed"
    return "sh_slack_notification"

def after_approval(state: GraphState):
    if state.approved:
        return "sh_self_healing"
    return END

# ══════════════════════════════════════════════════════════════════════════
#  Build unified graph
# ══════════════════════════════════════════════════════════════════════════

def _all_passed(state: GraphState) -> Command:
    return Command(update={"status": "all_passed", "explanation": "All tests passed. No fix needed."})

builder = StateGraph(GraphState, input=GraphInput, output=GraphOutput)

# Phase 1 — Test Generation
builder.add_node("tg_fetch_test_cases", tg_fetch_test_cases)
builder.add_node("tg_analyze_and_filter", tg_analyze_and_filter)
builder.add_node("tg_fetch_steps", tg_fetch_steps)
builder.add_node("tg_generate_tests", tg_generate_tests)
builder.add_node("tg_save_results", tg_save_results)

# Phase 2 — Execution
builder.add_node("trigger_test_execution", trigger_test_execution)
builder.add_node("wait_for_execution", wait_for_execution)

# Phase 3 — Self-Healing
builder.add_node("sh_fetch_failures", sh_fetch_failures)
builder.add_node("sh_diagnose_failures", sh_diagnose_failures)
builder.add_node("sh_slack_notification", sh_slack_notification)
builder.add_node("sh_self_healing", sh_self_healing)
builder.add_node("all_passed", _all_passed)

# Edges
builder.add_edge(START, "tg_fetch_test_cases")
builder.add_edge("tg_fetch_test_cases", "tg_analyze_and_filter")
builder.add_conditional_edges("tg_analyze_and_filter", after_generate, {
    "tg_fetch_steps": "tg_fetch_steps", END: END
})
builder.add_edge("tg_fetch_steps", "tg_generate_tests")
builder.add_edge("tg_generate_tests", "tg_save_results")
builder.add_edge("tg_save_results", "trigger_test_execution")
builder.add_edge("trigger_test_execution", "wait_for_execution")
builder.add_conditional_edges("wait_for_execution", after_wait, {
    "sh_fetch_failures": "sh_fetch_failures", "all_passed": "all_passed"
})
builder.add_edge("sh_fetch_failures", "sh_diagnose_failures")
builder.add_conditional_edges("sh_diagnose_failures", after_diagnose, {
    "sh_slack_notification": "sh_slack_notification", "all_passed": "all_passed"
})
builder.add_conditional_edges("sh_slack_notification", after_approval, {
    "sh_self_healing": "sh_self_healing", END: END
})
builder.add_edge("sh_self_healing", END)
builder.add_edge("all_passed", END)

graph = builder.compile()
