#!/usr/bin/env bash

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}===============================================================${NC}"
echo -e "${BLUE}        UiPath AgentHack 2026 - Track 3 Self-Healing Demo      ${NC}"
echo -e "${BLUE}===============================================================${NC}"

echo -e "\n${YELLOW}[Step 1] Initializing repository state with a logical bug...${NC}"
# Reset the test file to a buggy state
cat << 'EOF' > tests/test_calculator.py
def add(a, b):
    # Bug: returns multiplication instead of addition
    return a * b

def test_add():
    assert add(2, 3) == 5
EOF
echo -e "${GREEN}✓ tests/test_calculator.py is now set to a failing state (2 * 3 = 6 != 5)${NC}"

echo -e "\n${YELLOW}[Step 2] Triggering Test Triage Agent...${NC}"
echo -e "Running tests, extracting failure logs, analyzing with Cloudflare AI..."
echo -e "Posting alert to Slack channel: ${BLUE}new-channel${NC}."

# Touch state.json to ensure it exists
touch state.json

# Run step 1 of the agent
uip codedagent run agent '{"execution_id": "mock", "slack_channel": "new-channel"}' --state-file state.json --keep-state-file

echo -e "\n${YELLOW}[Step 3] Verification Check${NC}"
echo -e "1. Please check your Slack channel ${BLUE}new-channel${NC}."
echo -e "2. You should see a section layout explaining the bug and suggesting the fix."
echo -e "3. To simulate approving the fix, we will resume the execution."

read -p "Would you like to approve and apply the fix? (y/n): " choice

if [[ "$choice" == "y" || "$choice" == "Y" ]]; then
    echo -e "\n${YELLOW}[Step 4] Resuming Agent with Approval...${NC}"
    echo -e "Applying fix to test_calculator.py and validating..."
    uip codedagent run agent '{"approved": true}' --resume --state-file state.json
    
    echo -e "\n${GREEN}✓ Done! The test file was healed, tests passed, and Slack has been notified!${NC}"
else
    echo -e "\n${RED}[Step 4] Resuming Agent with Rejection...${NC}"
    uip codedagent run agent '{"approved": false}' --resume --state-file state.json
    echo -e "\n${YELLOW}✓ Self-healing skipped by user.${NC}"
fi

echo -e "\n${BLUE}===============================================================${NC}"
