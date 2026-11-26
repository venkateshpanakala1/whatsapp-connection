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


def get_bulk_sent_count(user_id, start_dt, end_dt):
    """Actual messages sent via Bulk Send in this range — separate from
    Meta's own billed-message count above, which also includes counter-
    replies and other conversation types. send_logs.sent_at is a naive
    UTC TIMESTAMP (Postgres's NOW()), so the tz-aware start/end passed in
    have their tzinfo stripped to compare correctly against it."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM send_logs
            WHERE user_id = %s AND status = 'sent'
              AND sent_at >= %s AND sent_at < %s
        """, (user_id, start_dt.replace(tzinfo=None), end_dt.replace(tzinfo=None)))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else 0
    finally:
        put_conn(conn)


# GET /api/budget/spend?days=30
#
# Pulls real per-day WhatsApp spend directly from Meta for this WABA,
# deliberately NOT computed from our own send_logs — Meta's per-category/
# per-country pricing changes and varies, and a locally-guessed number would
# drift from the tenant's actual bill.
#
# Requests per-message pricing analytics without the legacy conversation_analytics
# field, which can cause Meta to reject the combined field expansion.
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

    start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())

    try:
        res = http.get(
            f"{META_API}/{creds['waba_id']}",
            params={
                'fields': (
                    'currency,'
                    f"pricing_analytics.start({start_ts}).end({end_ts})"
                    '.granularity(DAILY).dimensions(["PRICING_CATEGORY","PRICING_TYPE"])'
                ),
            },
            headers={'Authorization': f"Bearer {creds['access_token']}"},
            timeout=15
        )
        data = res.json()
        print(f"[budget] Meta raw response for waba_id={creds['waba_id']}: {data}")
        if 'error' in data:
            return jsonify({'error': data['error'].get('message', 'Meta API error')}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    # Pricing analytics returns cost and volume data points by date.
    try:
        points = []
        for block in data.get('pricing_analytics', {}).get('data', []):
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
        cat = (p.get('pricing_category') or 'OTHER').upper()
        cost  = float(p.get('cost') or 0)
        convs = int(p.get('conversation') or p.get('volume') or 0)
        entry['total_cost'] += cost
        entry['total_conversations'] += convs
        entry['by_category'][cat] = entry['by_category'].get(cat, 0) + cost

    days_list = sorted(by_date.values(), key=lambda d: d['date'])
    for d in days_list:
        d['total_cost'] = round(d['total_cost'], 2)
        d['by_category'] = {k: round(v, 2) for k, v in d['by_category'].items()}

    grand_total         = round(sum(d['total_cost'] for d in days_list), 2)
    grand_conversations  = sum(d['total_conversations'] for d in days_list)
    bulk_sent_count      = get_bulk_sent_count(user_id, start_dt, end_dt)

    response = {
        'success': True,
        'currency': data.get('currency') or 'USD',
        'days': days_list,
        'category_order': CATEGORY_ORDER,
        'grand_total': grand_total,
        'grand_conversations': grand_conversations,
        'bulk_sent_count': bulk_sent_count,
    }
    # Temporary: while this endpoint is still unverified against a live
    # account, surface Meta's exact raw response whenever we found nothing,
    # so it's visible right on the page — no Railway log access needed to
    # tell whether Meta returned truly empty analytics vs. something our
    # parsing missed.
    if not days_list:
        response['_debug_raw_meta_response'] = data
    return jsonify(response)
