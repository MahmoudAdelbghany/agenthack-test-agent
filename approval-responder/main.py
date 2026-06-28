import json as _json
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel, Field
from typing import Optional, Any, Dict, List
from uipath.platform import UiPath


class Input(BaseModel):
    execution_id: Optional[str] = Field(None, description="The TM execution ID (or pass message_text to auto-extract). Map button 'value' or 'actions[0].value' here.")
    message_text: Optional[str] = Field(None, description="Full Slack message text or any string containing EXEC_ID / YES <id>")
    folder_key: Optional[str] = Field(None, description="Optional Orchestrator folder key override")
    raw_payload: Optional[str] = Field(None, description="Full raw Slack event JSON/text for fallback extraction (map the entire event object here if needed)")
    job_key: Optional[str] = Field(None, description="Direct Orchestrator job key (GUID) to resume — skip search if known")
    approved: Optional[bool] = Field(None, description="Override approval value (True/False). If None, defaults to True for button clicks.")
    auto_resume: Optional[bool] = Field(None, description="DEPRECATED - causes infinite loops. Do not use.")
    UiPathEvent: Optional[str] = Field(None, description="Injected by trigger: BUTTON_CLICKED, MESSAGE_RECEIVED, etc.")
    UiPathEventConnector: Optional[str] = Field(None, description="Injected by trigger: the connector key")
    UiPathAdditionalEventData: Optional[str] = Field(None, description="Injected by trigger: raw event data as JSON string")


class Output(BaseModel):
    status: str
    job_key: Optional[str] = None
    inbox_id: Optional[str] = None
    message: str


