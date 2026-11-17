"""Prompt templates. Output language: Korean. The JSON schema is small for smaller in-house models.

One prompt = one thread (all mails of one conversation). Category and meeting type labels come from config
(analysis.categories, analysis.meeting_types), so the team can change them without code changes.

The LLM extracts facts only. The story decision (story / review / not a story) is made by rules in
stories.judge, from the "story" facts below.
"""
from __future__ import annotations

PROMPT_VERSION = "v3"  # change this when the prompt changes: cached results of other versions are ignored

_EXTRACT_SYSTEM = """You analyze ONE work email thread for the mailbox owner ({me}).
The messages are numbered [1], [2], ... oldest first. A message marked (owner) was written by the owner.
Output exactly ONE JSON object and nothing else. No markdown, no comments, no explanation.

Keys:
- "summary": Korean, 1-2 sentences. The current state of the thread: who wants what from whom, by when, what is still open.
- "category": one of {labels}
- "priority": 1, 2 or 3. 1 = deadline within 2 days, or from a manager/customer, or explicit urgency. 3 = no action.
- "needs_reply": true or false. true only if the LATEST message expects a reply from the owner.
- "action_items": list of {{"description": Korean short imperative, "due": ISO 8601 or null, "owner": "me"|"other"|"unknown", "msg": message number}}. Only items that are still open after the latest message.
- "events": list of {{"title": string, "start": ISO 8601, "end": ISO 8601 or null, "location": string}}
- "meeting_type": one of {meeting_labels} or null
- "story": {{"persistence": true|false, "owner": string or null, "end_condition": Korean sentence or null, "title": Korean short noun phrase, "goal": Korean 1 sentence, "confidence": "high"|"medium"|"low", "done": true|false, "match": open story id or null}}

Category rules (choose exactly one label):
{rules}

Meeting type rules (null if the thread is not about a meeting):
{meeting_rules}
- Decide from the body, the invite (MEETING REQUEST), the attendees and the agenda. Do not decide from the subject only.

Story rules:
- A story is ONE goal that needs tracking over days or weeks (a project, an issue, an approval process). A story is not one mail.
- "persistence": true only if the work continues after this thread: more steps, waiting for other people, several days.
- A one-off task (reply once, attend one training, submit one form, read a notice) is NOT a story. Put it in action_items and set persistence false.
- Newsletters, automatic notifications, system alerts, notices, training and event announcements are NOT stories.
- "owner": the person or team that drives the goal, as written in the thread ("me" if it is the mailbox owner). null if it is not written.
- "end_condition": one sentence that says when the goal is done (e.g. "고객이 최종 보고서를 승인하면 종료"). null if you cannot write it from the thread.
- "confidence": "high" only if persistence, owner and end_condition are all clearly in the thread.
- "done": true only if the latest message clearly says that the end condition is met.
- "match": the id of one OPEN STORY (listed after the thread) with the same goal. null if no open story has the same goal.
- If you are not sure, use false, null or "low". Do not guess.

Date rules:
- Resolve relative dates ("내일", "이번 주 금요일", "next Monday", "EOD") from the RECEIVED time of that message. Timezone {tz}.
- Format: "2026-11-18T18:00:00+09:00". If there is a date but no time for a due date, use 18:00.
- If the date is not clear, use null. Do not guess.
- Requests addressed to the owner in To default to owner "me".

Example output:
{{"summary": "김팀장이 Q4 리포트를 11/18 18시까지 요청.", "category": "{first}", "priority": 1, "needs_reply": true, "action_items": [{{"description": "Q4 리포트 제출", "due": "2026-11-18T18:00:00+09:00", "owner": "me", "msg": 1}}], "events": [], "meeting_type": null, "story": {{"persistence": false, "owner": null, "end_condition": null, "title": "Q4 리포트 제출", "goal": "Q4 리포트를 제출한다", "confidence": "low", "done": false, "match": null}}}}
"""

THREAD_HEAD = "THREAD: {shown} of {total} message(s), oldest first.{omitted}\n"

MESSAGE = """
[{n}] RECEIVED: {received}
FROM: {sender}{mine}
TO: {to}
CC: {cc}
SUBJECT: {subject}
IMPORTANCE: {importance}
ATTACHMENTS: {attachments}
MEETING REQUEST: {meeting}

BODY:
{body}
"""

OPEN_STORIES_HEAD = "\nOPEN STORIES (for \"match\"):\n"


def extract_system(categories: dict[str, str], me: str, tz: str,
                   meeting_types: dict[str, str] | None = None) -> str:
    meeting_types = meeting_types or {"other": "회의"}
    labels = " | ".join(f'"{k}"' for k in categories)
    rules = "\n".join(f"- {k}: {v}" for k, v in categories.items())
    mlabels = " | ".join(f'"{k}"' for k in meeting_types)
    mrules = "\n".join(f"- {k}: {v}" for k, v in meeting_types.items())
    return _EXTRACT_SYSTEM.format(me=me or "the owner", labels=labels, rules=rules, tz=tz,
                                  meeting_labels=mlabels, meeting_rules=mrules, first=next(iter(categories)))


BRIEF_SYSTEM = """You write a short morning briefing in Korean for a busy engineer.
Input is a JSON list of facts. Output plain text: at most 3 bullet lines, each starting with "- ".
State the most important things to do today first. Use only the given facts. No greeting."""
