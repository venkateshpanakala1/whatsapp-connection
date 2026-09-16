"""Admin-only bulk email delivery through the Gmail API."""
import base64
import csv
import io
import os
import re
import time
from email.message import EmailMessage

from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename

from routes.auth import admin_required

email_bp = Blueprint('email', __name__)

EMAIL_RE = re.compile(r'(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])', re.I)
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_RECIPIENTS = 500


def _emails_from_upload(file_storage):
    """Return unique email addresses from a CSV or xlsx, regardless of column name."""
    raw = file_storage.read()
    if not raw:
        raise ValueError('The uploaded file is empty.')
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('File must be 5 MB or smaller.')

    name = secure_filename(file_storage.filename or '').lower()
    if name.endswith('.csv'):
        rows = csv.reader(io.StringIO(raw.decode('utf-8-sig', errors='replace')))
    elif name.endswith('.xlsx'):
        try:
            import openpyxl
            workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            rows = workbook.active.iter_rows(values_only=True)
        except Exception as exc:
            raise ValueError('Could not read the Excel file. Upload a valid .xlsx file.') from exc
    else:
        raise ValueError('Only CSV and Excel (.xlsx) files are supported.')

    recipients, seen = [], set()
    for row in rows:
        for value in row:
            for address in EMAIL_RE.findall(str(value or '')):
                address = address.lower()
                if address not in seen:
                    seen.add(address)
                    recipients.append(address)
    if not recipients:
        raise ValueError('No valid email addresses were found in the file.')
    if len(recipients) > MAX_RECIPIENTS:
        raise ValueError(f'A maximum of {MAX_RECIPIENTS} recipients can be sent per upload.')
    return recipients


GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.send']


def _oauth_client_path():
    path = os.getenv('GOOGLE_OAUTH_CLIENT_SECRETS', '').strip()
    if not path:
        raise RuntimeError('Email is not configured. Set GOOGLE_OAUTH_CLIENT_SECRETS on the server.')
    if not os.path.isfile(path):
        raise RuntimeError('The Google OAuth credentials file configured on the server was not found.')
    return path


def _token_path():
    return os.getenv('GMAIL_OAUTH_TOKEN_FILE', 'gmail-oauth-token.json').strip()


def _gmail_service():
    token_path = _token_path()
    if not os.path.isfile(token_path):
        raise RuntimeError('Gmail is not connected yet. Select Connect Gmail first.')
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        credentials = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            with open(token_path, 'w', encoding='utf-8') as token_file:
                token_file.write(credentials.to_json())
        if not credentials.valid:
            raise RuntimeError('Your Gmail connection has expired. Connect Gmail again.')
        return build('gmail', 'v1', credentials=credentials, cache_discovery=False)
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(
            'Could not initialize Gmail. Connect Gmail again or verify the OAuth configuration.'
        ) from exc


@email_bp.route('/oauth/start', methods=['GET'])
@admin_required
def oauth_start():
    try:
        from google_auth_oauthlib.flow import Flow
        from flask import redirect, session
        flow = Flow.from_client_secrets_file(_oauth_client_path(), scopes=GMAIL_SCOPES)
        flow.redirect_uri = os.getenv('GMAIL_OAUTH_REDIRECT_URI', request.url_root.rstrip('/') + '/api/admin-email/oauth/callback')
        authorization_url, state = flow.authorization_url(access_type='offline', prompt='consent')
        session['gmail_oauth_state'] = state
        return redirect(authorization_url)
    except RuntimeError as exc:
        return jsonify({'error': str(exc)}), 400


@email_bp.route('/oauth/callback', methods=['GET'])
@admin_required
def oauth_callback():
    from flask import redirect, session
    if request.args.get('error'):
        return redirect('/admin?gmail_error=' + request.args['error'])
    if not session.get('gmail_oauth_state'):
        return redirect('/admin?gmail_error=missing_state')
    try:
        from google_auth_oauthlib.flow import Flow
        flow = Flow.from_client_secrets_file(
            _oauth_client_path(), scopes=GMAIL_SCOPES, state=session.pop('gmail_oauth_state')
        )
        flow.redirect_uri = os.getenv('GMAIL_OAUTH_REDIRECT_URI', request.url_root.rstrip('/') + '/api/admin-email/oauth/callback')
        flow.fetch_token(authorization_response=request.url)
        with open(_token_path(), 'w', encoding='utf-8') as token_file:
            token_file.write(flow.credentials.to_json())
        return redirect('/admin?gmail_connected=1')
    except Exception:
        return redirect('/admin?gmail_error=connection_failed')


@email_bp.route('/send-bulk', methods=['POST'])
@admin_required
def send_bulk():
    upload = request.files.get('file')
    subject = (request.form.get('subject') or '').strip()
    body = (request.form.get('body') or '').strip()
    if not upload:
        return jsonify({'error': 'Upload a CSV or .xlsx recipient file.'}), 400
    if not subject or not body:
        return jsonify({'error': 'Subject and message are required.'}), 400
    if len(subject) > 200 or len(body) > 20000:
        return jsonify({'error': 'Subject or message is too long.'}), 400
    try:
        recipients = _emails_from_upload(upload)
        service = _gmail_service()
    except (ValueError, RuntimeError) as exc:
        return jsonify({'error': str(exc)}), 400

    sent, failures = 0, []
    for recipient in recipients:
        message = EmailMessage()
        message['To'] = recipient
        # Gmail supplies the authenticated mailbox as From. An optional configured
        # alias can be used only if Gmail has already verified it for that account.
        sender = os.getenv('GMAIL_SENDER_EMAIL', '').strip()
        if sender:
            message['From'] = sender
        message['Subject'] = subject
        message.set_content(body)
        try:
            encoded = base64.urlsafe_b64encode(message.as_bytes()).decode('ascii')
            service.users().messages().send(userId='me', body={'raw': encoded}).execute()
            sent += 1
            # Stay comfortably below Gmail API per-user burst limits.
            time.sleep(0.05)
        except Exception as exc:
            failures.append({'email': recipient, 'error': str(exc)[:180]})

    return jsonify({
        'success': sent > 0,
        'total': len(recipients),
        'sent': sent,
        'failed': len(failures),
        'failures': failures[:20],
        'message': f'Sent {sent} of {len(recipients)} emails.'
    }), 200 if sent else 502
