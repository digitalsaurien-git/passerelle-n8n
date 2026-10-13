"""Passerelle IA — single-account, single-worker prototype behind HTTPS proxy."""
import base64, hashlib, hmac, io, json, os, secrets, sqlite3, threading, time, urllib.request, urllib.error, zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from flask import Flask, request, jsonify, send_file, abort
from werkzeug.exceptions import HTTPException

ROOT = Path(__file__).parent
DATA = Path(os.getenv('DATA_DIR', '/data')); DATA.mkdir(parents=True, exist_ok=True)
PASSWORD = os.environ.get('APP_PASSWORD', '')
if len(PASSWORD) < 16:
    raise RuntimeError('APP_PASSWORD doit contenir au moins 16 caractères.')
ORIGINS = {s.strip().rstrip('/') for s in os.getenv('ALLOWED_ORIGINS', '').split(',') if s.strip()}
if not ORIGINS:
    raise RuntimeError('ALLOWED_ORIGINS doit contenir les URL exactes de votre application.')
SECURE = os.getenv('COOKIE_SECURE', 'true').lower() == 'true'
MODE = os.getenv('APP_MODE', 'demo')
WEBHOOK = os.getenv('N8N_WEBHOOK_URL', '')
TOKEN = os.getenv('N8N_WEBHOOK_TOKEN', '')
if MODE not in ('demo', 'n8n'):
    raise RuntimeError('APP_MODE: demo ou n8n uniquement')
if MODE == 'n8n' and (not WEBHOOK.startswith(('https://','http://')) or not TOKEN):
    raise RuntimeError('Configurer N8N_WEBHOOK_URL et N8N_WEBHOOK_TOKEN.')
app = Flask(__name__, static_folder='public', static_url_path='/static')
app.config['MAX_CONTENT_LENGTH'] = 12 * 1024 * 1024
pool = ThreadPoolExecutor(max_workers=2)
limits = []; login_lock = threading.Lock()
MIMES = {'pdf':'application/pdf','png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg','webp':'image/webp','txt':'text/plain','md':'text/plain','csv':'text/csv','json':'application/json','docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}

def db():
    c = sqlite3.connect(DATA / 'passerelle.sqlite', timeout=20)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

with db() as c:
    c.executescript('''PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, expires REAL);
    CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,title TEXT,updated REAL);
    CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,chat TEXT REFERENCES chats(id) ON DELETE CASCADE,role TEXT,text TEXT,created REAL);
    CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,message TEXT REFERENCES messages(id) ON DELETE CASCADE,name TEXT,mime TEXT,data BLOB);
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,chat TEXT REFERENCES chats(id) ON DELETE CASCADE,message TEXT REFERENCES messages(id) ON DELETE CASCADE,status TEXT,error TEXT,created REAL);
    ''')
    if 'provider' not in {row[1] for row in c.execute('PRAGMA table_info(messages)')}:
        c.execute("ALTER TABLE messages ADD COLUMN provider TEXT NOT NULL DEFAULT ''")
    c.execute("UPDATE jobs SET status='error',error='Serveur redémarré pendant le traitement. Vous pouvez réessayer.' WHERE status='pending'")

@app.before_request
def protect():
    if request.method in ('POST','DELETE','PUT','PATCH') and request.headers.get('Origin','') not in ORIGINS:
        return jsonify(error='Origine non autorisée. Vérifiez ALLOWED_ORIGINS.'),403
    if request.path.startswith('/api/') and request.path != '/api/login':
        token = hashlib.sha256(request.cookies.get('passerelle_session','').encode()).hexdigest()
        with db() as c:
            session = c.execute('SELECT 1 FROM sessions WHERE token=? AND expires>?',(token,time.time())).fetchone()
        if not session: return jsonify(error='Connectez-vous pour continuer.'),401

@app.after_request
def headers(r):
    r.headers['Cache-Control']='no-store'
    r.headers['X-Content-Type-Options']='nosniff'
    r.headers['Referrer-Policy']='no-referrer'
    r.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    r.headers['Permissions-Policy']='microphone=(self), camera=()'
    if SECURE: r.headers['Strict-Transport-Security']='max-age=31536000'
    return r

@app.errorhandler(HTTPException)
def http_error(e):
    return jsonify(error='Envoi trop volumineux (10 Mio maximum).' if e.code==413 else e.description), e.code

@app.get('/')
def index(): return app.send_static_file('index.html')
@app.get('/health')
def health(): return jsonify(status='ok')

@app.post('/api/login')
def login():
    with login_lock:
        now = time.time(); limits[:] = [t for t in limits if t>now-900]
        if len(limits)>=10: return jsonify(error='Trop de tentatives. Réessayez dans 15 minutes.'),429
        supplied=(request.get_json(silent=True) or {}).get('password','')
        if not isinstance(supplied,str) or not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(),hashlib.sha256(PASSWORD.encode()).digest()):
            limits.append(now); return jsonify(error='Mot de passe incorrect.'),401
        limits.clear()
    token=secrets.token_urlsafe(32)
    with db() as c:
        c.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
        c.execute('INSERT INTO sessions VALUES (?,?)',(hashlib.sha256(token.encode()).hexdigest(),time.time()+7*86400))
    r=jsonify(ok=True); r.set_cookie('passerelle_session',token,httponly=True,secure=SECURE,samesite='Strict',max_age=7*86400)
    return r

