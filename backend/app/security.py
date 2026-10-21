"""Single-owner authentication. Browser sessions never contain broker secrets."""
import hashlib
import hmac
import os
import secrets
import time
import base64
import struct
from collections import defaultdict, deque
from fastapi import HTTPException, Request, Response, Depends
from sqlalchemy import select
from .database import get_db
from .models import Owner, UserSession

COOKIE = 'pot_session'
SESSION_HOURS = int(os.getenv('SESSION_HOURS', '12'))
SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
SAME_SITE = os.getenv('COOKIE_SAMESITE', 'lax')
ALLOWED_ORIGINS = [o.strip().rstrip('/') for o in os.getenv('ALLOWED_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000').split(',') if o.strip()]
_attempts = defaultdict(deque)

def password_hash(password):
    salt = secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f'scrypt${salt.hex()}${result.hex()}'

def verify_password(password, value):
    try:
        _, salt, expected = value.split('$')
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def verify_totp(code,secret=None,now=None):
    secret=secret or os.getenv('OWNER_TOTP_SECRET')
    if not secret:return True
    if not code or not str(code).isdigit() or len(str(code))!=6:return False
    try:key=base64.b32decode(secret.upper().replace(' ',''),casefold=True)
    except (ValueError,TypeError):return False
    counter=int((now if now is not None else time.time())//30)
    for offset in (-1,0,1):
        mac=hmac.new(key,struct.pack('>Q',counter+offset),hashlib.sha1).digest()
        index=mac[-1]&15
        expected=f'{(struct.unpack(">I",mac[index:index+4])[0]&0x7fffffff)%1000000:06d}'
        if hmac.compare_digest(str(code),expected):return True
    return False

def rate_limit(request, group='login', limit=8, seconds=60):
    key = (request.client.host if request.client else 'unknown', group)
    queue = _attempts[key]
    now = time.monotonic()
    while queue and queue[0] < now - seconds:
        queue.popleft()
    if len(queue) >= limit:
        raise HTTPException(429, 'Too many attempts. Please wait one minute.')
    queue.append(now)

def validate_origin(request):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') not in ALLOWED_ORIGINS:
        raise HTTPException(403, 'This website origin is not allowed.')
    if request.headers.get('sec-fetch-site') == 'cross-site' and not origin:
        raise HTTPException(403, 'Origin header required for a cross-site action.')

def session_from_cookie(request, db):
    token = request.cookies.get(COOKIE)
    session = db.get(UserSession, digest(token)) if token else None
    if session and session.expires_at > time.time():
        return session
    return None

def require_session(request: Request, db=Depends(get_db)):
    session = session_from_cookie(request, db)
    if not session:
        raise HTTPException(401, 'Please sign in.')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        validate_origin(request)
        if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), session.csrf_token):
            raise HTTPException(403, 'Your security token expired. Refresh and sign in again.')
    return session

def create_session(db, response: Response):
    raw = secrets.token_urlsafe(48)
    session = UserSession(token_hash=digest(raw), csrf_token=secrets.token_urlsafe(32), expires_at=time.time()+SESSION_HOURS*3600)
    db.add(session)
    db.commit()
    response.set_cookie(COOKIE, raw, max_age=SESSION_HOURS*3600, httponly=True, secure=SECURE, samesite=SAME_SITE, path='/')
    return {'authenticated': True, 'setup_required': False, 'csrf_token': session.csrf_token}
