#!/usr/bin/env python3
"""
Setup script: creates UiPath Test Manager project, test cases with steps,
test set, and updates .env — all via uip CLI.
"""

import subprocess
import json
import os
import sys

def run_cli(args: list[str]) -> dict:
    cmd = ["uip"] + args
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  WARNING: non-JSON output: {result.stdout[:200]}")
        return {}


def main():
    PROJECT_KEY = "CALC"

    print("=" * 60)
    print("  UiPath Test Manager Setup for Demo")
    print("=" * 60)

    # ── 1. Check if project exists, create if not ──
    print("\n[1] Checking project...")
    existing = run_cli(["tm", "project", "list", "--output", "json"])
    projects = existing.get("Data", [])
    calc_proj = [p for p in projects if p.get("ProjectKey") == PROJECT_KEY]

    if not calc_proj:
        print("  Creating project CALC...")
        run_cli(["tm", "project", "create",
                 "--name", "Calculator Demo",
                 "--project-key", PROJECT_KEY,
                 "--description", "Hackathon demo: calculator test generation + self-healing"])
    else:
        print(f"  Project {PROJECT_KEY} already exists.")

    # ── 2. Create test cases ──
    print("\n[2] Creating test cases...")

    test_cases = [
        {
            "name": "Add two positive integers",
            "description": (
                "Test that add(a, b) returns the correct sum of two positive integers.\n"
                "Steps:\n"
                "1. Call add(2, 3)\n"
                "2. Assert result equals 5\n"
                "3. Call add(10, 20)\n"
                "4. Assert result equals 30"
            ),
        },
        {
            "name": "Add with negative numbers",
            "description": (
                "Test that add(a, b) handles negative numbers correctly.\n"
                "Steps:\n"
                "1. Call add(-2, 3)\n"
                "2. Assert result equals 1\n"
                "3. Call add(-5, -3)\n"
                "4. Assert result equals -8"
            ),
        },
        {
            "name": "Subtract two integers",
            "description": (
                "Test that subtract(a, b) returns a minus b.\n"
                "Steps:\n"
                "1. Call subtract(5, 3)\n"
                "2. Assert result equals 2\n"
                "3. Call subtract(10, 20)\n"
                "4. Assert result equals -10"
            ),
        },
        {
            "name": "Add with zero",
            "description": (
                "Test that add(a, b) works when one operand is zero.\n"
                "Steps:\n"
                "1. Call add(0, 5)\n"
                "2. Assert result equals 5\n"
                "3. Call add(7, 0)\n"
                "4. Assert result equals 7"
            ),
        },
    ]

    created_keys = []
    for tc in test_cases:
        data = run_cli([
            "tm", "testcases", "create",
            "--project-key", PROJECT_KEY,
            "--name", tc["name"],
            "--description", tc["description"],
            "--output", "json"
        ])
        key = data.get("Data", {}).get("TestCaseKey") or data.get("Data", {}).get("ObjKey", "")
        if key:
            created_keys.append(key)
            print(f"  Created: {key} — {tc['name']}")
        else:
            print(f"  WARNING: Could not get key for '{tc['name']}'")

    # Also keep the existing test_calculator_add if present
    all_cases = run_cli(["tm", "testcases", "list", "--project-key", PROJECT_KEY, "--output", "json"])
    for tc in all_cases.get("Data", []):
        k = tc.get("ObjKey", "")
        if k and k not in created_keys:
            created_keys.append(k)
            print(f"  Found existing: {k} — {tc.get('Name')}")

    print(f"\n  Total test case keys: {created_keys}")

    # ── 3. Create test set ──
    print("\n[3] Creating test set...")
    ts_data = run_cli([
        "tm", "testsets", "create",
        "--project-key", PROJECT_KEY,
        "--name", "Calculator Full Suite",
        "--description", "All calculator tests for hackathon demo",
        "--output", "json"
    ])
    test_set_key = ts_data.get("Data", {}).get("TestSetKey") or ts_data.get("Data", {}).get("ObjKey", "")

    if not test_set_key:
        existing_sets = run_cli(["tm", "testsets", "list", "--project-key", PROJECT_KEY, "--output", "json"])
        sets = existing_sets.get("Data", [])
        if sets:
            test_set_key = sets[0].get("TestSetKey", "")
            print(f"  Using existing test set: {test_set_key}")
        else:
            print("  FATAL: Could not create or find a test set")
            sys.exit(1)
    else:
        print(f"  Created test set: {test_set_key}")

    # ── 4. Add test cases to test set ──
    print("\n[4] Adding test cases to test set...")
    keys_csv = ",".join(created_keys)
    run_cli([
        "tm", "testcases", "add",
        "--test-set-key", test_set_key,
        "--test-case-keys", keys_csv,
        "--output", "json"
    ])
    print(f"  Added {len(created_keys)} test cases to {test_set_key}")

    # ── 5. Update .env ──
    print("\n[5] Updating .env...")
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    env_lines = []
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            env_lines = f.readlines()

    updates = {
        "UIPATH_PROJECT_KEY": PROJECT_KEY,
        "UIPATH_TEST_SET_KEY": test_set_key,
    }

    for key, value in updates.items():
        found = False
        for i, line in enumerate(env_lines):
            if line.startswith(f"{key}="):
                env_lines[i] = f"{key}={value}\n"
                found = True
                break
        if not found:
            env_lines.append(f"{key}={value}\n")

    with open(env_path, "w") as f:
        f.writelines(env_lines)

    print(f"  Set UIPATH_PROJECT_KEY={PROJECT_KEY}")
    print(f"  Set UIPATH_TEST_SET_KEY={test_set_key}")

    # ── 6. Verify ──
    print("\n[6] Verification...")
    final_cases = run_cli(["tm", "testcases", "list", "--project-key", PROJECT_KEY, "--output", "json"])
    case_count = len(final_cases.get("Data", []))
    print(f"  Test cases in project: {case_count}")

    final_sets = run_cli(["tm", "testsets", "list", "--project-key", PROJECT_KEY, "--output", "json"])
    set_count = len(final_sets.get("Data", []))
    print(f"  Test sets in project: {set_count}")

    print("\n" + "=" * 60)
    print("  SETUP COMPLETE")
    print("=" * 60)
    print(f"\n  Project Key   : {PROJECT_KEY}")
    print(f"  Test Set Key  : {test_set_key}")
    print(f"  Test Cases    : {case_count}")
    print(f"\n  .env updated with:")
    print(f"    UIPATH_PROJECT_KEY={PROJECT_KEY}")
    print(f"    UIPATH_TEST_SET_KEY={test_set_key}")
    print(f"\n  Run the demo:")
    print(f"    .venv/bin/python demo.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
