from flask import Blueprint, request, jsonify, session
import requests as http
from datetime import datetime, timedelta, timezone
from db import get_conn, put_conn

budget_bp = Blueprint('budget', __name__)
META_API = 'https://graph.facebook.com/v22.0'

# Fixed display order for conversation categories — colors on the frontend
# are assigned to these positions, so the mapping never shifts even if a
# given day has zero cost in one category.
CATEGORY_ORDER = ['MARKETING', 'UTILITY', 'AUTHENTICATION', 'SERVICE']


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


# GET /api/budget/spend?days=30
#
# Pulls real per-day WhatsApp spend from Meta's Conversation Analytics for
# this WABA, deliberately NOT computed from our own send_logs — Meta's
# per-category/per-country pricing changes and varies, and a locally-guessed
# number would drift from the tenant's actual bill. This is only as accurate
# as what Meta's Graph API returns for this specific WABA/token: it requires
# a payment method already attached to the WABA in Business Manager, and the
# exact response shape below is based on Meta's documented Conversation
# Analytics API but has not been verified against a live account. If Meta's
# error message here doesn't make sense, or the numbers look wrong, the
# `dimensions`/field names below are the first thing to check against
# current Meta docs.
@budget_bp.route('/spend', methods=['GET'])
def spend():
    user_id = session.get('user_id')
    creds = get_wa_credentials(user_id)
    if not creds:
        return jsonify({'error': 'WhatsApp not connected'}), 400

    try:
        days = int(request.args.get('days', 30))
    except ValueError:
        days = 30
    days = max(1, min(days, 90))

    end_dt   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start_dt = end_dt - timedelta(days=days)

    try:
        res = http.get(
            f"{META_API}/{creds['waba_id']}",
            params={
                'fields': (
                    'currency,'
                    f"conversation_analytics.start({int(start_dt.timestamp())})"
                    f".end({int(end_dt.timestamp())})"
                    '.granularity(DAILY)'
                    '.dimensions(["conversation_category"])'
                ),
            },
            headers={'Authorization': f"Bearer {creds['access_token']}"},
            timeout=15
        )
        data = res.json()
        if 'error' in data:
            return jsonify({'error': data['error'].get('message', 'Meta API error')}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Response shape per Meta's docs: conversation_analytics.data[0].data_points[],
    # each point like {start, end, conversation, cost, conversation_category, ...}.
    # Fail loudly (not silently show "$0 spent") if that shape doesn't match —
    # a budget page silently showing zero when the real answer is unknown is
    # worse than surfacing an explicit error.
    try:
        blocks = data.get('conversation_analytics', {}).get('data', [])
        points = []
        for block in blocks:
            points.extend(block.get('data_points', []))
    except Exception as e:
        return jsonify({'error': f"Unexpected response shape from Meta: {e}"}), 502

    by_date = {}
    for p in points:
        ts = p.get('start')
        if ts is None:
            continue
        date_key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%d')
        entry = by_date.setdefault(date_key, {
            'date': date_key, 'total_cost': 0.0, 'total_conversations': 0, 'by_category': {}
        })
        cat   = (p.get('conversation_category') or 'OTHER').upper()
        cost  = float(p.get('cost') or 0)
        convs = int(p.get('conversation') or 0)
        entry['total_cost'] += cost
        entry['total_conversations'] += convs
        entry['by_category'][cat] = entry['by_category'].get(cat, 0) + cost

    days_list = sorted(by_date.values(), key=lambda d: d['date'])
    for d in days_list:
        d['total_cost'] = round(d['total_cost'], 2)
        d['by_category'] = {k: round(v, 2) for k, v in d['by_category'].items()}

    grand_total         = round(sum(d['total_cost'] for d in days_list), 2)
    grand_conversations  = sum(d['total_conversations'] for d in days_list)

    return jsonify({
        'success': True,
        'currency': data.get('currency') or 'USD',
        'days': days_list,
        'category_order': CATEGORY_ORDER,
        'grand_total': grand_total,
        'grand_conversations': grand_conversations,
    })
