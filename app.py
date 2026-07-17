import os
import json
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, timedelta
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
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    tasks = db.relationship('Task', backref='owner', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Task(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    content    = db.Column(db.String(200), nullable=False)
    priority   = db.Column(db.String(10), default='medium')
    due_date   = db.Column(db.String(20), nullable=True)
    completed  = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)


# ─── AI ROUTINE MODELS ─────────────────────────────────────────────────────────

class Habit(db.Model):
    """A recurring task template — e.g. 'Solve 2 DSA problems'. The chatbot
    creates these from a routine request; daily rows in DailyTask are generated
    from active habits each day."""
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title      = db.Column(db.String(200), nullable=False)
    category   = db.Column(db.String(50), nullable=True)   # e.g. 'coding', 'dsa', 'language'
    frequency  = db.Column(db.String(20), default='daily')
    active     = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    daily_tasks = db.relationship('DailyTask', backref='habit', lazy=True, cascade='all, delete-orphan')
    streak      = db.relationship('Streak', backref='habit', uselist=False, cascade='all, delete-orphan')


class DailyTask(db.Model):
    """One day's instance of a habit. 'Reset daily' = a fresh row per date,
    not a mutation of the habit itself."""
    id           = db.Column(db.Integer, primary_key=True)
    habit_id     = db.Column(db.Integer, db.ForeignKey('habit.id'), nullable=False)
    user_id      = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date         = db.Column(db.Date, nullable=False, default=date.today)
    completed    = db.Column(db.Boolean, default=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('habit_id', 'date', name='uq_habit_date'),)


class Streak(db.Model):
    id                   = db.Column(db.Integer, primary_key=True)
    habit_id             = db.Column(db.Integer, db.ForeignKey('habit.id'), nullable=False, unique=True)
    current_streak       = db.Column(db.Integer, default=0)
    longest_streak        = db.Column(db.Integer, default=0)
    last_completed_date  = db.Column(db.Date, nullable=True)


class ChatMessage(db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    user_id   = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    role      = db.Column(db.String(10), nullable=False)   # 'user' | 'assistant'
    content   = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class ChatAction(db.Model):
    """Undo log — one row per chatbot-made mutation, enough state to reverse it."""
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action_type = db.Column(db.String(30), nullable=False)  # 'create_habits' | 'complete_tasks'
    payload     = db.Column(db.Text, nullable=False)         # JSON string
    undone      = db.Column(db.Boolean, default=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


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
    Called immediately after a HIGH priority task is created.
    Sends an email right away if due date is 1 or 2 days from today.
    """
    if task.priority != 'high' or not task.due_date:
        return  # Only notify for high priority tasks with a due date

    try:
        due    = date.fromisoformat(task.due_date)  # parse "YYYY-MM-DD"
        today  = date.today()
        days_remaining = (due - today).days
    except ValueError:
        return  # Invalid date format — skip

    if days_remaining == 2:
        subject = f"⚠️ High Priority Task due in 2 days!"
        body = f"""Hi {user.username},

You have a HIGH priority task due in 2 days ({task.due_date}):

  📌 {task.content}

Make sure you complete it on time!

Open TaskFlow: https://taskflow-ai-lc5z.onrender.com

— TaskFlow
"""
        send_email(user.email, user.username, subject, body)

    elif days_remaining == 1:
        subject = f"🚨 High Priority Task due TOMORROW!"
        body = f"""Hi {user.username},

Urgent reminder — your HIGH priority task is due TOMORROW ({task.due_date}):

  📌 {task.content}

Don't leave it for the last minute!

Open TaskFlow: https://taskflow-ai-lc5z.onrender.com

— TaskFlow
"""
        send_email(user.email, user.username, subject, body)

    elif days_remaining == 0:
        subject = f"🔴 High Priority Task due TODAY!"
        body = f"""Hi {user.username},

Your HIGH priority task is due TODAY ({task.due_date}):

  📌 {task.content}

Complete it as soon as possible!

Open TaskFlow: https://taskflow-ai-lc5z.onrender.com

— TaskFlow
"""
        send_email(user.email, user.username, subject, body)

    else:
        print(f"[Notify] Task '{task.content}' due in {days_remaining} days — no immediate email needed")


# ─── MIDNIGHT SCHEDULER — DAY-BEFORE FOLLOW-UP ───────────────────────────────

def midnight_check():
    """
    Runs every day at midnight IST.
    Finds HIGH priority incomplete tasks that are now exactly 1 day away
    (i.e. they were added when 2 days remained, now 1 day remains).
    Sends a follow-up reminder.
    """
    with app.app_context():
        tomorrow = (date.today() + timedelta(days=1)).strftime('%Y-%m-%d')
        print(f"[Scheduler] Midnight check — looking for HIGH priority tasks due on {tomorrow}")

        urgent_tasks = Task.query.filter_by(
            due_date=tomorrow,
            priority='high',
            completed=False
        ).all()

        if not urgent_tasks:
            print(f"[Scheduler] No urgent tasks due tomorrow")
            return

        # Group by user — one email per user
        tasks_by_user = {}
        for task in urgent_tasks:
            user = User.query.get(task.user_id)
            if user:
                if user.id not in tasks_by_user:
                    tasks_by_user[user.id] = {'user': user, 'tasks': []}
                tasks_by_user[user.id]['tasks'].append(task)

        for entry in tasks_by_user.values():
            user  = entry['user']
            tasks = entry['tasks']

            task_lines = '\n'.join(f"  📌 {t.content}" for t in tasks)

            subject = f"🚨 {len(tasks)} High Priority task{'s' if len(tasks) > 1 else ''} due TOMORROW!"
            body = f"""Hi {user.username},

This is your follow-up reminder from TaskFlow.

The following HIGH priority task{'s are' if len(tasks) > 1 else ' is'} due TOMORROW ({tomorrow}):

{task_lines}

Make sure you complete {'them' if len(tasks) > 1 else 'it'} today!

Open TaskFlow: https://taskflow-ai-lc5z.onrender.com

— TaskFlow
"""
            send_email(user.email, user.username, subject, body)


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
    tasks = Task.query.filter_by(user_id=current_user.id)\
                      .order_by(Task.created_at.desc()).all()

    ensure_today_tasks(current_user)
    daily_tasks = DailyTask.query.filter_by(user_id=current_user.id, date=date.today())\
                                  .join(Habit).order_by(Habit.title).all()
    routine_pct = 0
    if daily_tasks:
        routine_pct = round(100 * sum(1 for t in daily_tasks if t.completed) / len(daily_tasks))

    return render_template('index.html', tasks=tasks, daily_tasks=daily_tasks, routine_pct=routine_pct)


@app.route('/daily/complete/<int:daily_task_id>', methods=['POST'])
@login_required
def complete_daily_task(daily_task_id):
    task = DailyTask.query.filter_by(id=daily_task_id, user_id=current_user.id).first_or_404()
    task.completed = not task.completed
    if task.completed:
        task.completed_at = datetime.utcnow()
        update_streak(task.habit_id)
    else:
        task.completed_at = None
        revert_streak(task.habit_id)
    db.session.commit()
    return jsonify({'completed': task.completed})


@app.route('/add', methods=['POST'])
@login_required
def add_task():
    content  = request.form.get('content', '').strip()
    priority = request.form.get('priority', 'medium')
    due_date = request.form.get('due_date', None)

    if not content:
        return redirect(url_for('index'))

    new_task = Task(
        content  = content,
        priority = priority,
        due_date = due_date if due_date else None,
        user_id  = current_user.id
    )
    db.session.add(new_task)
    db.session.commit()

    # ── Real-time notification ──────────────────────────────
    # Check immediately after saving — no waiting for a scheduler
    notify_if_urgent(new_task, current_user)

    return redirect(url_for('index'))


@app.route('/complete/<int:task_id>')
@login_required
def complete_task(task_id):
    task = Task.query.filter_by(id=task_id, user_id=current_user.id).first_or_404()
    task.completed = not task.completed
    db.session.commit()
    return redirect(url_for('index'))


@app.route('/delete/<int:task_id>')
@login_required
def delete_task(task_id):
    task = Task.query.filter_by(id=task_id, user_id=current_user.id).first_or_404()
    db.session.delete(task)
    db.session.commit()
    return redirect(url_for('index'))


# ─── AI ROUTINE HELPERS ────────────────────────────────────────────────────────

def ensure_today_tasks(user):
    """Generate today's DailyTask row for every active habit that doesn't have one yet."""
    today = date.today()
    habits = Habit.query.filter_by(user_id=user.id, active=True).all()
    for h in habits:
        if not DailyTask.query.filter_by(habit_id=h.id, date=today).first():
            db.session.add(DailyTask(habit_id=h.id, user_id=user.id, date=today))
    db.session.commit()


def update_streak(habit_id):
    streak = Streak.query.filter_by(habit_id=habit_id).first()
    if not streak:
        streak = Streak(habit_id=habit_id)
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


def revert_streak(habit_id):
    """Best-effort undo — only correct if the completion being undone was today's."""
    streak = Streak.query.filter_by(habit_id=habit_id).first()
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

    ensure_today_tasks(current_user)
    today_tasks = DailyTask.query.filter_by(user_id=current_user.id, date=date.today()).all()
    task_dicts = [{'id': t.id, 'content': t.habit.title, 'completed': t.completed} for t in today_tasks]

    db.session.add(ChatMessage(user_id=current_user.id, role='user', content=user_message))
    db.session.commit()

    try:
        result = classify_message(user_message, task_dicts)
    except Exception as e:
        print(f"[Chat] LLM error: {e}")
        return jsonify({'reply': "Sorry, I couldn't process that right now — try again in a moment.",
                         'debug_error': str(e)}), 500

    intent = result.get('intent')
    reply = "I'm not sure how to help with that yet."
    action_id = None

    if intent == 'create_routine':
        titles = []
        for h in result.get('habits', []):
            habit = Habit(user_id=current_user.id, title=h.get('title', '').strip(), category=h.get('category'))
            if not habit.title:
                continue
            db.session.add(habit)
            db.session.flush()  # assign habit.id before use
            db.session.add(DailyTask(habit_id=habit.id, user_id=current_user.id, date=date.today()))
            db.session.add(Streak(habit_id=habit.id))
            titles.append(habit.title)

        action = ChatAction(user_id=current_user.id, action_type='create_habits',
                             payload=json.dumps({'habit_titles': titles}))
        db.session.add(action)
        db.session.commit()
        action_id = action.id
        reply = f"Set up your routine: {', '.join(titles)}. These reset daily and I'll track your streaks."

    elif intent == 'complete_tasks':
        completed_ids = []
        for tid in result.get('task_ids', []):
            task = DailyTask.query.filter_by(id=tid, user_id=current_user.id, date=date.today()).first()
            if task and not task.completed:
                task.completed = True
                task.completed_at = datetime.utcnow()
                update_streak(task.habit_id)
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
        reply = (f"Still open today: {', '.join(t.habit.title for t in pending)}"
                 if pending else "Everything's done for today — nice.")

    elif intent == 'clarify':
        reply = result.get('question', 'Could you clarify that?')

    elif intent == 'chat':
        reply = result.get('reply', reply)

    db.session.add(ChatMessage(user_id=current_user.id, role='assistant', content=reply))
    db.session.commit()

    return jsonify({'reply': reply, 'action_id': action_id})


@app.route('/chat/undo/<int:action_id>', methods=['POST'])
@login_required
def undo_chat_action(action_id):
    action = ChatAction.query.filter_by(id=action_id, user_id=current_user.id, undone=False).first_or_404()
    payload = json.loads(action.payload)

    if action.action_type == 'complete_tasks':
        for tid in payload.get('task_ids', []):
            task = DailyTask.query.filter_by(id=tid, user_id=current_user.id).first()
            if task and task.completed:
                task.completed = False
                task.completed_at = None
                revert_streak(task.habit_id)

    elif action.action_type == 'create_habits':
        habits = Habit.query.filter(
            Habit.user_id == current_user.id,
            Habit.title.in_(payload.get('habit_titles', []))
        ).all()
        for h in habits:
            db.session.delete(h)  # cascades to daily_tasks + streak

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

start_scheduler()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