@app.post('/api/logout')
def logout():
    with db() as c: c.execute('DELETE FROM sessions WHERE token=?',(hashlib.sha256(request.cookies.get('passerelle_session','').encode()).hexdigest(),))
    r=jsonify(ok=True); r.delete_cookie('passerelle_session',secure=SECURE,samesite='Strict'); return r

@app.get('/api/info')
def info(): return jsonify(mode=MODE, webhookConfigured=bool(WEBHOOK), version='1.0.0')
@app.get('/api/chats')
def chats():
    with db() as c: return jsonify([dict(x) for x in c.execute('SELECT * FROM chats ORDER BY updated DESC')])
@app.post('/api/chats')
def create_chat():
    cid=secrets.token_hex(16)
    with db() as c: c.execute('INSERT INTO chats VALUES (?,?,?)',(cid,'Nouvelle conversation',time.time()))
    return jsonify(id=cid),201

def extract_office_text(name, mime, raw):
    """Small dependency-free extractor so both providers receive useful DOCX/XLSX text."""
    try:
        if mime.startswith('text/') or mime == 'application/json':
            return raw.decode('utf-8', errors='replace')[:300000]
        if name.lower().endswith('.docx'):
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if sum(i.file_size for i in z.infolist()) > 50 * 1024 * 1024: return ''
                if z.getinfo('word/document.xml').file_size > 20 * 1024 * 1024: return ''
                xml = ET.fromstring(z.read('word/document.xml'))
                paragraphs = []
                for p in xml.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'):
                    text = ''.join(t.text or '' for t in p.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t'))
                    if text.strip(): paragraphs.append(text)
                return '\n'.join(paragraphs)[:300000]
        if name.lower().endswith('.xlsx'):
            ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if sum(i.file_size for i in z.infolist()) > 50 * 1024 * 1024: return ''
                shared = []
                if 'xl/sharedStrings.xml' in z.namelist():
                    root = ET.fromstring(z.read('xl/sharedStrings.xml'))
                    shared = [''.join(t.text or '' for t in si.iter(ns+'t')) for si in root.iter(ns+'si')]
                lines = []
                for path in sorted(p for p in z.namelist() if p.startswith('xl/worksheets/sheet') and p.endswith('.xml')):
                    lines.append('['+Path(path).stem+']')
                    root = ET.fromstring(z.read(path))
                    for row in root.iter(ns+'row'):
                        values = []
                        for cell in row.iter(ns+'c'):
                            v = cell.find(ns+'v')
                            value = '' if v is None else (v.text or '')
                            if cell.get('t') == 's' and value.isdigit() and int(value) < len(shared): value = shared[int(value)]
                            values.append((cell.get('r') or '')+'='+value)
                        if values: lines.append(' | '.join(values))
                        if sum(map(len,lines)) > 300000: break
                return '\n'.join(lines)[:300000]
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError):
        return ''
    return ''

