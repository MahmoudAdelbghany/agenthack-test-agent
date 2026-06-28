#!/usr/bin/env python3
"""
End-to-End Demo: UiPath Self-Healing Test Agent

Entry point: developer opens a PR → agent runs autonomously until Slack approval.

Flow:
  1. Fetch test cases from UiPath Test Manager
  2. AI detects language + filters relevant cases
  3. AI generates executable test code
  4. Trigger test execution in UiPath Test Cloud
  5. Wait for execution to complete
  6. If failures: diagnose with AI → send Slack with approve/reject buttons
  7. Developer clicks "Yes" → fix is applied + pushed to GitHub
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from main import graph

DEVELOPER_CODE = """
def add(a, b):
    \"\"\"Add two numbers and return the result.\"\"\"
    return a * b

def subtract(a, b):
    \"\"\"Subtract b from a and return the result.\"\"\"
    return a - b
"""


def banner(text):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}\n")


async def run_demo():
    banner("UiPath Self-Healing Test Agent — E2E Demo")

    project_key = os.getenv("UIPATH_PROJECT_KEY", "CALC")
    test_set_key = os.getenv("UIPATH_TEST_SET_KEY", "CALC:80")
    slack_channel = os.getenv("SLACK_CHANNEL", "")

    print(f"  Project Key   : {project_key}")
    print(f"  Test Set Key  : {test_set_key}")
    print(f"  Slack Channel : {slack_channel or '(not set — will use local interrupt)'}")
    print(f"  Developer Code:\n")
    for line in DEVELOPER_CODE.strip().split("\n"):
        print(f"    {line}")
    print()

    initial_input = {
        "developer_code": DEVELOPER_CODE,
        "project_key": project_key,
        "test_set_key": test_set_key,
        "slack_channel": slack_channel or None,
        "branch": os.getenv("GITHUB_BRANCH", "main"),
    }

    print("Launching agent graph...")
    print("(If Slack is configured, check your channel for approval buttons)")
    print("(If not, the agent will pause for local approval via interrupt)\n")

    result = await graph.ainvoke(initial_input)

    banner("AGENT FINISHED")
    print(f"  Status      : {result.get('status', 'unknown')}")
    print(f"  Explanation : {result.get('explanation', 'N/A')}")
    if result.get("applied_fix"):
        print(f"  Applied Fix : yes (pushed to GitHub)")
    print()


if __name__ == "__main__":
    asyncio.run(run_demo())