def _extract_exec_id(text: Any) -> Optional[str]:
    """Aggressively extract a plausible execution ID from any text."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    s_upper = s.upper()

    for marker in ["EXEC_ID:", "EXEC ID:", "EXECUTION_ID:", "EXECUTION ID:"]:
        if marker in s_upper:
            idx = s_upper.find(marker) + len(marker)
            candidate = s[idx:].strip().split()[0].strip('`\'" ,.;')
            if candidate:
                return candidate

    if "YES " in s_upper:
        idx = s_upper.find("YES ") + 4
        candidate = s[idx:].strip().split()[0].strip('`\'" ,.;')
        if candidate:
            return candidate

    cleaned = s.strip('`\'" ,.;')
    if cleaned and " " not in cleaned and len(cleaned) >= 3:
        if cleaned.replace("-", "").replace("_", "").isalnum():
            return cleaned

    for token in s.split():
        t = token.strip('`\'" ,.;')
        if len(t) >= 3 and t.replace("-", "").replace("_", "").isalnum():
            return t
    return None


def _deep_extract(data: Any) -> Optional[str]:
    """Recursively extract a plausible ID from a nested dict/list."""
    if isinstance(data, dict):
        for key in ["execution_id", "value", "action_id", "job_key"]:
            if key in data:
                found = _extract_exec_id(data[key])
                if found:
                    return found
        for v in data.values():
            found = _deep_extract(v)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _deep_extract(item)
            if found:
                return found
    elif isinstance(data, (str, int)):
        return _extract_exec_id(data)
    return None


async def _get_full_job(uip: UiPath, job_id: str, folder_key: str) -> Dict[str, Any]:
    """Fetch full job details via raw API to get InputArguments and InboxId."""
    try:
        resp = await uip.api_client.request_async(
            method="GET",
            url=f"orchestrator_/odata/Jobs('{job_id}')",
            headers={"X-UIPATH-OrganizationUnitId": folder_key} if folder_key else {}
        )
        return resp.json()
    except Exception as ex:
        print(f"[WARN] Failed to fetch full job {job_id}: {ex}")
        return {}


async def main(input: Input) -> Output:
    """Resume the suspended job for the given execution_id from Slack approval."""
    uip = UiPath()

    print(f"[RESOLVER] Triggered. inputs: execution_id={input.execution_id!r}, message_text={input.message_text!r}, "
          f"job_key={input.job_key!r}, approved={input.approved!r}, UiPathEvent={input.UiPathEvent!r}, folder={input.folder_key!r}")

    folder_key = input.folder_key or "5afe02d5-5912-4095-a51e-42a34ae7c290"
    approval_value = True if input.approved is None else input.approved

    # --- Direct job_key shortcut ---
    if input.job_key:
        print(f"[RESOLVER] Direct job_key provided: {input.job_key}")
        try:
            job = await uip.jobs.retrieve_async(input.job_key, folder_key=folder_key)
            full = await _get_full_job(uip, job.id, folder_key)
            inbox_id = full.get("InboxId")
            if inbox_id:
                print(f"[RESOLVER] Resuming via inbox_id: {inbox_id}")
                await uip.jobs.resume_async(inbox_id=inbox_id, payload={"approved": approval_value}, folder_key=folder_key)
                return Output(status="resumed", job_key=input.job_key, inbox_id=inbox_id,
                              message=f"Job {input.job_key} resumed via inbox {inbox_id}")
            else:
                print(f"[RESOLVER] No InboxId found, trying job_id resume")
                await uip.jobs.resume_async(job_id=job.id, payload={"approved": approval_value}, folder_key=folder_key)
                return Output(status="resumed", job_key=input.job_key,
                              message=f"Job {input.job_key} resumed via job_id")
        except Exception as e:
            return Output(status="resume_failed", job_key=input.job_key, message=f"Resume failed: {e}")

    # --- Step 1: Resolve execution_id from inputs ---
    exec_id = _extract_exec_id(input.execution_id) or _extract_exec_id(input.message_text)

    if not exec_id and input.raw_payload:
        try:
            data = _json.loads(input.raw_payload) if isinstance(input.raw_payload, str) else input.raw_payload
            exec_id = _deep_extract(data)
        except Exception as ex:
            print(f"[WARN] raw_payload parse failed: {ex}")
            exec_id = _extract_exec_id(input.raw_payload)

    print(f"[RESOLVER] Resolved execution_id: {exec_id!r}")

    if not exec_id:
        # If triggered by a button click, find the most recent suspended agent job
        if input.UiPathEvent == "BUTTON_CLICKED":
            print("[RESOLVER] Button click detected (no execution_id). Finding most recent suspended agent job (last 30 min)...")
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
            try:
                jobs_page = await uip.jobs.list_async(
                    folder_key=folder_key,
                    filter="State eq 'Suspended'",
                    top=20,
                    orderby="StartTime desc"
                )
                all_jobs = jobs_page.items
                print(f"[RESOLVER] Found {len(all_jobs)} suspended jobs")
            except Exception as e:
                return Output(status="error", message=f"Error listing jobs via SDK: {e}")

            target_job = None
            target_inbox_id = None
            for job in all_jobs:
                start = job.start_time
                try:
                    if isinstance(start, str):
                        start = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    if start and start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                except Exception:
                    start = None
                if start and start < cutoff:
                    print(f"[RESOLVER]   Skipping {job.key} (started {start}, too old)")
                    continue
                full = await _get_full_job(uip, job.id, folder_key)
                input_args = full.get("InputArguments", "") or ""
                inbox_id = full.get("InboxId")
                is_responder = "approval-responder" in input_args
                print(f"[RESOLVER]   Job {job.key} start={start} responder={is_responder} args={input_args[:100]}")
                if not is_responder:
                    target_job = job
                    target_inbox_id = inbox_id
                    print(f"[RESOLVER]   >>> SELECTED {job.key}")
                    break

            if not target_job:
                return Output(status="not_found", message="No recent suspended agent job found (within 3 min).")

            job_key = target_job.key
            try:
                if target_inbox_id:
                    print(f"[RESOLVER] Resuming via inbox_id={target_inbox_id}")
                    await uip.jobs.resume_async(inbox_id=target_inbox_id, payload={"approved": approval_value}, folder_key=folder_key)
                    return Output(status="resumed", job_key=job_key, inbox_id=target_inbox_id,
                                  message=f"Job {job_key} resumed via inbox {target_inbox_id}")
                else:
                    print(f"[RESOLVER] Resuming via job_id={target_job.id}")
                    await uip.jobs.resume_async(job_id=target_job.id, payload={"approved": approval_value}, folder_key=folder_key)
                    return Output(status="resumed", job_key=job_key, message=f"Job {job_key} resumed via job_id")
            except Exception as e:
                return Output(status="resume_failed", job_key=job_key, message=f"Resume failed: {e}")
        else:
            return Output(status="error",
                          message="No execution_id found. Not a button click event either.")

    # --- Step 2: Search suspended jobs by execution_id ---
    print(f"[RESOLVER] Searching for suspended job matching exec_id={exec_id!r} in folder {folder_key}")
    try:
        jobs_page = await uip.jobs.list_async(
            folder_key=folder_key,
            filter="State eq 'Suspended'",
            top=50
        )
        jobs = jobs_page.items
        print(f"[RESOLVER] Found {len(jobs)} suspended jobs")
    except Exception as e:
        return Output(status="error", message=f"Error listing jobs via SDK: {e}")

    # --- Step 4: Match job by execution_id in InputArguments ---
    target_job = None
    target_inbox_id = None
    for job in jobs:
        full = await _get_full_job(uip, job.id, folder_key)
        input_args_str = full.get("InputArguments", "") or ""
        inbox_id = full.get("InboxId")
        print(f"[RESOLVER]   Job {job.key} (id={job.id}, state={job.state}, inbox={inbox_id}) input_args_preview={input_args_str[:200]!r}")

        if exec_id in input_args_str or exec_id == job.key:
            target_job = job
            target_inbox_id = inbox_id
            print(f"[RESOLVER]   >>> MATCHED job {job.key}")
            break

    if not target_job:
        summaries = []
        for job in jobs:
            full = await _get_full_job(uip, job.id, folder_key)
            summaries.append(f"  job_key={job.key}, input_args={full.get('InputArguments', '')[:100]}")
        debug_info = "\n".join(summaries) if summaries else "  (none)"
        return Output(status="not_found",
                      message=f"No suspended job found for exec_id={exec_id!r}.\nSuspended jobs:\n{debug_info}")

    # --- Step 5: Resume the matched job ---
    job_key = target_job.key
    try:
        if target_inbox_id:
            print(f"[RESOLVER] Resuming via inbox_id={target_inbox_id} with payload approved={approval_value}")
            await uip.jobs.resume_async(
                inbox_id=target_inbox_id,
                payload={"approved": approval_value},
                folder_key=folder_key
            )
            return Output(status="resumed", job_key=job_key, inbox_id=target_inbox_id,
                          message=f"Job {job_key} resumed via inbox {target_inbox_id}")
        else:
            print(f"[RESOLVER] No inbox_id, resuming via job_id={target_job.id}")
            await uip.jobs.resume_async(
                job_id=target_job.id,
                payload={"approved": approval_value},
                folder_key=folder_key
            )
            return Output(status="resumed", job_key=job_key,
                          message=f"Job {job_key} resumed via job_id")
    except Exception as e:
        return Output(status="resume_failed", job_key=job_key, message=f"Resume failed: {e}")
