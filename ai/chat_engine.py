import os
import json
import re
from google import genai
from google.genai import types

SYSTEM_PROMPT = """You are the TaskFlow AI assistant. You help the user manage their daily \
tasks. You are given the recent conversation history, today's task list (with ids), and \
the user's latest message. Use the conversation history to stay on track.

Important: "resets each day" refers ONLY to the completed/done checkmark — every task's \
`completed` status clears at midnight so the user can redo their routine fresh, but the \
task itself is permanent and stays in their list forever until the user (or you, on their \
behalf) explicitly deletes it. Never tell the user tasks are automatically cleared, removed, \
or that they don't need to delete old ones — that's false. Tasks can and do accumulate \
unless deleted, and you are able to delete them when asked.

Respond with ONLY a single JSON object — no prose, no markdown fences — matching \
exactly one of these shapes:

1. You now have enough information to create/add one or more daily tasks:
{"intent": "create_routine", "habits": [{"title": "...", "category": "...", "priority": "high"|"medium"|"low"}]}
Break it into concrete, recurring daily tasks. Titles should be short and \
actionable (e.g. "Solve 2 DSA problems", "Read a book chapter"). Default priority to \
"medium" unless the user specifies otherwise (e.g. "make them high priority"). If the \
user says "add them"/"add those"/"add it" referring to tasks you or they mentioned \
earlier in the conversation history, use those specific tasks — never create a task \
literally titled after the pronoun itself.

2. User says they completed one or more of today's tasks:
{"intent": "complete_tasks", "task_ids": [1, 2]}
Match against today's task list by content. Only include ids you are confident about.

3. User asks what's left / how they're doing:
{"intent": "status_query"}

4. User wants one or more tasks permanently deleted/removed from their list:
{"intent": "delete_tasks", "task_ids": [1, 2]}
Match against today's task list by content. Only include ids you are confident about. \
This is a real, permanent deletion — not the daily completion reset.
CRITICAL: if the user says "them"/"those"/"that"/"it", resolve the pronoun ONLY to the \
specific task(s) named in the immediately preceding assistant message in the conversation \
history — never to the user's full task list. Deleting is destructive and irreversible-\
feeling, so when in doubt, delete FEWER tasks, not more — if you are not confident exactly \
which tasks a pronoun refers to, use "clarify" instead of guessing. Only include ALL of the \
user's tasks in task_ids if they explicitly say something like "all", "everything", or \
"clear my whole list".

5. Genuinely ambiguous AND you haven't already asked a similar question in the recent \
history:
{"intent": "clarify", "question": "..."}

6. Anything else (prioritizing tasks, small talk, questions, encouragement):
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
    today_tasks: list of dicts like {'id': 1, 'content': 'Solve 2 DSA problems', 'completed': False, 'priority': 'medium'}
    history: list of dicts like {'role': 'user'|'assistant', 'content': '...'}, oldest first,
             not including the current user_message.
    Returns a parsed dict per the shapes documented in SYSTEM_PROMPT. Falls back to local
    rule-based matching or a plain 'chat' intent if Gemini is unavailable or errors.
    """
    # ── LOCAL RULE-BASED FALLBACK & SAFETY NET ────────────────────────────────
    msg_lower = user_message.lower().strip()

    # Words/phrases that signal a vague or conversational request — these should
    # be forwarded to the LLM so it can generate specific, actionable tasks
    # instead of creating a single task with the literal text.
    _VAGUE_SIGNALS = re.compile(
        r'\b(?:some|a few|a couple|several|many|tasks|habits|routine|routines'
        r'|to help|to improve|to boost|to increase|for me|for my|that|which'
        r'|about|around|related to|based on|suggest|recommend|ideas|tips)\b'
    )

    # Bare pronouns/references that need conversation context to resolve —
    # never safe to match locally by content; let these fall through to the
    # LLM (with full history) instead of risking a false match or, worse,
    # literally creating a task titled after the pronoun itself.
    _PRONOUNS = {'them', 'those', 'that', 'it', 'this', 'all', 'everything'}

    # 1. Add task / habit
    add_match = re.search(r'(?:add|create|new)\s+(?:a\s+)?(?:habit|task)\s+(?:named\s+)?["\']?([^"\']+)["\']?', msg_lower)
    if not add_match:
        # Match e.g. "add football", "create reading"
        add_match = re.search(r'^(?:add|create)\s+["\']?([^"\']+)["\']?$', msg_lower)

    if add_match:
        captured = add_match.group(1).strip()
        # If the captured text looks vague/conversational, or is a bare pronoun
        # with no real content of its own, skip the local shortcut and let the
        # LLM interpret the user's real intent using conversation history.
        if not _VAGUE_SIGNALS.search(captured) and captured not in _PRONOUNS:
            habit_title = captured.capitalize()
            habit_title = re.sub(r'[.!?]+$', '', habit_title)
            if habit_title:
                return {
                    'intent': 'create_routine',
                    'habits': [{'title': habit_title, 'category': 'general'}]
                }

    # 2. Complete task
    complete_match = re.search(r'(?:complete|done\s+with|finished|mark\s+done|check\s+off)\s+(.+)', msg_lower)
    if complete_match:
        target = complete_match.group(1).strip()
        target = re.sub(r'[.!?]+$', '', target)
        if target not in _PRONOUNS:
            matched_ids = []
            for t in today_tasks:
                if target == str(t['id']) or f"task {t['id']}" in target:
                    matched_ids.append(t['id'])
                    break
                if target in t['content'].lower() or t['content'].lower() in target:
                    matched_ids.append(t['id'])
            if matched_ids:
                return {
                    'intent': 'complete_tasks',
                    'task_ids': matched_ids
                }

    # 2b. Delete task
    delete_match = re.search(r'(?:delete|remove|get\s+rid\s+of)\s+(?:the\s+)?(?:task\s+)?(.+)', msg_lower)
    if delete_match:
        target = delete_match.group(1).strip()
        target = re.sub(r'[.!?]+$', '', target)
        if target not in _PRONOUNS:
            matched_ids = []
            for t in today_tasks:
                if target == str(t['id']) or f"task {t['id']}" in target:
                    matched_ids.append(t['id'])
                    break
                if target in t['content'].lower() or t['content'].lower() in target:
                    matched_ids.append(t['id'])
            if matched_ids:
                return {
                    'intent': 'delete_tasks',
                    'task_ids': matched_ids
                }

    # 3. Status query
    if any(phrase in msg_lower for phrase in ['status', "what's left", 'show tasks', 'how am i doing', 'list tasks']):
        return {
            'intent': 'status_query'
        }

    # 4. Prioritize tasks — only for an actual "prioritize my tasks" style request,
    # not any message that merely mentions the word "priority" (e.g. "make it high
    # priority" while adding a task, which was being hijacked by a bare substring
    # check before it ever reached the add-task logic or the LLM).
    is_add_or_delete_command = bool(re.match(r'^\s*(add|create|new|delete|remove)\b', msg_lower))
    prioritize_intent = re.search(r'prioriti[sz]e|priority\s+(?:list|order)|what should i (?:work on|focus on|do)\s+first', msg_lower)
    if prioritize_intent and not is_add_or_delete_command:
        if today_tasks:
            high_tasks = [t['content'] for t in today_tasks if t.get('priority') == 'high' and not t['completed']]
            med_tasks = [t['content'] for t in today_tasks if t.get('priority') == 'medium' and not t['completed']]
            low_tasks = [t['content'] for t in today_tasks if t.get('priority') == 'low' and not t['completed']]
            completed_tasks = [t['content'] for t in today_tasks if t['completed']]
            
            reply_lines = ["Here is my recommendation for prioritizing your daily tasks:\n"]
            if high_tasks:
                reply_lines.append("🔴 **High Priority (Do These First):**")
                for t in high_tasks:
                    reply_lines.append(f"  - {t}")
                reply_lines.append("")
            if med_tasks:
                reply_lines.append("🟡 **Medium Priority (Next up):**")
                for t in med_tasks:
                    reply_lines.append(f"  - {t}")
                reply_lines.append("")
            if low_tasks:
                reply_lines.append("🟢 **Low Priority (If time permits):**")
                for t in low_tasks:
                    reply_lines.append(f"  - {t}")
                reply_lines.append("")
            if completed_tasks:
                reply_lines.append("✅ **Completed Today:**")
                for t in completed_tasks:
                    reply_lines.append(f"  - {t}")
                reply_lines.append("")
                
            if not (high_tasks or med_tasks or low_tasks):
                reply_lines = ["You have no pending tasks left for today! Great job! 🎉"]
            
            return {
                'intent': 'chat',
                'reply': "\n".join(reply_lines)
            }
        else:
            return {
                'intent': 'chat',
                'reply': "You don't have any daily tasks active yet. Add one with '+ Add' or ask me to add one!"
            }

    # 5. Focus session suggestion
    if 'plan a focus session' in msg_lower or 'focus session' in msg_lower or 'timer' in msg_lower:
        return {
            'intent': 'chat',
            'reply': "I'd love to help you plan a focus session! I recommend a 25-minute Pomodoro block:\n\n1. **Choose one task** from your list.\n2. **Set the Focus Timer** (available in the left panel ⏱️) to 25 minutes.\n3. **Minimize distractions** (close tabs, put phone away).\n4. **Work single-mindedly** until the timer rings.\n5. **Take a 5-minute break**, then repeat!"
        }

    # 6. Productivity tips suggestion
    if 'tips to boost productivity' in msg_lower or 'productivity tips' in msg_lower or 'boost productivity' in msg_lower:
        return {
            'intent': 'chat',
            'reply': "Here are my top 3 productivity tips to stay on track:\n\n1. **Eat the Frog**: Complete your highest-priority task first thing in the morning.\n2. **Time-Boxing**: Allocate fixed time slots to specific tasks to prevent them from dragging on.\n3. **Minimize Context-Switching**: Batch similar tasks (like replying to emails or coding) together to stay in the flow zone."
        }

    # ── GEMINI API EXECUTION ──────────────────────────────────────────────────
    if os.environ.get('GEMINI_API_KEY'):
        try:
            client = get_client()
            model = os.environ.get('GEMINI_MODEL', 'gemini-3.5-flash-lite')

            if today_tasks:
                task_lines = "\n".join(
                    f"- id={t['id']} [{'done' if t['completed'] else 'pending'}] {t['content']} [priority={t.get('priority', 'medium')}]"
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
        except Exception as e:
            print(f"[ChatEngine] Gemini error: {type(e).__name__}: {e}")
            if '429' in str(e):
                return {'intent': 'chat', 'reply': "I'm getting a lot of requests right now — give me a few seconds and try again."}
            return {'intent': 'chat', 'reply': "I'm having a little trouble connecting to my server right now. Could we try again in a bit?"}
    else:
        # Fallback explanation if API key is not configured and no rule matches
        return {
            'intent': 'chat',
            'reply': "Hello! The GEMINI_API_KEY environment variable is not set. You can still manage your schedule using the dashboard controls or simple commands like 'add habit X', 'complete task X', or 'prioritize'."
        }
