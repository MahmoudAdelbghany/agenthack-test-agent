# Hackathon Demo & Simulation Guide

This guide describes how to run the **Self-Healing Test Agent** simulation for your hackathon submission or demo video.

---

## 1. Local Simulation Script (`simulate.sh`)

We have created an interactive runner script called [simulate.sh](file:///home/abghany/agenthack-test-agent/simulate.sh) which automates the entire loop in your terminal and posts alerts to your Slack workspace.

### How to Run:
1. Open your terminal in the agent project directory `/home/abghany/agenthack-test-agent`.
2. Run the script:
   ```bash
   ./simulate.sh
   ```
3. **Watch the Flow:**
   * **Step 1:** The script resets [test_calculator.py](file:///home/abghany/agenthack-test-agent/tests/test_calculator.py) to a failing state (logical bug).
   * **Step 2:** The agent is launched, runs the tests, extracts the traceback, and invokes **Cloudflare Workers AI** to analyze the failure. It then posts a structured alert message to your Slack channel `new-channel` and pauses.
   * **Step 3:** Open Slack to verify the failure message has been delivered.
   * **Step 4:** Back in the terminal, type `y` to approve the fix.
   * **Step 5:** The agent resumes, writes the fixed code back, verifies it by re-running pytest, and posts a success confirmation message back to Slack.

---

## 2. Real-World CI/CD & Test Cloud Integration

In a real production environment (how you should describe it to the hackathon judges), the architecture maps as follows:

```
[Developer Push]
       │
       ▼
[GitHub Actions / GitLab CI]
       │
       ├── 1. Triggers test runs in Test Cloud via CLI:
       │      uip tm testsets run --test-set-key "PROJECT:12"
       │
       └── 2. On Failure, triggers the Triage Agent Process:
              uip codedagent invoke agent '{"execution_id": "<ID>"}'
```

### Deployed Invoke Command
Now that the agent is successfully deployed to your **Orchestrator Personal Workspace**, it can be invoked remotely from any CI/CD environment using the `uip codedagent invoke` command:
```bash
uip codedagent invoke agent '{"execution_id": "<ID>", "slack_channel": "new-channel"}'
```
This pulls the process execution into Orchestrator, leverages your live Integration Service Slack connection, and halts at the checkpoint waiting for user approval.

---

## 3. Remote CI Orchestration Script (`ci_run.py`)

We have created an automated pipeline orchestrator script: [ci_run.py](file:///home/abghany/agenthack-test-agent/ci_run.py). 

This script is meant to be run inside your remote CI/CD environment (GitHub Actions, GitLab CI, etc.). It automates:
1. Triggering the Test Cloud run via `uip tm testsets run`
2. Polling and waiting for the execution to complete via `uip tm wait`
3. If and only if the tests fail, invoking your deployed cloud agent with the execution ID via `uip codedagent invoke`

### Running the CI Script:
```bash
python ci_run.py \
  --project-key "YOUR_PROJECT_KEY" \
  --test-set-key "YOUR_TEST_SET_KEY" \
  --slack-channel "new-channel"
```

---

## 4. GitHub Actions Pipeline

We have scaffolded a ready-to-use GitHub Actions workflow: [.github/workflows/uipath-test-heal.yml](file:///home/abghany/agenthack-test-agent/.github/workflows/uipath-test-heal.yml).

To run this pipeline in GitHub:
1. Push your repository to GitHub.
2. In your GitHub repository settings, go to **Settings > Secrets and variables > Actions** and add the following secrets:
   * `UIPATH_ORGANIZATION` - Your UiPath Organization Name (`eeytvmnte`)
   * `UIPATH_TENANT` - Your UiPath Tenant Name (`DefaultTenant`)
   * `UIPATH_CLIENT_ID` & `UIPATH_CLIENT_SECRET` - Your API access credentials (created under Admin > External Applications in UiPath Automation Cloud)
   * `UIPATH_PROJECT_KEY` - Your Test Manager project key
   * `UIPATH_TEST_SET_KEY` - Your Test Manager test set key
3. Every push to the `main` branch will automatically execute the tests in the Test Cloud, and self-heal the codebase if they fail!

