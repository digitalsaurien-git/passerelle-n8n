import io,json,os,tempfile,threading,time
from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
received=[]
class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        raw=self.rfile.read(int(self.headers['Content-Length'])); payload=json.loads(raw); received.append(payload)
        out=json.dumps({'answer':'Réponse '+payload['provider'],'conversationId':payload['conversationId']}).encode()
        self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self,*args): pass
server=HTTPServer(('127.0.0.1',0),Stub); threading.Thread(target=server.serve_forever,daemon=True).start()
tmp=tempfile.TemporaryDirectory()
os.environ.update(APP_PASSWORD='un-mot-de-passe-de-test',ALLOWED_ORIGINS='http://test.local',COOKIE_SECURE='false',APP_MODE='n8n',N8N_WEBHOOK_URL=f'http://127.0.0.1:{server.server_port}/hook',N8N_WEBHOOK_TOKEN='test-token',DATA_DIR=tmp.name)
import app as module
client=module.app.test_client(); H={'Origin':'http://test.local'}
r=client.post('/api/login',json={'password':'un-mot-de-passe-de-test'},headers=H); assert r.status_code==200,r.text
cid=client.post('/api/chats',headers=H).get_json()['id']
r=client.post(f'/api/chats/{cid}/messages',headers=H,data={'text':'Analyse ces notes','provider':'claude','files':(io.BytesIO('bonjour à tous'.encode()),'notes.txt')},content_type='multipart/form-data'); assert r.status_code==202,r.text
for _ in range(100):
    data=client.get(f'/api/chats/{cid}').get_json()
    if data['jobs'][0]['status']!='pending': break
    time.sleep(.03)
assert data['jobs'][0]['status']=='done',data
assert data['messages'][-1]['text']=='Réponse claude'
assert received[0]['provider']=='claude'
assert received[0]['messages'][0]['attachments'][0]['extractedText']=='bonjour à tous'
r=client.post(f'/api/chats/{cid}/messages',headers=H,data={'text':'Et maintenant avec OpenAI','provider':'openai'},content_type='multipart/form-data'); assert r.status_code==202,r.text
for _ in range(100):
    data=client.get(f'/api/chats/{cid}').get_json()
    if data['jobs'][0]['status']!='pending': break
    time.sleep(.03)
assert data['jobs'][0]['status']=='done',data
assert received[1]['provider']=='openai'
assert len(received[1]['messages'])==3,received[1]['messages']
assert data['messages'][-1]['provider']=='openai'
assert client.post('/api/chats',headers={'Origin':'http://evil.test'}).status_code==403
server.shutdown();tmp.cleanup();print('SMOKE OK: auth, historique, fichier, Claude, OpenAI, CORS')
