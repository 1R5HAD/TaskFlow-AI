# TaskFlow AI — AI-Powered Daily Habit & Task Coach

![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)
![Flask](https://img.shields.io/badge/Flask-3.0.3-black?logo=flask)
![Docker](https://img.shields.io/badge/Docker-Containerized-2496ED?logo=docker)
![CI/CD](https://img.shields.io/badge/CI%2FCD-GitHub%20Actions-2088FF?logo=githubactions)
![Deploy](https://img.shields.io/badge/Deployed-Render-46E3B7?logo=render)
![Tests](https://img.shields.io/badge/Tests-8%20Passed-brightgreen?logo=pytest)
yes
A fully functional multi-user To-Do web application with a complete end-to-end DevOps pipeline — automated testing, Docker containerization, CI/CD via GitHub Actions, and cloud deployment on Render with PostgreSQL.

> 🎓 Built as part of the **DevOps (25CSE1663)** course — Semester 6, CSE, BNMIT

---

## 🌐 Live Demo

**[<TASKFLOW_AI_RENDER_URL — update after first deploy>](<TASKFLOW_AI_RENDER_URL — update after first deploy>)**

> Note: The app may take ~30 seconds to load on first visit due to Render's free tier spin-down policy.

---

## 📸 Features

- 🔐 **User Authentication** — Signup, login, logout with hashed passwords
- ✅ **Task Management** — Add, complete, and delete tasks
- 🎯 **Priority Levels** — High 🔴, Medium 🟡, Low 🟢 with color-coded cards
- 📅 **Due Dates** — Set due dates for every task
- 📊 **Stats Bar** — Live count of total, pending, and completed tasks
- 📧 **Email Reminders** — Automatic email alerts for High priority tasks due in 1 or 2 days
- 🔒 **Data Isolation** — Each user sees only their own tasks
- 🌙 **Dark UI** — Modern dark-themed responsive interface

---

## 🏗️ DevOps Pipeline

```
git push origin main
        ↓
┌─────────────────────────────────────────┐
│         GitHub Actions Pipeline         │
│                                         │
│  Stage 1: Test                          │
│  → pip install dependencies             │
│  → pytest runs 8 tests                  │
│  → Fails here if any test fails ❌      │
│                                         │
│  Stage 2: Build                         │
│  → docker build .                       │
│  → docker push to Docker Hub            │
│                                         │
│  Stage 3: Deploy                        │
│  → curl Render deploy webhook           │
│  → Render pulls new image + redeploys   │
└─────────────────────────────────────────┘
        ↓
   Live on Render ✅
   PostgreSQL untouched ✅
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Frontend | HTML, CSS, JavaScript, Jinja2 |
| Backend | Python Flask 3.0.3 |
| Database (Production) | PostgreSQL on Render |
| Database (Development) | SQLite |
| ORM | Flask-SQLAlchemy 3.1.1 |
| Authentication | Flask-Login 0.6.3 |
| Password Hashing | Werkzeug 3.0.3 |
| Email API | Brevo (sib-api-v3-sdk) |
| Scheduler | APScheduler 3.10.4 |
| Containerization | Docker |
| Image Registry | Docker Hub |
| CI/CD | GitHub Actions |
| Hosting | Render |
| Testing | pytest 8.3.2 |

---

## 📁 Project Structure

```
taskflow-ai/
├── app.py                        # Flask app — routes, models, scheduler, email
├── requirements.txt              # Python dependencies
├── Dockerfile                    # Container build instructions
├── conftest.py                   # pytest path configuration
├── .gitignore                    # Ignored files
├── templates/
│   ├── index.html                # Main task dashboard
│   ├── login.html                # Login page
│   └── signup.html               # Signup page
├── tests/
│   └── test_app.py               # 8 automated tests
└── .github/
    └── workflows/
        └── deploy.yml            # 3-stage CI/CD pipeline
```

---

## 🚀 Running Locally

### Prerequisites
- Python 3.11+
- Git
- Docker Desktop (optional, for container testing)

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/1R5HAD/TaskFlow-AI.git
cd TaskFlow-AI

# 2. Create and activate virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # macOS/Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the app
python app.py

# 5. Open in browser
# http://localhost:5000
```

The app will automatically create a local `tasks.db` SQLite database on first run.

---

## 🐳 Running with Docker

```bash
# Build the image
docker build -t taskflow-ai .

# Run the container
docker run -p 5000:5000 taskflow-ai

# Open in browser
# http://localhost:5000
```

---

## 🧪 Running Tests

```bash
# Run all tests with verbose output
pytest tests/ -v
```

Expected output:
```
tests/test_app.py::test_signup                      PASSED
tests/test_app.py::test_signup_duplicate_username   PASSED
tests/test_app.py::test_login_success               PASSED
tests/test_app.py::test_login_wrong_password        PASSED
tests/test_app.py::test_logout                      PASSED
tests/test_app.py::test_add_task                    PASSED
tests/test_app.py::test_delete_task                 PASSED
tests/test_app.py::test_unauthenticated_access      PASSED

8 passed in ~2s
```

Tests use an in-memory SQLite database — they never touch the production PostgreSQL instance.

---

## ⚙️ Environment Variables

Set these in Render's Environment tab for production deployment:

| Variable | Description |
|---|---|
| `SECRET_KEY` | Flask session signing key — any long random string |
| `DATABASE_URL` | PostgreSQL connection URL from Render |
| `BREVO_API_KEY` | Brevo API key for sending emails |
| `BREVO_SENDER_EMAIL` | Verified sender email address on Brevo |
| `BREVO_SENDER_NAME` | Display name for sent emails (e.g. TaskFlow) |

Set these in GitHub → Settings → Secrets → Actions for the CI/CD pipeline:

| Secret | Description |
|---|---|
| `DOCKER_USERNAME` | Docker Hub username |
| `DOCKER_PASSWORD` | Docker Hub access token |
| `RENDER_DEPLOY_HOOK` | Render deploy webhook URL |

---

## 📧 Email Notification Logic

```
User adds a HIGH priority task
        ↓
Due in 2 days? → "Your task is due in 2 days" email sent instantly
Due in 1 day?  → "Your task is due tomorrow" email sent instantly
Due today?     → "Your task is due today" email sent instantly
Due in 3+ days → No immediate email
        ↓
Every midnight (IST) — scheduler checks:
Any HIGH priority incomplete task due tomorrow?
→ Follow-up reminder email sent to owner
```

Medium and Low priority tasks never trigger email notifications.

---

## 🔒 Security

- Passwords hashed with `pbkdf2:sha256` via Werkzeug — never stored as plain text
- Session cookies signed with `SECRET_KEY`
- All task routes filter by `user_id` — users cannot access each other's tasks
- All credentials stored as environment variables — never hardcoded in source code
- GitHub Secrets used for CI/CD credentials — never visible in pipeline logs

---

## 📋 CI/CD Pipeline Details

The `.github/workflows/deploy.yml` defines three sequential jobs:

```yaml
test  →  build  →  deploy
```

- **test** — Runs pytest on ubuntu-latest with SQLite. Blocks all subsequent stages on failure.
- **build** — Builds Docker image and pushes to Docker Hub. Only runs if tests pass.
- **deploy** — Calls Render webhook. Only runs if build succeeds.

This ensures broken code **never reaches production**.

---

## 📄 License

This project is built for academic purposes as part of the DevOps course at BNMIT.
