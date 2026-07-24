import os
import json
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta, timezone
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

from ai.chat_engine import classify_message

app = Flask(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')

database_url = os.environ.get('DATABASE_URL', 'sqlite:///tasks.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ─── AUTH SETUP ───────────────────────────────────────────────────────────────

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = ''


# ─── MODELS ───────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    tasks         = db.relationship('Task', backref='owner', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Task(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    content    = db.Column(db.String(200), nullable=False)
    priority   = db.Column(db.String(10), default='medium')
    due_date   = db.Column(db.String(20), nullable=True) # Kept for SQLite backward compatibility
    completed  = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    streak     = db.relationship('Streak', backref='task', uselist=False, cascade='all, delete-orphan')


class Streak(db.Model):
    id                   = db.Column(db.Integer, primary_key=True)
    # Maps SQLAlchemy task_id property to the existing physical 'habit_id' column to preserve DB compatibility
    task_id              = db.Column('habit_id', db.Integer, db.ForeignKey('task.id'), nullable=False, unique=True)
    current_streak       = db.Column(db.Integer, default=0)
    longest_streak       = db.Column(db.Integer, default=0)
    last_completed_date  = db.Column(db.Date, nullable=True)


class ChatMessage(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    user_id   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role      = db.Column(db.String(10), nullable=False)   # 'user' | 'assistant'
    content   = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class ChatAction(db.Model):
    """Undo log — one row per chatbot-made mutation, enough state to reverse it."""
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action_type = db.Column(db.String(30), nullable=False)  # 'create_habits' | 'complete_tasks'
    payload     = db.Column(db.Text, nullable=False)         # JSON string
    undone      = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ─── EMAIL HELPER ─────────────────────────────────────────────────────────────

def send_email(to_email, to_name, subject, body):
    """Send an email via Brevo HTTP API — works on Render free tier."""
    try:
        configuration = sib_api_v3_sdk.Configuration()
        configuration.api_key['api-key'] = os.environ.get('BREVO_API_KEY', '')

        api_instance = sib_api_v3_sdk.TransactionalEmailsApi(
            sib_api_v3_sdk.ApiClient(configuration)
        )

        send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
            to=[{"email": to_email, "name": to_name}],
            sender={
                "email": os.environ.get('BREVO_SENDER_EMAIL', ''),
                "name":  os.environ.get('BREVO_SENDER_NAME', 'TaskFlow')
            },
            subject=subject,
            text_content=body
        )

        api_instance.send_transac_email(send_smtp_email)
        print(f"[Email] ✅ Sent to {to_email}: {subject}")
        return True, None

    except ApiException as e:
        error = f"Brevo API error: {e}"
        print(f"[Email] ❌ {error}")
        return False, error
    except Exception as e:
        print(f"[Email] ❌ {e}")
        return False, str(e)


# ─── REAL-TIME NOTIFICATION ON TASK CREATION ─────────────────────────────────

def notify_if_urgent(task, user):
    """
    Called immediately after a daily task is created.
    No-op since due dates are removed.
    """
    pass


# ─── MIDNIGHT SCHEDULER — YESTERDAY'S MISSES & RESET ─────────────────────────

def midnight_check():
    """
    Runs every day at midnight IST.
    1. Finds HIGH priority tasks from yesterday that were left incomplete,
       and sends a follow-up email.
    2. Resets completed status for all daily tasks to False.
    """
    with app.app_context():
        yesterday = date.today() - timedelta(days=1)
        print(f"[Scheduler] Midnight check — looking for incomplete High priority tasks from yesterday")

        urgent_tasks = Task.query.filter_by(
            priority='high',
            completed=False
        ).all()

        if urgent_tasks:
            # Group by user — one email per user
            tasks_by_user = {}
            for task in urgent_tasks:
                user = db.session.get(User, task.user_id)
                if user:
                    if user.id not in tasks_by_user:
                        tasks_by_user[user.id] = {'user': user, 'tasks': []}
                    tasks_by_user[user.id]['tasks'].append(task)

            for entry in tasks_by_user.values():
                user  = entry['user']
                tasks = entry['tasks']

                task_lines = '\n'.join(f"  📌 {t.content}" for t in tasks)

                subject = f"⚠️ Missed High Priority task{'s' if len(tasks) > 1 else ''} yesterday!"
                body = f"""Hi {user.username},

This is a follow-up reminder from TaskFlow.

The following HIGH priority daily task{'s' if len(tasks) > 1 else ''} were left incomplete yesterday:

{task_lines}

Try to get back on track today!

Open TaskFlow: https://taskflow-ai-lc5z.onrender.com

— TaskFlow
"""
                send_email(user.email, user.username, subject, body)

        # Reset daily completion statuses
        print("[Scheduler] Resetting completion status for all daily tasks")
        Task.query.update({Task.completed: False})
        db.session.commit()


# ─── SCHEDULER SETUP ──────────────────────────────────────────────────────────

def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=midnight_check,
        trigger='cron',
        hour=0,
        minute=0,
        timezone='Asia/Kolkata',    # midnight IST
        id='midnight_reminder'
    )
    scheduler.start()
    print("[Scheduler] Started — midnight check active (IST)")
    atexit.register(lambda: scheduler.shutdown())


# ─── ROUTES: AUTH ─────────────────────────────────────────────────────────────

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not username or not email or not password:
            flash('All fields are required.', 'error')
            return redirect(url_for('signup'))
        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'error')
            return redirect(url_for('signup'))
        if User.query.filter_by(username=username).first():
            flash('Username already taken.', 'error')
            return redirect(url_for('signup'))
        if User.query.filter_by(email=email).first():
            flash('Email already registered.', 'error')
            return redirect(url_for('signup'))

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('index'))

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user, remember=True)
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password.', 'error')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# ─── ROUTES: TASKS ────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    # Retrieve all daily tasks for the user
    tasks = Task.query.filter_by(user_id=current_user.id).order_by(Task.created_at.desc()).all()
    
    pct = 0
    if tasks:
        pct = round(100 * sum(1 for t in tasks if t.completed) / len(tasks))

    return render_template('index.html', daily_tasks=tasks, routine_pct=pct)


