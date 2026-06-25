from flask import Blueprint, request, jsonify, Response, session
import requests as http
from db import get_conn, put_conn
import json
import time
import threading
import uuid

send_bp = Blueprint('send', __name__)
META_API = 'https://graph.facebook.com/v22.0'

_jobs = {}


def get_wa_credentials(user_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT access_token, waba_id, phone_number_id
            FROM whatsapp_connections WHERE status='active' AND user_id=%s
            ORDER BY id DESC LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {'access_token': row[0], 'waba_id': row[1], 'phone_number_id': row[2]}
    finally:
        put_conn(conn)


def get_contacts_by_file(source_file, user_id):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT phone, name FROM contacts WHERE user_id = %s AND source_file = %s ORDER BY id ASC',
            (user_id, source_file)
        )
        rows = cur.fetchall()
        cur.close()
        return [{'phone': r[0], 'name': r[1] or ''} for r in rows]
    finally:
        put_conn(conn)


def log_send(job_id, phone, name, template_name, status, user_id):
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO send_logs (user_id, job_id, phone, name, template_name, status)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (user_id, job_id, phone, name or '', template_name, status))
        conn.commit()
        cur.close()
    except Exception:
        pass
    finally:
        try: put_conn(conn)
        except: pass


def send_worker(job_id, contacts, template_name, template_lang, creds, delay, user_id):
    job         = _jobs[job_id]
    pause_event = job['pause_event']
    job['status'] = 'running'

    for i, contact in enumerate(contacts):
        pause_event.wait()

        phone = contact['phone']
        name  = contact.get('name', '')
        payload = {
            'messaging_product': 'whatsapp',
            'recipient_type':    'individual',
            'to':                phone,
            'type':              'template',
            'template': {
                'name':     template_name,
                'language': {'code': template_lang}
            }
        }
        try:
            res  = http.post(
                f"{META_API}/{creds['phone_number_id']}/messages",
                headers={
                    'Authorization': f"Bearer {creds['access_token']}",
                    'Content-Type':  'application/json'
                },
                json=payload,
                timeout=10
            )
            data = res.json()
            if 'error' in data:
                job['failed'] += 1
                if len(job['errors']) < 10:
                    job['errors'].append(f"{phone}: {data['error']['message']}")
                log_send(job_id, phone, name, template_name, 'failed', user_id)
            else:
                job['sent'] += 1
                log_send(job_id, phone, name, template_name, 'sent', user_id)
        except Exception as e:
            job['failed'] += 1
            log_send(job_id, phone, name, template_name, 'failed', user_id)

        job['current'] = i + 1

        elapsed = 0
        while elapsed < delay:
            pause_event.wait()
            time.sleep(0.5)
            elapsed += 0.5

    job['status'] = 'done'


# GET /api/send/templates
@send_bp.route('/templates', methods=['GET'])
def get_templates():
    user_id = session.get('user_id')
    creds = get_wa_credentials(user_id)
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400
    try:
        all_templates = []
        url    = f"{META_API}/{creds['waba_id']}/message_templates"
        params = {'limit': 100, 'fields': 'id,name,category,language,status,components'}
        headers = {'Authorization': f"Bearer {creds['access_token']}"}

        while url:
            res  = http.get(url, params=params, headers=headers, timeout=10)
            data = res.json()
            if 'error' in data:
                return jsonify({'error': data['error']['message']}), 400
            all_templates.extend(data.get('data', []))
            url    = data.get('paging', {}).get('next')
            params = {}

        approved = [
            t for t in all_templates
            if t.get('status') in ('APPROVED', 'PENDING')
            and not t.get('name', '').startswith('cr_')
        ]
        return jsonify({'success': True, 'templates': approved[:5], 'all': approved})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# POST /api/send/start
@send_bp.route('/start', methods=['POST'])
def start_send():
    user_id       = session.get('user_id')
    body          = request.get_json()
    source_file   = (body.get('source_file')   or '').strip()
    template_name = (body.get('template_name') or '').strip()
    template_lang = (body.get('template_lang') or 'en').strip()
    delay         = int(body.get('delay', 1))

    if not source_file or not template_name:
        return jsonify({'error': 'source_file and template_name are required'}), 400

    creds = get_wa_credentials(user_id)
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400

    contacts = get_contacts_by_file(source_file, user_id)
    if not contacts:
        return jsonify({'error': f'No contacts found in "{source_file}"'}), 400

    pause_event = threading.Event()
    pause_event.set()

    job_id       = str(uuid.uuid4())
    _jobs[job_id] = {
        'status':      'pending',
        'sent':        0,
        'failed':      0,
        'current':     0,
        'total':       len(contacts),
        'delay':       delay,
        'errors':      [],
        'pause_event': pause_event,
        'source_file': source_file,
        'template':    template_name,
    }

    t = threading.Thread(
        target=send_worker,
        args=(job_id, contacts, template_name, template_lang, creds, delay, user_id),
        daemon=True
    )
    t.start()

    return jsonify({'success': True, 'job_id': job_id, 'total': len(contacts)})


# POST /api/send/pause/<job_id>
@send_bp.route('/pause/<job_id>', methods=['POST'])
def pause_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    job['pause_event'].clear()
    job['status'] = 'paused'
    return jsonify({'success': True})


# POST /api/send/resume/<job_id>
@send_bp.route('/resume/<job_id>', methods=['POST'])
def resume_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    job['pause_event'].set()
    job['status'] = 'running'
    return jsonify({'success': True})


# GET /api/send/status/<job_id>
@send_bp.route('/status/<job_id>')
def job_status(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({'found': False}), 404
    return jsonify({
        'found':   True,
        'status':  job.get('status'),
        'current': job.get('current', 0),
        'total':   job.get('total', 0),
        'sent':    job.get('sent', 0),
        'failed':  job.get('failed', 0),
        'errors':  job.get('errors', []),
    })


# GET /api/send/progress/<job_id>  — SSE stream
@send_bp.route('/progress/<job_id>')
def progress(job_id):
    if job_id not in _jobs:
        return jsonify({'error': 'Job not found'}), 404

    def generate():
        while True:
            job  = _jobs.get(job_id, {})
            data = json.dumps({
                'status':  job.get('status'),
                'current': job.get('current', 0),
                'total':   job.get('total', 0),
                'sent':    job.get('sent', 0),
                'failed':  job.get('failed', 0),
                'errors':  job.get('errors', []),
            })
            yield f"data: {data}\n\n"
            if job.get('status') == 'done':
                break
            time.sleep(0.5)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )
