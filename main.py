import asyncio
import os
import re
import subprocess
import json
import httpx
from pydantic import BaseModel, Field
from typing import List, Optional
from langgraph.graph import START, StateGraph, END
from langgraph.types import Command, interrupt
from uipath.platform import UiPath
from uipath.platform.connections import ActivityMetadata, ActivityParameterLocationInfo
from uipath.eval.mocks import mockable

@mockable()
def get_user_approval(test_name: str, explanation: str, proposed_fix: str) -> dict:
    return interrupt({
        "prompt": "Review proposed self-healing fix",
        "test_case": test_name,
        "explanation": explanation,
        "proposed_fix": proposed_fix
    })

# Define Slack Send Activity Metadata for UiPath Integration Service
SLACK_SEND_MESSAGE = ActivityMetadata(
    object_path="/send_message_to_channel_v2",
    method_name="POST",
    content_type="application/json",
    parameter_location_info=ActivityParameterLocationInfo(
        query_params=["send_as"],
        body_fields=["channel", "messageToSend", "attachment", "buttons"],
    ),
)

class FailedTestDetail(BaseModel):
    test_name: str
    file_path: str
    line_number: int
    error_message: str
    traceback: str
    code_context: str
    proposed_fix: Optional[str] = None
    explanation: Optional[str] = None

class GraphInput(BaseModel):
    execution_id: str = Field(default="mock", description="The UiPath Test Manager Execution ID, or 'mock' to run local self-healing tests")
    project_key: Optional[str] = Field(default=None, description="The UiPath Test Manager project key")
    slack_channel: Optional[str] = Field(default=None, description="Slack channel to notify")
    repo_path: Optional[str] = Field(default=".", description="Path to the repository to run tests in")

class GraphState(BaseModel):
    execution_id: str
    project_key: Optional[str] = None
    slack_channel: Optional[str] = None
    repo_path: str = "."
    failed_tests: List[FailedTestDetail] = []
    current_test_index: int = 0
    approved: Optional[bool] = None
    status: str = "initialized"
    explanation: str = ""
    applied_fix: Optional[str] = None

class GraphOutput(BaseModel):
    status: str
    explanation: str
    applied_fix: Optional[str] = None

# Cloudflare Workers AI Call Helper
async def call_cloudflare_ai(prompt: str, system_prompt: str = "You are a helpful coding assistant.") -> str:
    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    api_token = os.getenv("CLOUDFLARE_API_TOKEN")
    if not account_id or not api_token:
        from dotenv import load_dotenv
        load_dotenv()
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
        api_token = os.getenv("CLOUDFLARE_API_TOKEN")
        
    if not account_id or not api_token:
        raise ValueError("Missing CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN environment variables.")
        
    model = "@cf/meta/llama-3.1-8b-instruct-fast"
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload, timeout=60.0)
        response.raise_for_status()
        data = response.json()
        if data.get("success"):
            # Llama-3.1 response formatting
            if "response" in data["result"]:
                return data["result"]["response"]
            elif "choices" in data["result"] and len(data["result"]["choices"]) > 0:
                return data["result"]["choices"][0]["message"]["content"]
            else:
                return str(data["result"])
        else:
            raise RuntimeError(f"Cloudflare Workers AI failed: {data.get('errors')}")

async def call_cloudflare_ai_json(prompt: str, system_prompt: str) -> dict:
    raw_response = await call_cloudflare_ai(prompt, system_prompt)
    
    # Strip potential markdown code blocks
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\n", "", cleaned)
        cleaned = re.sub(r"\n```$", "", cleaned)
        cleaned = cleaned.strip()
        
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        # Fallback regex parsing if LLM output has preamble or is slightly malformed
        explanation_match = re.search(r'"explanation"\s*:\s*"([^"]+)"', cleaned)
        fixed_match = re.search(r'"fixed_content"\s*:\s*"(.*)"', cleaned, re.DOTALL)
        
        explanation = explanation_match.group(1) if explanation_match else "Could not parse explanation."
        fixed_content = fixed_match.group(1) if fixed_match else ""
        
        # Unescape quotes
        fixed_content = fixed_content.replace('\\"', '"').replace('\\n', '\n')
        return {
            "explanation": explanation,
            "fixed_content": fixed_content
        }

# Slack Notification Helper via Integration Service
async def post_to_slack(channel: str, message: str, attachment: dict = None) -> bool:
    sdk = UiPath()
    try:
        slack_conn = sdk.connections.retrieve("slack-triage")
        sdk.connections.invoke_activity(
            activity_metadata=SLACK_SEND_MESSAGE,
            connection_id=slack_conn.id,
            activity_input={
                "channel": channel,
                "messageToSend": message,
                "attachment": attachment,
                "send_as": "bot"
            }
        )
        return True
    except Exception as e:
        print(f"[Slack Notification] Warning: failed to post to Slack via Integration Service. Details: {e}")
        return False

