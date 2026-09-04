# =========================================================
# anova — api.py
# طبقة HTTP بسيطة فوق نفس منطق تحميل الفيديو الموجود في bot.py،
# عشان أي موقع ويب (زي Pulse) يقدر يكلّم البوت مباشرة من غير تليجرام.
#
# الفكرة: بدل ما نكرر منطق yt-dlp، بنستورد download_media() ودوالها
# المساعدة من bot.py زي ما هي، ونغلّفها في endpoints بسيطة.
# الملف ده لازم يتحط جنب bot.py في نفس المجلد.
# =========================================================

import os
import time
import uuid
import threading
from collections import defaultdict

from flask import Flask, request, jsonify, send_file, after_this_request

# استيراد المنطق الموجود فعلاً في bot.py — من غير ما نكرره
from bot import download_media, get_file_size_mb, DOWNLOAD_PATH

app = Flask(__name__)

# ============================
# CORS — نسمح بس لدومين Pulse بالنداء على الـ API ده
# ============================
# غيّر القيمة دي لدومين موقعك الفعلي بعد النشر (أو "*" مؤقتًا وقت التجربة
# بس، ومش وقت الإنتاج، عشان أي موقع تاني هيقدر يستهلك سيرفرك ويكلفك).
ALLOWED_ORIGIN = os.getenv('ALLOWED_ORIGIN', 'https://pythouz.github.io')


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = ALLOWED_ORIGIN
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


# ============================
# حماية بسيطة من الإغراق (Rate limiting) — لكل IP
# ============================
# مهم جدًا: التحميل عملية مكلفة (باندويدث + معالجة)، والـ endpoint ده
# عام ومفتوح لأي حد يعرف الرابط. من غير حد أقصى، أي حد يقدر يغرق
# السيرفر بيك بمئات الطلبات ويستهلك الباندويدث/التكلفة بتاعتك.
RATE_LIMIT_MAX = 6          # 6 طلبات
RATE_LIMIT_WINDOW = 120     # كل دقيقتين لكل IP

_rate_limit_store = defaultdict(list)
_rate_limit_lock = threading.Lock()


def is_rate_limited(ip):
    now = time.time()
    with _rate_limit_lock:
        recent = [t for t in _rate_limit_store[ip] if now - t < RATE_LIMIT_WINDOW]
        if len(recent) >= RATE_LIMIT_MAX:
            _rate_limit_store[ip] = recent
            return True
        recent.append(now)
        _rate_limit_store[ip] = recent
        return False


# ============================
# روابط تحميل مؤقتة (بدل ما نرجّع مسار السيرفر مباشرة)
# ============================
_download_tokens = {}  # token -> (file_path, expires_at)
TOKEN_TTL_SECONDS = 15 * 60  # 15 دقيقة كفاية للمستخدم يضغط "تحميل"


def _cleanup_expired_tokens():
    now = time.time()
    expired = [t for t, (_, exp) in _download_tokens.items() if exp < now]
    for t in expired:
        file_path, _ = _download_tokens.pop(t, (None, None))
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})


@app.route('/api/download', methods=['POST', 'OPTIONS'])
def api_download():
    if request.method == 'OPTIONS':
        return '', 204  # رد فارغ لطلب CORS preflight

    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if is_rate_limited(ip):
        return jsonify({'error': 'محاولات كتيرة، حاول تاني بعد شوية'}), 429

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    media_type = data.get('media_type', 'video')
    quality = data.get('quality')  # مثلاً '720p' أو None

    if not url or not url.lower().startswith(('http://', 'https://')):
        return jsonify({'error': 'الرابط مش صالح'}), 400
    if media_type not in ('video', 'audio'):
        return jsonify({'error': 'نوع الوسائط لازم يكون video أو audio'}), 400

    try:
        message, file_path = download_media(url, media_type=media_type, video_quality=quality)
    except Exception as e:
        return jsonify({'error': f'فشل التحميل: {e}'}), 500

    if not file_path or not os.path.exists(file_path):
        return jsonify({'error': message or 'تعذر تحميل الرابط ده'}), 502

    _cleanup_expired_tokens()

    size_mb = round(get_file_size_mb(file_path), 1)
    token = uuid.uuid4().hex
    _download_tokens[token] = (file_path, time.time() + TOKEN_TTL_SECONDS)

    return jsonify({
        'title': message,
        'size_mb': size_mb,
        'download_url': f'/api/file/{token}',
        'filename': os.path.basename(file_path)
    })


@app.route('/api/file/<token>', methods=['GET'])
def api_file(token):
    entry = _download_tokens.get(token)
    if not entry:
        return jsonify({'error': 'الرابط منتهي الصلاحية أو غير موجود'}), 404

    file_path, expires_at = entry
    if time.time() > expires_at or not os.path.exists(file_path):
        _download_tokens.pop(token, None)
        return jsonify({'error': 'الرابط منتهي الصلاحية'}), 404

    @after_this_request
    def cleanup(response):
        # نمسح الملف والتوكن بعد ما يتبعت فعليًا، عشان مساحة السيرفر
        # ما تمتلئش من ملفات قديمة محدش رجع يحمّلها.
        _download_tokens.pop(token, None)
        try:
            os.remove(file_path)
        except OSError:
            pass
        return response

    return send_file(file_path, as_attachment=True, download_name=os.path.basename(file_path))


def run_api(host='0.0.0.0', port=None):
    port = port or int(os.getenv('PORT', 8080))
    app.run(host=host, port=port, threaded=True)


def start_api_in_background():
    """بتشغّل الـ API في Thread منفصل، عشان تشتغل جنب البوت مش بدله."""
    thread = threading.Thread(target=run_api, daemon=True)
    thread.start()
    return thread
