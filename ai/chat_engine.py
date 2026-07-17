import os
import json
import re
from google import genai
from google.genai import types

SYSTEM_PROMPT = """You are the TaskFlow AI assistant. You help the user manage a daily \
routine of tasks (habits) that reset each day. You are given the recent conversation \
history, today's task list (with ids), and the user's latest message. Use the \
conversation history to stay on track — if you previously asked a question and the \
user answered it (e.g. "yes", "coding"), treat that as building toward the goal, not \
a brand new unrelated message. Don't repeat a question you already asked; move the \
conversation forward.

Respond with ONLY a single JSON object — no prose, no markdown fences — matching \
exactly one of these shapes:

1. You now have enough information (from this message and/or the recent history) to \
create a routine:
{"intent": "create_routine", "habits": [{"title": "...", "category": "..."}]}
Break it into 3-6 concrete, recurring daily habits. Titles should be short and \
actionable (e.g. "Solve 2 DSA problems", "30 min Japanese vocab"). If the user has \
only named one topic so far (e.g. just "coding"), you may still create a reasonable \
routine for that one topic rather than asking again — bias toward acting once the \
user has confirmed intent (e.g. said "yes" or "daily habit") and named at least one \
area, rather than repeatedly re-asking.

2. User says they completed one or more of today's tasks:
{"intent": "complete_tasks", "task_ids": [1, 2]}
Match against today's task list by content. Only include ids you are confident about.

3. User asks what's left / how they're doing:
{"intent": "status_query"}

4. Genuinely ambiguous AND you haven't already asked a similar question in the recent \
history:
{"intent": "clarify", "question": "..."}

5. Anything else (small talk, a question you can just answer, encouragement):
{"intent": "chat", "reply": "..."}

Never invent task ids that aren't in the provided list.
"""

_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))
    return _client


def _extract_json(text):
    """Best-effort JSON extraction — handles stray markdown fences or whitespace
    around an otherwise valid JSON object."""
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
        text = re.sub(r'```$', '', text).strip()
    return json.loads(text)


def classify_message(user_message, today_tasks, history=None):
    """
    today_tasks: list of dicts like {'id': 1, 'content': 'Solve 2 DSA problems', 'completed': False}
    history: list of dicts like {'role': 'user'|'assistant', 'content': '...'}, oldest first,
             not including the current user_message.
    Returns a parsed dict per the shapes documented in SYSTEM_PROMPT. Falls back to a
    plain 'chat' intent if the model's output can't be parsed, rather than raising.
    """
    client = get_client()
    model = os.environ.get('GEMINI_MODEL', 'gemini-flash-latest')

    if today_tasks:
        task_lines = "\n".join(
            f"- id={t['id']} [{'done' if t['completed'] else 'pending'}] {t['content']}"
            for t in today_tasks
        )
    else:
        task_lines = "(no tasks yet today)"

    if history:
        history_lines = "\n".join(
            f"{'User' if h['role'] == 'user' else 'Assistant'}: {h['content']}" for h in history
        )
    else:
        history_lines = "(no prior messages)"

    prompt = (
        f"Recent conversation:\n{history_lines}\n\n"
        f"Today's tasks:\n{task_lines}\n\n"
        f"User's latest message: {user_message}"
    )

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )

    raw_text = response.text or ''
    try:
        return _extract_json(raw_text)
    except (json.JSONDecodeError, ValueError):
        print(f"[ChatEngine] Non-JSON response from model: {raw_text[:300]!r}")
        return {'intent': 'chat', 'reply': "Could you rephrase that? I didn't quite catch it."}