@app.route('/routine/status')
@login_required
def routine_status():
    """Today's tasks as JSON — used by the chat panel to refresh the task list
    in place, without reloading the page."""
    tasks = Task.query.filter_by(user_id=current_user.id).order_by(Task.created_at.desc()).all()
    pct = 0
    if tasks:
        pct = round(100 * sum(1 for t in tasks if t.completed) / len(tasks))

    tasks_json = [{
        'id': t.id,
        'title': t.content,
        'priority': t.priority,
        'completed': t.completed,
        'streak': t.streak.current_streak if t.streak else 0,
    } for t in tasks]

    return jsonify({'tasks': tasks_json, 'pct': pct})


@app.route('/daily/complete/<int:daily_task_id>', methods=['POST'])
@login_required
def complete_daily_task(daily_task_id):
    task = Task.query.filter_by(id=daily_task_id, user_id=current_user.id).first_or_404()
    task.completed = not task.completed
    if task.completed:
        update_streak(task.id)
    else:
        revert_streak(task.id)
    db.session.commit()
    return jsonify({'completed': task.completed})


@app.route('/add', methods=['POST'])
@login_required
def add_task():
    content  = request.form.get('content', '').strip()
    priority = request.form.get('priority', 'medium')

    if not content:
        return redirect(url_for('index'))

    new_task = Task(
        content  = content,
        priority = priority,
        user_id  = current_user.id
    )
    db.session.add(new_task)
    db.session.flush() # assign ID

    # Create a Streak for this task
    db.session.add(Streak(task_id=new_task.id))

    db.session.commit()
    return redirect(url_for('index'))


@app.route('/complete/<int:daily_task_id>', methods=['POST', 'GET'])
@login_required
def complete_task(daily_task_id):
    task = Task.query.filter_by(id=daily_task_id, user_id=current_user.id).first_or_404()
    task.completed = not task.completed
    if task.completed:
        update_streak(task.id)
    else:
        revert_streak(task.id)
    db.session.commit()
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.args.get('ajax') == '1' or request.method == 'POST':
        return jsonify({'completed': task.completed})
    return redirect(url_for('index'))


@app.route('/delete/<int:task_id>')
@login_required
def delete_task(task_id):
    task = Task.query.filter_by(id=task_id, user_id=current_user.id).first_or_404()
    db.session.delete(task)
    db.session.commit()
    return redirect(url_for('index'))


# ─── STREAK HELPERS ──────────────────────────────────────────────────────────

def ensure_today_tasks(user):
    """No-op wrapper to avoid breaking code calling this on index or chat routes."""
    pass


def update_streak(task_id):
    streak = Streak.query.filter_by(task_id=task_id).first()
    if not streak:
        streak = Streak(task_id=task_id)
        db.session.add(streak)

    today = date.today()
    if streak.last_completed_date == today:
        return  # already counted today
    if streak.last_completed_date == today - timedelta(days=1):
        streak.current_streak += 1
    else:
        streak.current_streak = 1
    streak.last_completed_date = today
    streak.longest_streak = max(streak.longest_streak or 0, streak.current_streak)


def revert_streak(task_id):
    """Best-effort undo — only correct if the completion being undone was today's."""
    streak = Streak.query.filter_by(task_id=task_id).first()
    if streak and streak.last_completed_date == date.today():
        streak.current_streak = max(0, streak.current_streak - 1)
        streak.last_completed_date = None


# ─── ROUTES: AI CHAT ────────────────────────────────────────────────────────────

