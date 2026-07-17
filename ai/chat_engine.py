import os
import json
from google import genai
from google.genai import types

SYSTEM_PROMPT = """You are the TaskFlow AI assistant. You help the user manage a daily \
routine of tasks (habits) that reset each day. You are given the user's message and \
today's task list (with ids). Respond with ONLY a single JSON object — no prose, no \
markdown fences — matching exactly one of these shapes:

1. User wants a new routine created (e.g. "make me a routine for improving my coding \
and studying a new language"):
{"intent": "create_routine", "habits": [{"title": "...", "category": "..."}]}
Break the request into 3-6 concrete, recurring daily habits. Titles should be short \
and actionable (e.g. "Solve 2 DSA problems", "30 min Japanese vocab").

2. User says they completed one or more of today's tasks (e.g. "I finished the DSA \
and reading tasks"):
{"intent": "complete_tasks", "task_ids": [1, 2]}
Match against today's task list by content. Only include ids you are confident about.

3. User asks what's left / how they're doing:
{"intent": "status_query"}

4. The request is genuinely ambiguous (e.g. multiple tasks could match, unclear which \
one they mean):
{"intent": "clarify", "question": "..."}

5. Anything else (small talk, a question you can just answer, encouragement):
{"intent": "chat", "reply": "..."}

Never invent task ids that aren't in the provided list. If no tasks match a completion \
claim, use "clarify" and ask which task they mean rather than guessing.
"""

_client = None


def get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY'))
    return _client


def classify_message(user_message, today_tasks):
    """
    today_tasks: list of dicts like {'id': 1, 'content': 'Solve 2 DSA problems', 'completed': False}
    Returns a parsed dict per the shapes documented in SYSTEM_PROMPT.
    """
    client = get_client()
    model = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')

    if today_tasks:
        task_lines = "\n".join(
            f"- id={t['id']} [{'done' if t['completed'] else 'pending'}] {t['content']}"
            for t in today_tasks
        )
    else:
        task_lines = "(no tasks yet today)"

    prompt = f"Today's tasks:\n{task_lines}\n\nUser message: {user_message}"

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )

    return json.loads(response.text)
