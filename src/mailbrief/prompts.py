"""Prompt templates. Output language: Korean. The JSON schema is small for smaller in-house models.

Category labels and rules come from config (analysis.categories), so the team can change the
classification without code changes.
"""
from __future__ import annotations

PROMPT_VERSION = "v2"  # change this when the prompt changes: cached results of other versions are ignored

_EXTRACT_SYSTEM = """You analyze ONE work email for the mailbox owner ({me}).
Output exactly ONE JSON object and nothing else. No markdown, no comments, no explanation.

Keys:
- "summary": Korean, 1-2 sentences. Who wants what from whom, and by when.
- "category": one of {labels}
- "priority": 1, 2 or 3. 1 = deadline within 2 days, or from a manager/customer, or explicit urgency. 3 = no action.
- "needs_reply": true or false. true only if the sender expects a reply from the owner.
- "action_items": list of {{"description": Korean short imperative, "due": ISO 8601 or null, "owner": "me"|"other"|"unknown"}}
- "events": list of {{"title": string, "start": ISO 8601, "end": ISO 8601 or null, "location": string}}

Category rules (choose exactly one label):
{rules}

Date rules:
- Resolve relative dates ("내일", "이번 주 금요일", "next Monday", "EOD") from the RECEIVED time. Timezone {tz}.
- Format: "2026-11-18T18:00:00+09:00". If there is a date but no time for a due date, use 18:00.
- If the date is not clear, use null. Do not guess.
- Requests addressed to the owner in To default to owner "me".

Example output:
{{"summary": "김팀장이 Q4 리포트를 11/18 18시까지 요청.", "category": "{first}", "priority": 1, "needs_reply": true, "action_items": [{{"description": "Q4 리포트 제출", "due": "2026-11-18T18:00:00+09:00", "owner": "me"}}], "events": []}}
"""

EXTRACT_USER = """RECEIVED: {received}
FROM: {sender}
TO: {to}
CC: {cc}
SUBJECT: {subject}
IMPORTANCE: {importance}
ATTACHMENTS: {attachments}

BODY:
{body}
"""


def extract_system(categories: dict[str, str], me: str, tz: str) -> str:
    labels = " | ".join(f'"{k}"' for k in categories)
    rules = "\n".join(f"- {k}: {v}" for k, v in categories.items())
    return _EXTRACT_SYSTEM.format(me=me or "the owner", labels=labels, rules=rules, tz=tz,
                                  first=next(iter(categories)))


BRIEF_SYSTEM = """You write a short morning briefing in Korean for a busy engineer.
Input is a JSON list of facts. Output plain text: at most 3 bullet lines, each starting with "- ".
State the most important things to do today first. Use only the given facts. No greeting."""