def chat_data(cid, include_data=False):
    with db() as c:
        chat=c.execute('SELECT * FROM chats WHERE id=?',(cid,)).fetchone()
        if not chat: abort(404,description='Conversation introuvable.')
        messages=[]
        for row in c.execute('SELECT * FROM messages WHERE chat=? ORDER BY created,rowid',(cid,)):
            msg=dict(row); msg['attachments']=[]
            for f in c.execute('SELECT * FROM files WHERE message=?',(row['id'],)):
                item={'id':f['id'],'name':f['name'],'mime':f['mime'],'size':len(f['data'])}
                if include_data:
                    item['data']=base64.b64encode(f['data']).decode()
                    extracted=extract_office_text(f['name'],f['mime'],f['data'])
                    if extracted: item['extractedText']=extracted
                msg['attachments'].append(item)
            messages.append(msg)
        jobs=[dict(j) for j in c.execute('SELECT * FROM jobs WHERE chat=? ORDER BY created DESC',(cid,))]
    return dict(chat, messages=messages,jobs=jobs)

@app.get('/api/chats/<cid>')
def get_chat(cid): return jsonify(chat_data(cid))
@app.delete('/api/chats/<cid>')
def delete_chat(cid):
    with db() as c:
        if c.execute("SELECT 1 FROM jobs WHERE chat=? AND status='pending'",(cid,)).fetchone(): abort(409,description='Attendez la fin du traitement.')
        c.execute('DELETE FROM chats WHERE id=?',(cid,))
    return jsonify(ok=True)
@app.get('/api/files/<fid>')
def file_download(fid):
    with db() as c: f=c.execute('SELECT * FROM files WHERE id=?',(fid,)).fetchone()
    if not f: abort(404)
    return send_file(io.BytesIO(f['data']),mimetype=f['mime'],download_name=f['name'],as_attachment=True)

@app.post('/api/chats/<cid>/messages')
def message(cid):
    text=request.form.get('text','').strip()
    provider=request.form.get('provider','openai').strip().lower()
    if provider not in ('openai','claude'): abort(400,description='Modèle IA invalide.')
    if len(text)>40000: abort(400,description='Texte limité à 40 000 caractères par message.')
    uploads=request.files.getlist('files')
    if len(uploads)>5: abort(400,description='5 pièces jointes maximum par message.')
    if not text and not uploads: abort(400,description='Ajoutez un message ou une pièce jointe.')
    files=[]; total=0
    for f in uploads:
        name=(f.filename or 'fichier').replace('\\','/').split('/')[-1][:180]
        ext=name.rsplit('.',1)[-1].lower()
        if ext not in MIMES: abort(400,description=f'Format non pris en charge : {name}. Utilisez PDF, image, texte, CSV, DOCX ou XLSX.')
        data=f.read(10*1024*1024+1); total+=len(data)
        if not data or len(data)>8*1024*1024: abort(400,description='Chaque fichier doit contenir entre 1 octet et 8 Mio.')
        files.append((secrets.token_hex(16),name,MIMES[ext],data))
    if total>10*1024*1024: abort(400,description='10 Mio maximum par envoi.')
    mid=secrets.token_hex(16); jid=secrets.token_hex(16); now=time.time()
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM chats WHERE id=?',(cid,)).fetchone(): abort(404)
        if c.execute("SELECT 1 FROM jobs WHERE chat=? AND status='pending'",(cid,)).fetchone(): abort(409,description='Une réponse est déjà en cours.')
        if c.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]>=4: abort(429,description='Serveur occupé. Réessayez dans un instant.')
        size=c.execute('SELECT COALESCE(SUM(length(data)),0) FROM files JOIN messages ON message=messages.id WHERE chat=?',(cid,)).fetchone()[0]
        count=c.execute('SELECT COUNT(*) FROM messages WHERE chat=?',(cid,)).fetchone()[0]
        if size+total>10*1024*1024 or count>=60: abort(400,description='Cette conversation a atteint sa limite (10 Mio de fichiers ou 60 messages). Ouvrez une nouvelle conversation.')
        c.execute('INSERT INTO messages(id,chat,role,text,created,provider) VALUES (?,?,?,?,?,?)',(mid,cid,'user',text,now,provider))
        c.executemany('INSERT INTO files VALUES (?,?,?,?,?)',[(fid,mid,name,mime,data) for fid,name,mime,data in files])
        c.execute('UPDATE chats SET updated=?,title=CASE WHEN title=? THEN ? ELSE title END WHERE id=?',(now,'Nouvelle conversation',(text or files[0][1])[:65],cid))
        c.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',(jid,cid,mid,'pending','',now))
    pool.submit(run_job,jid,cid)
    return jsonify(jobId=jid),202

