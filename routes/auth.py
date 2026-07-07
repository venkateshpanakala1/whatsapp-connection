from flask import Blueprint, request, jsonify, session
from werkzeug.security import generate_password_hash, check_password_hash
from db import get_conn, put_conn

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['POST'])
def login():
    body     = request.get_json()
    email    = (body.get('email')    or '').strip().lower()
    password = (body.get('password') or '').strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute('SELECT id, password_hash FROM users WHERE email = %s', (email,))
        row = cur.fetchone()

        if row is None:
            # New email → auto-create account
            pw_hash = generate_password_hash(password)
            cur.execute(
                'INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id',
                (email, pw_hash)
            )
            user_id = cur.fetchone()[0]
            conn.commit()
            session.permanent = True
            session['user_id'] = user_id
            session['email']   = email
            return jsonify({'success': True, 'new_user': True,  'message': 'Account created successfully'})
        else:
            user_id, pw_hash = row
            if check_password_hash(pw_hash, password):
                session.permanent = True
                session['user_id'] = user_id
                session['email']   = email
                return jsonify({'success': True, 'new_user': False, 'message': 'Welcome back!'})
            else:
                return jsonify({'error': 'Incorrect password'}), 401
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
