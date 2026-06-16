#!/usr/bin/env python3
import sys
import subprocess
import json
import argparse

def run_command(cmd):
    """Helper to run a system command and parse JSON output."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Command {' '.join(cmd)} failed with exit code {result.returncode}:")
        print(result.stderr)
        raise RuntimeError(result.stderr or f"Exit code {result.returncode}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON output from command {' '.join(cmd)}:")
        print(result.stdout)
        raise e

def main():
    parser = argparse.ArgumentParser(description="UiPath CI/CD Test Cloud & Self-Healing Orchestrator")
    parser.add_argument("--project-key", required=True, help="UiPath Test Manager Project Key (e.g. DEMO)")
    parser.add_argument("--test-set-key", required=True, help="Test Set Key to run (e.g. DEMO:12)")
    parser.add_argument("--slack-channel", required=True, help="Slack channel to send alerts to")
    args = parser.parse_args()

    print(f"--- 1. Triggering Test Cloud Run for Set: {args.test_set_key} ---")
    run_cmd = [
        "uip", "tm", "testsets", "run",
        "--test-set-key", args.test_set_key,
        "--output", "json"
    ]
    run_result = None
    try:
        run_result = run_command(run_cmd)
    except Exception as e:
        print(f"Warning: Failed to execute run command: {e}")
        
    execution_id = None
    if run_result and run_result.get("Result") == "Success" and run_result.get("Data"):
        execution_id = run_result["Data"].get("Id") or run_result["Data"].get("ExecutionId")
        if not execution_id and isinstance(run_result["Data"], str):
            execution_id = run_result["Data"]
            
    if not execution_id:
        print("⚠️ Warning: Could not trigger test set run or extract execution ID. Falling back to mock execution ID for self-healing demo.")
        execution_id = "mock"
    else:
        print(f"Test run successfully triggered! Execution ID: {execution_id}")

    status = "Failed"
    if execution_id == "mock":
        print("Demo Mode: Skipping wait for mock execution, forcing self-healing trigger.")
    else:
        print(f"\n--- 2. Waiting for Test Execution to Complete... ---")
        wait_cmd = [
            "uip", "tm", "wait",
            "--execution-id", execution_id,
            "--project-key", args.project_key,
            "--timeout", "600",
            "--output", "json"
        ]
        try:
            wait_result = run_command(wait_cmd)
            if wait_result.get("Result") == "Success" and wait_result.get("Data"):
                status = wait_result["Data"].get("Status", "Failed")
        except Exception as e:
            print(f"Error waiting for execution: {e}. Defaulting to Failed to trigger triage.")
            
        print(f"Test Run Finished. Status: {status}")


    # If the tests failed, trigger the Self-Healing Coded Agent in the cloud
    if status.lower() in ["failed", "error"]:
        print(f"\n🚨 Tests failed! Triggering Self-Healing Coded Agent on the cloud... 🚨")
        
        # Prepare input parameters for the deployed agent
        agent_input = {
            "execution_id": execution_id,
            "project_key": args.project_key,
            "slack_channel": args.slack_channel
        }
        
        invoke_cmd = [
            "uip", "codedagent", "invoke", "agent",
            json.dumps(agent_input)
        ]
        
        print(f"Invoking deployed agent with input: {json.dumps(agent_input)}")
        invoke_result = subprocess.run(invoke_cmd, capture_output=True, text=True)
        
        if invoke_result.returncode == 0:
            print("Self-healing agent successfully triggered in Orchestrator!")
            print("Check your Slack channel for failure alerts and the interactive approval buttons.")
            print(invoke_result.stdout)
        else:
            print("Error triggering self-healing agent:")
            print("STDOUT:", invoke_result.stdout)
            print("STDERR:", invoke_result.stderr)
            sys.exit(1)
    else:
        print("\n🟢 All tests passed successfully! No self-healing needed.")

if __name__ == "__main__":
    main()
