#!/usr/bin/env python3
import sys
import subprocess
import json
import argparse

def run_command(cmd):
    """Helper to run a system command and parse JSON output."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error executing command {' '.join(cmd)}:")
        print(e.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error parsing JSON output from command {' '.join(cmd)}:")
        print(result.stdout)
        sys.exit(1)

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
    run_result = run_command(run_cmd)
    
    if run_result.get("Result") != "Success" or not run_result.get("Data"):
        print("Failed to trigger test set run. Output:")
        print(json.dumps(run_result, indent=2))
        sys.exit(1)
        
    execution_id = run_result["Data"].get("Id") or run_result["Data"].get("ExecutionId")
    if not execution_id:
        # Check alternate key structure
        execution_id = run_result["Data"] if isinstance(run_result["Data"], str) else None
        
    if not execution_id:
        print("Could not extract execution ID from run response.")
        sys.exit(1)
        
    print(f"Test run successfully triggered! Execution ID: {execution_id}")

    print(f"\n--- 2. Waiting for Test Execution to Complete... ---")
    wait_cmd = [
        "uip", "tm", "wait",
        "--execution-id", execution_id,
        "--project-key", args.project_key,
        "--timeout", "600",
        "--output", "json"
    ]
    wait_result = run_command(wait_cmd)
    
    status = "Failed"
    if wait_result.get("Result") == "Success" and wait_result.get("Data"):
        status = wait_result["Data"].get("Status", "Failed")
        
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
        else:
            print("Error triggering self-healing agent:")
            print(invoke_result.stderr)
            sys.exit(1)
    else:
        print("\n🟢 All tests passed successfully! No self-healing needed.")

if __name__ == "__main__":
    main()
