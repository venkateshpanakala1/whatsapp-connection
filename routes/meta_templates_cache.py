"""Short-lived shared-in-process cache for WABA template list requests."""
import threading
import time

import requests as http

META_API = 'https://graph.facebook.com/v22.0'
CACHE_TTL_SECONDS = 30
_cache = {}
_lock = threading.Lock()


def get_meta_templates(waba_id, access_token):
    key = str(waba_id)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return cached[1], None

    templates = []
    url = f'{META_API}/{key}/message_templates'
    params = {
        'limit': 100,
        'fields': 'id,name,category,language,status,components,created_time,rejected_reason'
    }
    headers = {'Authorization': f'Bearer {access_token}'}
    try:
        while url:
            response = http.get(url, params=params, headers=headers, timeout=10)
            data = response.json()
            if 'error' in data:
                error = data['error']
                message = (error.get('error_user_msg') or error.get('message')
                           or 'Meta API request failed')
                return None, message
            templates.extend(data.get('data', []))
            url = data.get('paging', {}).get('next')
            params = {}
    except Exception as e:
        return None, str(e)

    with _lock:
        _cache[key] = (time.monotonic() + CACHE_TTL_SECONDS, templates)
    return templates, None


def invalidate_meta_templates(waba_id):
    with _lock:
        _cache.pop(str(waba_id), None)