# LangGraph Node 1: Fetch Failures
async def fetch_failures(state: GraphState) -> Command:
    print(f"--- Fetching Test Failures for execution_id: {state.execution_id} ---")
    failed_tests = []
    
    # Check if we are running in mock/demo mode or if Test Manager is unavailable
    is_mock = state.execution_id.lower() == "mock"
    
    if not is_mock:
        try:
            # Check Test Manager CLI status and run commands
            # We run the probe first and then try to list executions
            print("Checking Test Manager execution status...")
            cmd = ["uip", "tm", "executions", "testcaselogs", "list", 
                   "--execution-id", state.execution_id, 
                   "--project-key", state.project_key or "",
                   "--only-failed", "--output", "json"]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            log_data = json.loads(result.stdout)
            
            # If logs exist, extract details
            if log_data.get("Result") == "Success" and log_data.get("Data"):
                for log in log_data["Data"]:
                    log_id = log.get("Id")
                    test_case_id = log.get("TestCaseId")
                    test_name = log.get("TestCaseName", "Unknown Test")
                    
                    # Fetch step logs or assertion details
                    assert_cmd = ["uip", "tm", "testcaselog", "list-assertions", 
                                  "--project-key", state.project_key or "",
                                  "--test-case-log-id", log_id, "--output", "json"]
                    assert_res = subprocess.run(assert_cmd, capture_output=True, text=True)
                    assert_data = json.loads(assert_res.stdout) if assert_res.returncode == 0 else {}
                    
                    error_msg = ""
                    traceback = ""
                    if assert_data.get("Result") == "Success" and assert_data.get("Data"):
                        error_msg = assert_data["Data"][0].get("Message", "Assertion failed")
                        traceback = assert_data["Data"][0].get("StackTrace", "")
                    
                    # Try to locate the file in repo
                    file_path = f"tests/test_{test_name.lower().replace(' ', '_')}.py"
                    if not os.path.exists(file_path):
                        file_path = "tests/test_calculator.py"  # Fallback for demo
                        
                    code_context = ""
                    if os.path.exists(file_path):
                        with open(file_path, "r") as f:
                            code_context = f.read()
                            
                    failed_tests.append(FailedTestDetail(
                        test_name=test_name,
                        file_path=file_path,
                        line_number=1,
                        error_message=error_msg,
                        traceback=traceback,
                        code_context=code_context
                    ))
        except Exception as e:
            print(f"[Test Manager] Warning: Could not fetch details from Test Manager ({e}). Falling back to local/mock mode.")
            is_mock = True

    if is_mock or not failed_tests:
        print("Running local mock test suite to capture failures...")
        # Reset the test file to its buggy state to ensure idempotency and testability
        buggy_code = """def add(a, b):
    # Bug: returns multiplication instead of addition
    return a * b

def test_add():
    assert add(2, 3) == 5
"""
        os.makedirs("tests", exist_ok=True)
        with open("tests/test_calculator.py", "w") as f:
            f.write(buggy_code)

        # Run pytest on the local calculator test and capture the failure
        cmd = [".venv/bin/pytest", "--tb=short", "tests/test_calculator.py"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        # Parse output for AssertionError: assert 6 == 5
        output = result.stdout
        print(output)
        
        # Simple parser for the pytest traceback
        error_match = re.search(r"E\s+AssertionError: (.*)", output)
        error_msg = error_match.group(1) if error_match else "Assertion failed"
        
        line_match = re.search(r"tests/test_calculator.py:(\d+): AssertionError", output)
        line_num = int(line_match.group(1)) if line_match else 6
        
        code_context = ""
        file_path = "tests/test_calculator.py"
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                code_context = f.read()
                
        failed_tests.append(FailedTestDetail(
            test_name="test_add",
            file_path=file_path,
            line_number=line_num,
            error_message=error_msg,
            traceback=output,
            code_context=code_context
        ))

    return Command(update={
        "failed_tests": failed_tests,
        "status": "failures_fetched"
    })

# LangGraph Node 2: Diagnose Failures
async def diagnose_failures(state: GraphState) -> Command:
    if not state.failed_tests:
        return Command(update={"status": "no_failures", "explanation": "No failures detected."})
        
    print("--- Diagnosing Failures via Cloudflare Workers AI ---")
    current_test = state.failed_tests[state.current_test_index]
    
    prompt = f"""We have a failing test case in our test suite.
Test File: {current_test.file_path}
Test Name: {current_test.test_name}
Error message: {current_test.error_message}
Traceback:
{current_test.traceback}

Here is the full content of the test file:
```python
{current_test.code_context}
```

Identify the bug, describe the cause, and return the modified file contents to fix the issue.
"""

    system_prompt = """You are a self-healing coding assistant. Analyze the failure and provide a fix.
You MUST respond with a valid JSON object containing exactly two keys:
1. "explanation": a concise string explaining the bug and the suggested fix.
2. "fixed_content": the complete modified file contents (valid Python code) that corrects the bug.
Do not wrap your response in markdown code blocks or add any text outside of the JSON object.
"""

    print(f"Calling Cloudflare AI for test: {current_test.test_name}...")
    diagnosis = await call_cloudflare_ai_json(prompt, system_prompt)
    
    explanation = diagnosis.get("explanation", "Could not analyze the failure.")
    fixed_content = diagnosis.get("fixed_content", current_test.code_context)
    
    current_test.explanation = explanation
    current_test.proposed_fix = fixed_content
    
    print(f"Explanation: {explanation}")
    
    return Command(update={
        "failed_tests": state.failed_tests,
        "explanation": explanation,
        "status": "diagnosed"
    })

# LangGraph Node 3: Slack Notification & Interrupt (HITL)
async def slack_notification(state: GraphState) -> Command:
    current_test = state.failed_tests[state.current_test_index]
    
    msg = f"🚨 *Test Failure Alert*\n" \
          f"*Test Case:* `{current_test.test_name}` in `{current_test.file_path}`\n" \
          f"*Root Cause:* {current_test.explanation}\n\n" \
          f"I suggest applying the fix. Would you like me to do it?"
          
    print("\n=================== SLACK NOTIFICATION PENDING ===================")
    print(msg)
    print("=================================================================\n")
    
    # Attempt to post to Slack via Integration Service if configured
    if state.slack_channel:
        # Construct Slack Block Kit attachments
        blocks = {
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": msg
                    }
                }
            ]
        }
        await post_to_slack(state.slack_channel, "Test Failure Triage Alert", blocks)
        
    # Pause execution for human approval
    decision = get_user_approval(
        current_test.test_name,
        current_test.explanation,
        current_test.proposed_fix
    )
    
    # Receive response from interrupt (local or webhook)
    approved = decision.get("approved", False)
    print(f"Approval received: {approved}")
    
    return Command(update={
        "approved": approved,
        "status": "approval_processed"
    })

