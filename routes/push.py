from flask import Blueprint, request, jsonify, session
from db import get_conn, put_conn
from pywebpush import webpush, WebPushException
import json
import os

push_bp = Blueprint('push', __name__)

VAPID_PUBLIC_KEY    = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY   = os.getenv('VAPID_PRIVATE_KEY', '')
VAPID_CONTACT_EMAIL = os.getenv('VAPID_CONTACT_EMAIL', 'mailto:admin@example.com')


# GET /api/push/vapid-public-key
@push_bp.route('/vapid-public-key', methods=['GET'])
def vapid_public_key():
    return jsonify({'key': VAPID_PUBLIC_KEY})


# POST /api/push/subscribe
@push_bp.route('/subscribe', methods=['POST'])
def subscribe():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Not logged in'}), 401

    body     = request.get_json() or {}
    endpoint = (body.get('endpoint') or '').strip()
    keys     = body.get('keys') or {}
    p256dh   = (keys.get('p256dh') or '').strip()
    auth     = (keys.get('auth') or '').strip()

    if not endpoint or not p256dh or not auth:
        return jsonify({'error': 'endpoint and keys are required'}), 400

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (endpoint) DO UPDATE
                SET user_id = EXCLUDED.user_id, p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth
        """, (user_id, endpoint, p256dh, auth))
        conn.commit()
        cur.close()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        put_conn(conn)


def send_push_to_user(user_id, title, body, url='/replies'):
    """
    Send a Web Push notification to every device/browser this user has
    subscribed on. Silently no-ops if VAPID keys aren't configured, or if the
    user has no subscriptions — this must never block the caller (webhook).
    """
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            'SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = %s',
            (user_id,)
        )
        subs = cur.fetchall()
        cur.close()
    finally:
        put_conn(conn)

    payload = json.dumps({'title': title, 'body': body, 'url': url})

    for sub_id, endpoint, p256dh, auth in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': endpoint,
                    'keys': {'p256dh': p256dh, 'auth': auth},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={'sub': VAPID_CONTACT_EMAIL},
            )
        except WebPushException as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in (404, 410):
                # Subscription expired or was revoked by the browser — clean it up.
                conn = get_conn()
                try:
                    cur = conn.cursor()
                    cur.execute('DELETE FROM push_subscriptions WHERE id = %s', (sub_id,))
                    conn.commit()
                    cur.close()
                finally:
                    put_conn(conn)
            else:
                print(f'[push] webpush error for subscription {sub_id}: {e}')
        except Exception as e:
            print(f'[push] unexpected error for subscription {sub_id}: {e}')
