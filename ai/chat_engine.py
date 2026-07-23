import os
import json
import re
from google import genai
from google.genai import types

SYSTEM_PROMPT = """You are the TaskFlow AI assistant. You help the user manage their daily \
tasks (which reset each day). You are given the recent conversation \
history, today's task list (with ids), and the user's latest message. Use the \
conversation history to stay on track.

Respond with ONLY a single JSON object — no prose, no markdown fences — matching \
exactly one of these shapes:

1. You now have enough information to create/add one or more daily tasks:
{"intent": "create_routine", "habits": [{"title": "...", "category": "..."}]}
Break it into concrete, recurring daily tasks. Titles should be short and \
actionable (e.g. "Solve 2 DSA problems", "Read a book chapter").

2. User says they completed one or more of today's tasks:
{"intent": "complete_tasks", "task_ids": [1, 2]}
Match against today's task list by content. Only include ids you are confident about.

3. User asks what's left / how they're doing:
{"intent": "status_query"}

4. Genuinely ambiguous AND you haven't already asked a similar question in the recent \
history:
{"intent": "clarify", "question": "..."}

5. Anything else (prioritizing tasks, small talk, questions, encouragement):
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

    # 1. Greetings fallback
    if re.match(r'^(?:hey|hello|hi|yo|sup|good morning|good afternoon|good evening|hey there|hello there|hi there)[.!?]*$', msg_lower):
        return {
            'intent': 'chat',
            'reply': "Hey there! 👋 How can I help you with your tasks today?"
        }

    # 2. Conversational acknowledgments ("umm", "sure", "ok", "thanks")
    if msg_lower in ['umm', 'um', 'uh', 'sure', 'ok', 'okay', 'cool', 'thanks', 'thank you', 'great', 'nice', 'awesome', 'got it', 'k']:
        return {
            'intent': 'chat',
            'reply': "Let me know what task you'd like to work on or add! (e.g., 'add Read 20 pages' or 'prioritize')"
        }

    # 3. Identity / Who are you fallback
    if any(phrase in msg_lower for phrase in ['who are you', 'who r u', 'what is your name', 'what are you', 'who created you']):
        return {
            'intent': 'chat',
            'reply': "I'm TaskFlow AI! I'm your productivity assistant designed to help you organize daily tasks, track habits, and stay focused."
        }

    # 4. Help / What can you do fallback
    if any(phrase in msg_lower for phrase in ['what can you do', 'how do i use you', 'help me', 'commands', 'what can i ask']):
        return {
            'intent': 'chat',
            'reply': "Here is how I can help you:\n\n"
                     "• **Add tasks**: e.g., 'add Workout' or 'add Read 20 pages'\n"
                     "• **Complete tasks**: e.g., 'done with Workout' or 'complete task 1'\n"
                     "• **Check progress**: e.g., 'status' or 'what's left'\n"
                     "• **Prioritize**: e.g., 'prioritize my tasks'\n"
                     "• **Focus Timer**: click the Focus tab on the left edge"
        }

    # 5. Punctuation / Single question mark fallback
    if msg_lower in ['?', '??', '???', 'help', '']:
        return {
            'intent': 'chat',
            'reply': "How can I help you today? You can ask me to add a task, check your status, or prioritize your schedule!"
        }

    # 6. Incomplete / vague "add task" prompt
    if msg_lower in ['add a task', 'add task', 'create a task', 'create task', 'new task', 'add habit', 'create habit']:
        return {
            'intent': 'chat',
            'reply': "What task would you like to add? For example, type 'add Read 20 pages' or 'add Workout'."
        }

    # 7. Topic-based Task Suggestions (Coding, Study, Health, Focus, General)
    if any(k in msg_lower for k in ['suggest', 'recommend', 'ideas', 'give me tasks', 'tasks for', 'tasks to', 'improve']):
        if any(c in msg_lower for c in ['code', 'coding', 'program', 'developer', 'dsa', 'skill', 'software']):
            return {
                'intent': 'create_routine',
                'habits': [
                    {'title': 'Solve 1 LeetCode/DSA problem', 'category': 'coding'},
                    {'title': 'Read 20 mins of technical documentation', 'category': 'coding'},
                    {'title': 'Build or refactor code for 30 mins', 'category': 'coding'}
                ]
            }
        elif any(c in msg_lower for c in ['study', 'read', 'learn', 'book', 'exam']):
            return {
                'intent': 'create_routine',
                'habits': [
                    {'title': 'Read 20 pages of a book', 'category': 'study'},
                    {'title': 'Review study notes for 25 mins', 'category': 'study'}
                ]
            }
        elif any(c in msg_lower for c in ['health', 'fitness', 'workout', 'gym', 'exercise', 'run']):
            return {
                'intent': 'create_routine',
                'habits': [
                    {'title': '30-minute workout session', 'category': 'health'},
                    {'title': 'Drink 2 liters of water', 'category': 'health'}
                ]
            }
        elif any(c in msg_lower for c in ['focus', 'concentrat']):
            return {
                'intent': 'create_routine',
                'habits': [
                    {'title': '25-min Pomodoro focus session', 'category': 'focus'},
                    {'title': 'Take a 5-min screen-free break', 'category': 'focus'}
                ]
            }

    # Words/phrases that signal a vague or conversational add request — these should
    # be forwarded to the LLM so it can generate specific, actionable tasks
    # instead of creating a single task with the literal text.
    _VAGUE_SIGNALS = re.compile(
        r'\b(?:some|a few|a couple|several|many|tasks|habits|routine|routines'
        r'|to help|to improve|to boost|to increase|for me|for my|that|which'
        r'|about|around|related to|based on|suggest|recommend|ideas|tips)\b'
    )

    # 8. Add task / habit
    add_match = re.search(r'(?:add|create|new)\s+(?:a\s+)?(?:habit|task)\s+(?:named\s+)?["\']?([^"\']+)["\']?', msg_lower)
    if not add_match:
        # Match e.g. "add football", "create reading"
        add_match = re.search(r'^(?:add|create)\s+["\']?([^"\']+)["\']?$', msg_lower)

    if add_match:
        captured = add_match.group(1).strip()
        # If the user literally said "a task", "task", etc.
        if captured.lower() in ['a task', 'task', 'a habit', 'habit', 'tasks', 'habits', 'something', 'anything', '']:
            return {
                'intent': 'chat',
                'reply': "What task would you like to add? For example, type 'add Read 20 pages' or 'add Workout'."
            }
        # If the captured text looks vague/conversational, skip the local
        # shortcut and let the LLM interpret the user's real intent.
        if not _VAGUE_SIGNALS.search(captured):
            habit_title = captured.capitalize()
            habit_title = re.sub(r'[.!?]+$', '', habit_title)
            if habit_title:
                return {
                    'intent': 'create_routine',
                    'habits': [{'title': habit_title, 'category': 'general'}]
                }

    # 9. Complete task
    complete_match = re.search(r'(?:complete|done\s+with|finished|mark\s+done|check\s+off)\s+(.+)', msg_lower)
    if complete_match:
        target = complete_match.group(1).strip()
        target = re.sub(r'[.!?]+$', '', target)
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

    # 10. Status query
    if any(phrase in msg_lower for phrase in ['status', "what's left", 'show tasks', 'how am i doing', 'list tasks', 'my tasks']):
        return {
            'intent': 'status_query'
        }

    # 11. Prioritize tasks
    if 'prioritize' in msg_lower or 'priority' in msg_lower:
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

    # 12. Focus session suggestion
    if 'plan a focus session' in msg_lower or 'focus session' in msg_lower or 'timer' in msg_lower:
        return {
            'intent': 'chat',
            'reply': "I'd love to help you plan a focus session! I recommend a 25-minute Pomodoro block:\n\n1. **Choose one task** from your list.\n2. **Set the Focus Timer** (available in the left panel ⏱️) to 25 minutes.\n3. **Minimize distractions** (close tabs, put phone away).\n4. **Work single-mindedly** until the timer rings.\n5. **Take a 5-minute break**, then repeat!"
        }

    # 13. Productivity tips suggestion
    if 'tips to boost productivity' in msg_lower or 'productivity tips' in msg_lower or 'boost productivity' in msg_lower:
        return {
            'intent': 'chat',
            'reply': "Here are my top 3 productivity tips to stay on track:\n\n1. **Eat the Frog**: Complete your highest-priority task first thing in the morning.\n2. **Time-Boxing**: Allocate fixed time slots to specific tasks to prevent them from dragging on.\n3. **Minimize Context-Switching**: Batch similar tasks (like replying to emails or coding) together to stay in the flow zone."
        }

    # ── GEMINI API EXECUTION ──────────────────────────────────────────────────
    if os.environ.get('GEMINI_API_KEY'):
        try:
            client = get_client()
            primary_model = os.environ.get('GEMINI_MODEL', 'gemini-2.0-flash')
            fallback_models = [m for m in [primary_model, 'gemini-2.0-flash', 'gemini-1.5-flash'] if m]

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

            response = None
            last_err = None
            for model_name in dict.fromkeys(fallback_models):
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_PROMPT,
                            response_mime_type="application/json",
                            temperature=0.2,
                        ),
                    )
                    if response:
                        break
                except Exception as err:
                    last_err = err
                    print(f"[ChatEngine] Model {model_name} failed ({err}), trying next model...")

            if response and response.text:
                try:
                    return _extract_json(response.text)
                except (json.JSONDecodeError, ValueError):
                    print(f"[ChatEngine] Non-JSON response from model: {response.text[:300]!r}")
                    return {'intent': 'chat', 'reply': "Could you rephrase that? I'd be happy to help with your tasks!"}

            raise last_err or Exception("All Gemini models failed")

        except Exception as e:
            print(f"[ChatEngine] Gemini error: {type(e).__name__}: {e}")
            return {
                'intent': 'chat',
                'reply': "I'm here to help! You can ask me to add tasks (e.g. 'add Workout'), mark tasks done, check your status, or prioritize."
            }
    else:
        # Fallback explanation if API key is not configured and no rule matches
        return {
            'intent': 'chat',
            'reply': "Hey! I'm here to help. You can add tasks by typing 'add <task>', mark them complete, or ask me to prioritize your day!"
        }