# LangGraph Node 4: Self-Healing
async def self_healing(state: GraphState) -> Command:
    if not state.approved:
        print("Self-healing skipped: User rejected or skipped the fix.")
        return Command(update={
            "status": "skipped",
            "explanation": "Self-healing skipped by user."
        })
        
    current_test = state.failed_tests[state.current_test_index]
    print(f"--- Applying Fix for test: {current_test.test_name} ---")
    
    # Write the fixed content to the file
    with open(current_test.file_path, "w") as f:
        f.write(current_test.proposed_fix)
    print(f"Successfully wrote fix to {current_test.file_path}")
    
    # Re-run the tests to verify
    print("Verifying the fix by re-running pytest...")
    cmd = [".venv/bin/pytest", current_test.file_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    status = "failed_to_heal"
    explanation = f"Applied fix, but tests are still failing:\n{result.stdout}"
    
    if result.returncode == 0:
        status = "healed"
        explanation = "🟢 Fix applied successfully and verified! All tests passed."
        print(explanation)
        if state.slack_channel:
            await post_to_slack(state.slack_channel, f"🟢 *Self-Healing Successful!* Test `{current_test.test_name}` is now passing.")
    else:
        print("Verification failed. Tests still failing.")
        if state.slack_channel:
            await post_to_slack(state.slack_channel, f"🔴 *Self-Healing Failed!* Test `{current_test.test_name}` is still failing after applying fix.")
            
    return Command(update={
        "status": status,
        "explanation": explanation,
        "applied_fix": current_test.proposed_fix
    })

# Helper router function
def should_heal(state: GraphState):
    if state.status == "no_failures":
        return END
    return "slack_notification"

def handle_approval(state: GraphState):
    if state.approved:
        return "self_healing"
    return END

# Build StateGraph
builder = StateGraph(GraphState, input=GraphInput, output=GraphOutput)

builder.add_node("fetch_failures", fetch_failures)
builder.add_node("diagnose_failures", diagnose_failures)
builder.add_node("slack_notification", slack_notification)
builder.add_node("self_healing", self_healing)

builder.add_edge(START, "fetch_failures")
builder.add_edge("fetch_failures", "diagnose_failures")
builder.add_conditional_edges(
    "diagnose_failures",
    should_heal,
    {
        "slack_notification": "slack_notification",
        END: END
    }
)
builder.add_conditional_edges(
    "slack_notification",
    handle_approval,
    {
        "self_healing": "self_healing",
        END: END
    }
)
builder.add_edge("self_healing", END)

# Export the CompiledStateGraph
graph = builder.compile()