@app.route('/chat', methods=['POST'])
@login_required
def chat():
    user_message = (request.json or {}).get('message', '').strip()
    if not user_message:
        return jsonify({'error': 'empty message'}), 400

    today_tasks = Task.query.filter_by(user_id=current_user.id).all()
    task_dicts = [{'id': t.id, 'content': t.content, 'completed': t.completed, 'priority': t.priority} for t in today_tasks]

    recent = ChatMessage.query.filter_by(user_id=current_user.id)\
                               .order_by(ChatMessage.timestamp.desc()).limit(8).all()
    history = [{'role': m.role, 'content': m.content} for m in reversed(recent)]

    db.session.add(ChatMessage(user_id=current_user.id, role='user', content=user_message))
    db.session.commit()

    try:
        result = classify_message(user_message, task_dicts, history)
    except Exception as e:
        print(f"[Chat] LLM error: {type(e).__name__}: {e}")
        result = {'intent': 'chat', 'reply': "Sorry, I couldn't process that — could you rephrase it?",
                  'debug_error': f"{type(e).__name__}: {e}"}

    intent = result.get('intent')
    reply = "I'm not sure how to help with that yet."
    action_id = None

    if intent == 'create_routine':
        titles = []
        for h in result.get('habits', []):
            # AI tasks get added to the same list with default medium priority
            priority = h.get('priority', 'medium')
            task = Task(user_id=current_user.id, content=h.get('title', '').strip(), priority=priority)
            if not task.content:
                continue
            db.session.add(task)
            db.session.flush()  # assign task.id
            db.session.add(Streak(task_id=task.id))
            titles.append(task.content)

        action = ChatAction(user_id=current_user.id, action_type='create_habits',
                             payload=json.dumps({'habit_titles': titles}))
        db.session.add(action)
        db.session.commit()
        action_id = action.id
        reply = f"Added task(s) to your list: {', '.join(titles)}."

    elif intent == 'complete_tasks':
        completed_ids = []
        for tid in result.get('task_ids', []):
            task = Task.query.filter_by(id=tid, user_id=current_user.id).first()
            if task and not task.completed:
                task.completed = True
                update_streak(task.id)
                completed_ids.append(tid)

        action = ChatAction(user_id=current_user.id, action_type='complete_tasks',
                             payload=json.dumps({'task_ids': completed_ids}))
        db.session.add(action)
        db.session.commit()
        action_id = action.id if completed_ids else None
        reply = (f"Marked {len(completed_ids)} task(s) done — nice work."
                 if completed_ids else "I couldn't match that to any of today's tasks.")

    elif intent == 'status_query':
        pending = [t for t in today_tasks if not t.completed]
        if pending:
            bullets = '\n'.join(f"- {t.content}" for t in pending)
            reply = f"You have {len(pending)} task{'s' if len(pending) != 1 else ''} pending today:\n{bullets}"
        else:
            reply = "Everything's done for today — nice."

    elif intent == 'clarify':
        reply = result.get('question', 'Could you clarify that?')

    elif intent == 'chat':
        reply = result.get('reply', reply)

    db.session.add(ChatMessage(user_id=current_user.id, role='assistant', content=reply))
    db.session.commit()

    resp = {'reply': reply, 'action_id': action_id}
    if 'debug_error' in result:
        resp['debug_error'] = result['debug_error']
    return jsonify(resp)


@app.route('/chat/undo/<int:action_id>', methods=['POST'])
@login_required
def undo_chat_action(action_id):
    action = ChatAction.query.filter_by(id=action_id, user_id=current_user.id, undone=False).first_or_404()
    payload = json.loads(action.payload)

    if action.action_type == 'complete_tasks':
        for tid in payload.get('task_ids', []):
            task = Task.query.filter_by(id=tid, user_id=current_user.id).first()
            if task and task.completed:
                task.completed = False
                revert_streak(task.id)

    elif action.action_type == 'create_habits':
        tasks = Task.query.filter(
            Task.user_id == current_user.id,
            Task.content.in_(payload.get('habit_titles', []))
        ).all()
        for t in tasks:
            db.session.delete(t)  # cascades to streak

    action.undone = True
    db.session.commit()
    return jsonify({'status': 'undone'})


# ─── TEST EMAIL ROUTE (remove before final submission) ────────────────────────

@app.route('/test-email')
@login_required
def test_email():
    success, error = send_email(
        to_email=current_user.email,
        to_name=current_user.username,
        subject="✅ TaskFlow — Test Email",
        body=f"Hi {current_user.username},\n\nThis is a test email from TaskFlow.\n\nYour email notifications are working correctly!\n\n— TaskFlow"
    )
    if success:
        return f"✅ Test email sent to {current_user.email} — check your inbox!"
    else:
        return f"❌ Failed: {error}"


# ─── STARTUP ──────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()
    # Programmatically drop the legacy PostgreSQL foreign key constraint if it exists
    try:
        db.session.execute(db.text('ALTER TABLE streak DROP CONSTRAINT IF EXISTS streak_habit_id_fkey;'))
        db.session.commit()
        print("[Database] Legacy foreign key constraint dropped successfully.")
    except Exception as e:
        db.session.rollback()
        print(f"[Database] Skip dropping constraint: {e}")

start_scheduler()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
