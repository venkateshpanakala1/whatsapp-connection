import re
from flask import Blueprint, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_conn, put_conn

auth_bp = Blueprint('auth', __name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


@auth_bp.route('/login', methods=['POST'])
def login():
    body     = request.get_json()
    email    = (body.get('email')    or '').strip().lower()
    password = (body.get('password') or '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    if not EMAIL_RE.match(email):
        return jsonify({'error': 'Please enter a valid email address', 'field': 'email'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id, password_hash FROM users WHERE email = %s', (email,))
        row = cur.fetchone()

        if row is None:
            # No auto-signup — a login only succeeds against an existing account.
            return jsonify({'error': 'No account found with this email', 'field': 'email'}), 404

        user_id, pw_hash = row
        if check_password_hash(pw_hash, password):
            session.permanent = True
            session['user_id'] = user_id
            session['email']   = email
            return jsonify({'success': True, 'message': 'Welcome back!'})
        else:
            return jsonify({'error': 'Incorrect password', 'field': 'password'}), 401
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        try: cur.close()
        except: pass
        put_conn(conn)


@auth_bp.route('/forgot-password', methods=['POST'])
def forgot_password():
    body         = request.get_json()
    email        = (body.get('email')        or '').strip().lower()
    new_password = (body.get('new_password') or '').strip()

    if not email or not new_password:
        return jsonify({'error': 'Email and new password are required'}), 400
    if not EMAIL_RE.match(email):
        return jsonify({'error': 'Please enter a valid email address', 'field': 'email'}), 400
    if len(new_password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters', 'field': 'password'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE email = %s', (email,))
        if cur.fetchone() is None:
            return jsonify({'error': 'No account found with this email', 'field': 'email'}), 404

        pw_hash = generate_password_hash(new_password)
        cur.execute('UPDATE users SET password_hash = %s WHERE email = %s', (pw_hash, email))
        conn.commit()
        return jsonify({'success': True, 'message': 'Password updated — you can now sign in.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        try: cur.close()
        except: pass
        put_conn(conn)


@auth_bp.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@auth_bp.route('/me', methods=['GET'])
def me():
    if 'user_id' in session:
        return jsonify({'logged_in': True, 'email': session.get('email')})
    return jsonify({'logged_in': False})