@app.post('/api/chats/<cid>/retry')
def retry(cid):
    with db() as c:
        c.execute('BEGIN IMMEDIATE')
        job=c.execute('SELECT * FROM jobs WHERE chat=? ORDER BY created DESC LIMIT 1',(cid,)).fetchone()
        if not job or job['status']!='error': abort(409,description='Aucun traitement en échec à relancer.')
        if c.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]>=4: abort(429,description='Serveur occupé.')
        c.execute("UPDATE jobs SET status='pending',error='' WHERE id=?",(job['id'],))
    pool.submit(run_job,job['id'],cid)
    return jsonify(jobId=job['id']),202

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def run_job(jid,cid):
    try:
        data=chat_data(cid,True)
        provider=(data['messages'][-1].get('provider') or 'openai').lower()
        payload={'protocolVersion':1,'requestId':jid,'conversationId':cid,'provider':provider,'messages':data['messages']}
        if MODE=='demo':
            last=data['messages'][-1]; files=[a['name'] for m in data['messages'] for a in m['attachments']]
            answer='Mode démonstration — aucun modèle IA appelé.\n\nModèle choisi : '+provider.upper()+'\nVotre message : '+(last['text'] or '(pièces jointes seules)')+'\n\nHistorique reçu : '+str(len(data['messages']))+' messages.\nPièces conservées : '+(', '.join(files) or 'aucune')+'.\n\nVous pouvez répondre dans cette conversation et ajouter un document. Pour obtenir une vraie analyse, activez le mode n8n.'
        else:
            body=json.dumps(payload,ensure_ascii=False).encode()
            if len(body)>38*1024*1024: raise ValueError('Conversation trop volumineuse. Ouvrez une nouvelle conversation.')
            req=urllib.request.Request(WEBHOOK,data=body,headers={'Content-Type':'application/json','X-Webhook-Token':TOKEN},method='POST')
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=240) as r:
                raw=r.read(2*1024*1024+1)
            if len(raw)>2*1024*1024: raise ValueError('Réponse n8n trop volumineuse.')
            result=json.loads(raw)
            if isinstance(result,list) and len(result)==1: result=result[0]
            if not isinstance(result,dict): raise ValueError('n8n doit renvoyer un objet JSON avec answer.')
            if result.get('error'): raise ValueError('Le workflow n8n signale une erreur. Consultez son exécution.')
            if result.get('conversationId') and result['conversationId']!=cid: raise ValueError('Identifiant de conversation incohérent.')
            answer=result.get('answer')
            if not isinstance(answer,str) or not answer.strip(): raise ValueError('Réponse n8n vide : vérifier Respond to Webhook et le champ answer.')
        now=time.time()
        with db() as c:
            c.execute('INSERT INTO messages(id,chat,role,text,created,provider) VALUES (?,?,?,?,?,?)',(secrets.token_hex(16),cid,'assistant',answer[:200000],now,provider))
            c.execute("UPDATE jobs SET status='done' WHERE id=?",(jid,))
            c.execute('UPDATE chats SET updated=? WHERE id=?',(now,cid))
    except Exception as e:
        if isinstance(e,urllib.error.HTTPError): error=f'n8n a répondu HTTP {e.code}. Vérifiez le workflow, son URL et les identifiants.'
        elif isinstance(e,(TimeoutError,urllib.error.URLError)): error='n8n inaccessible ou délai dépassé. Vérifiez son exécution avant de relancer (un appel IA peut avoir été effectué).'
        elif isinstance(e,ValueError): error=str(e)[:350]
        else: error='Traitement interrompu. Vérifiez la configuration et réessayez.'
        with db() as c: c.execute("UPDATE jobs SET status='error',error=? WHERE id=?",(error,jid))
