"""
TaskFlow — Basic Test Suite
============================
These tests run automatically in GitHub Actions before every deployment.
If any test fails, the pipeline stops here — nothing deploys.

We use Flask's built-in test client — it simulates a browser making
requests to your app without needing a real server running.
"""

import pytest
from app import app, db, User, Task


# ─── TEST SETUP ───────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    """
    Runs before each test — creates a clean test environment.
    Uses SQLite in-memory DB (:memory:) so tests never touch
    your real PostgreSQL database. Wiped after every test.
    """
    app.config['TESTING']                  = True
    app.config['SQLALCHEMY_DATABASE_URI']  = 'sqlite:///:memory:'
    app.config['WTF_CSRF_ENABLED']         = False
    app.config['LOGIN_DISABLED']           = False

    with app.app_context():
        db.create_all()
        yield app.test_client()
        db.drop_all()


def signup_and_login(client, username='testuser', email='test@example.com', password='password123'):
    """Helper — signs up and logs in a user in one call."""
    client.post('/signup', data={
        'username': username,
        'email': email,
        'password': password
    })


# ─── AUTH TESTS ───────────────────────────────────────────────────────────────

def test_signup(client):
    """
    A new user submits the signup form.
    Expect: redirected to home page and user saved in DB.
    FIX: email is lowercased by app — check for lowercase version.
    """
    response = client.post('/signup', data={
        'username': 'irshad',
        'email': 'Irshad@Example.com',   # intentional mixed case
        'password': 'mypassword'
    }, follow_redirects=True)

    assert response.status_code == 200

    with app.app_context():
        user = User.query.filter_by(username='irshad').first()
        assert user is not None
        assert user.email == 'irshad@example.com'   # app lowercases it


def test_signup_duplicate_username(client):
    """
    Two users try to sign up with the same username.
    Expect: second signup fails and stays on signup page.
    """
    client.post('/signup', data={'username': 'irshad', 'email': 'a@example.com', 'password': 'pass123'})
    response = client.post('/signup', data={'username': 'irshad', 'email': 'b@example.com', 'password': 'pass123'}, follow_redirects=True)

    assert response.status_code == 200
    with app.app_context():
        # Only one user with this username should exist
        count = User.query.filter_by(username='irshad').count()
        assert count == 1


def test_login_success(client):
    """
    A registered user logs in with correct credentials.
    Expect: redirected to home page.
    """
    signup_and_login(client)

    response = client.post('/login', data={
        'username': 'testuser',
        'password': 'password123'
    }, follow_redirects=True)

    assert response.status_code == 200
    assert b'TaskFlow' in response.data


def test_login_wrong_password(client):
    """
    A user tries to log in with the wrong password.
    Expect: stays on login page, does NOT reach home page.
    """
    # Only sign up, do NOT log in first
    client.post('/signup', data={
        'username': 'testuser2',
        'email': 'test2@example.com',
        'password': 'correctpassword'
    })
    # Logout to make sure no session is active
    client.get('/logout')

    response = client.post('/login', data={
        'username': 'testuser2',
        'password': 'wrongpassword'
    }, follow_redirects=True)

    assert response.status_code == 200
    assert b'New Task' not in response.data  # home page must NOT load


def test_logout(client):
    """
    A logged-in user clicks logout.
    Expect: redirected to login page.
    """
    signup_and_login(client)
    client.post('/login', data={'username': 'testuser', 'password': 'password123'})

    response = client.get('/logout', follow_redirects=True)

    assert response.status_code == 200
    assert b'Sign In' in response.data or b'login' in response.request.path.encode()


# ─── TASK TESTS ───────────────────────────────────────────────────────────────

def test_add_task(client):
    """
    A logged-in user adds a new task.
    Expect: task saved in DB with correct content and priority.
    """
    signup_and_login(client)
    client.post('/login', data={'username': 'testuser', 'password': 'password123'})

    client.post('/add', data={
        'content': 'Buy groceries',
        'priority': 'medium',
        'due_date': '2026-12-01'
    })

    with app.app_context():
        task = Task.query.filter_by(content='Buy groceries').first()
        assert task is not None
        assert task.priority == 'medium'


def test_delete_task(client):
    """
    A logged-in user deletes their own task.
    Expect: task no longer in DB.
    """
    signup_and_login(client)
    client.post('/login', data={'username': 'testuser', 'password': 'password123'})
    client.post('/add', data={'content': 'Task to delete', 'priority': 'low', 'due_date': ''})

    with app.app_context():
        task = Task.query.filter_by(content='Task to delete').first()
        assert task is not None
        task_id = task.id

    client.get(f'/delete/{task_id}', follow_redirects=True)

    with app.app_context():
        deleted = Task.query.get(task_id)
        assert deleted is None


def test_unauthenticated_access(client):
    """
    A visitor tries to access home page without logging in.
    Expect: redirected to /login.
    Proves: @login_required is protecting the route.
    """
    response = client.get('/', follow_redirects=False)

    assert response.status_code == 302
    assert '/login' in response.headers['Location']
