from flask import Flask, send_from_directory, session, redirect
from flask_cors import CORS
from functools import wraps
import os
from dotenv import load_dotenv
from db import init_db
from routes.whatsapp import whatsapp_bp
from routes.templates import templates_bp
from routes.contacts import contacts_bp
from routes.send import send_bp
from routes.webhook import webhook_bp
from routes.replies import replies_bp, resume_pending_counter_replies, backfill_reply_contact_names
from routes.auth import auth_bp

load_dotenv()

app = Flask(__name__, static_folder='public', static_url_path='')
app.secret_key = os.getenv('SECRET_KEY', 'wa-saas-dev-secret-change-in-prod')
CORS(app, supports_credentials=True)

app.register_blueprint(whatsapp_bp, url_prefix='/api/whatsapp')
app.register_blueprint(templates_bp, url_prefix='/api/templates')
app.register_blueprint(contacts_bp,  url_prefix='/api/contacts')
app.register_blueprint(send_bp,      url_prefix='/api/send')
app.register_blueprint(webhook_bp)
app.register_blueprint(replies_bp,   url_prefix='/api/replies')
app.register_blueprint(auth_bp,      url_prefix='/api/auth')

init_db()
backfill_reply_contact_names()
resume_pending_counter_replies()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        return view(*args, **kwargs)
    return wrapped


@app.route('/login')
def login_page():
    if 'user_id' in session:
        return redirect('/')
    return send_from_directory('public', 'login.html')

@app.route('/')
@login_required
def index():
    return send_from_directory('public', 'index.html')

@app.route('/templates')
@login_required
def templates():
    return send_from_directory('public', 'templates.html')

@app.route('/contacts')
@login_required
def contacts():
    return send_from_directory('public', 'contacts.html')

@app.route('/send')
@login_required
def send_page():
    return send_from_directory('public', 'send.html')

@app.route('/replies')
@login_required
def replies_page():
    return send_from_directory('public', 'replies.html')


@app.route('/favicon.ico')
def favicon():
    return '', 204


@app.route('/privacy-policy')
def privacy_policy():
    return '''
    <h1>Privacy Policy</h1>
    <p>We collect WhatsApp phone numbers and message data solely to provide our messaging service.
    Data is stored securely and never shared with third parties.
    Contact us at thinkaboutneighbour@gmail.com for any privacy concerns.</p>
    ''', 200

@app.route('/terms')
def terms():
    return '''
    <h1>Terms of Service</h1>
    <p>By using this service you agree to use it only for lawful WhatsApp Business communication.
    We reserve the right to suspend accounts that violate WhatsApp policies.</p>
    ''', 200

@app.route('/data-deletion')
def data_deletion():
    return '''
    <h1>Data Deletion Instructions</h1>
    <p>To request deletion of your data, email us at thinkaboutneighbour@gmail.com
    with your registered WhatsApp number. We will process your request within 30 days.</p>
    ''', 200


if __name__ == '__main__':
    port = int(os.getenv('PORT', 3000))
    print(f'Server running at http://localhost:{port}')
    app.run(host='0.0.0.0', port=port, debug=True)
