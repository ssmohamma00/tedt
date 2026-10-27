#!/usr/bin/env python3
"""Game For Sale - secure LAN game dashboard. Python standard library only."""
import gzip, base64, http.server, json, sqlite3, os, secrets, hashlib, hmac, time, urllib.parse, uuid, datetime, sys, socket, subprocess, ipaddress, threading, re
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor'))
import qrcode
from qrcode.image.svg import SvgPathImage
from http.cookies import SimpleCookie
LOGIN_ATTEMPTS={}; LOGIN_WINDOW=300; LOGIN_MAX=10; SESSION_IDLE=20*60
BASE=os.path.dirname(os.path.abspath(__file__)); DB=os.path.join(BASE,'dashboard.sqlite3'); HOST=os.environ.get('DASHBOARD_HOST','0.0.0.0'); PORT=int(os.environ.get('DASHBOARD_PORT','8080'))
def db():
 c=sqlite3.connect(DB,timeout=15);c.row_factory=sqlite3.Row;return c
def pw_hash(p,salt=None):
 salt=salt or secrets.token_bytes(16);return salt.hex()+':'+hashlib.pbkdf2_hmac('sha256',p.encode(),salt,310000).hex()
def pw_ok(p,stored):
 try:s,h=stored.split(':');return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(s),310000).hex(),h)
 except:return False
def init():
 c=db();c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=NORMAL');c.execute('PRAGMA foreign_keys=ON');c.executescript("""CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created TEXT NOT NULL);CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires INTEGER NOT NULL);CREATE TABLE IF NOT EXISTS categories(id TEXT PRIMARY KEY,name TEXT NOT NULL,icon TEXT NOT NULL,price INTEGER NOT NULL DEFAULT 0);CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY,category_id TEXT NOT NULL,name TEXT NOT NULL,version TEXT,image TEXT);CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,number INTEGER UNIQUE NOT NULL,user_id TEXT NOT NULL,name TEXT,phone TEXT,date TEXT NOT NULL,total INTEGER NOT NULL);CREATE TABLE IF NOT EXISTS order_games(order_id TEXT NOT NULL,name TEXT NOT NULL,category TEXT NOT NULL,price INTEGER NOT NULL);""")
 c.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT "")')
 catalog=json.load(open(os.path.join(BASE,'catalog.json'),encoding='utf-8'))
 # Add catalog entries that were never seeded before; never overwrite or resurrect what the admin edited/deleted.
 row=c.execute("SELECT value FROM settings WHERE key='seeded'").fetchone()
 try:seeded=set(json.loads(row[0])) if row else set()
 except Exception:seeded=set()
 for cat in catalog.get('categories',[]):
  if 'c:'+cat['id'] not in seeded:
   c.execute('INSERT OR IGNORE INTO categories VALUES(?,?,?,?)',(cat['id'],cat['name'],cat.get('icon','🎮'),int(cat.get('price',0))));seeded.add('c:'+cat['id'])
  for g in cat.get('games',[]):
   if 'g:'+str(g['id']) not in seeded:
    c.execute('INSERT OR IGNORE INTO games VALUES(?,?,?,?,?)',(str(g['id']),cat['id'],g['name'],g.get('version',''),g.get('image','')));seeded.add('g:'+str(g['id']))
 c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('seeded',json.dumps(sorted(seeded))))
 try:c.execute('ALTER TABLE order_games ADD COLUMN game_id TEXT')
 except sqlite3.OperationalError:pass
 try:c.execute('ALTER TABLE order_games ADD COLUMN installed INTEGER NOT NULL DEFAULT 0')
 except sqlite3.OperationalError:pass
 c.execute('CREATE TABLE IF NOT EXISTS order_extras(order_id TEXT NOT NULL,kind TEXT NOT NULL,name TEXT NOT NULL,price INTEGER NOT NULL DEFAULT 0)')
 c.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT "")')
 c.commit();c.close()
def user_dict(r):return {'id':r['id'],'username':r['username'],'role':r['role']}
STORE_KEYS=('phone','card')
def store_info(c):
 rows={r['key']:r['value'] for r in c.execute('SELECT key,value FROM settings WHERE key IN (?,?)',STORE_KEYS).fetchall()}
 return {k:rows.get(k,'') for k in STORE_KEYS}

# ---------------- Network settings (DHCP by default, optional manual static IP) ----------------
NET_FILE=os.path.join(BASE,'network.json'); NET_LOCK=threading.Lock(); NET_LAST={'state':'idle','message':'','time':0}
IS_WIN=os.name=='nt'; IS_MAC=sys.platform=='darwin'
DEFAULT_NET={'mode':'dhcp','iface':'','ip':'','prefix':24,'gateway':'','dns':[]}
def net_load():
 try:
  d=json.load(open(NET_FILE,encoding='utf-8'));out=dict(DEFAULT_NET);out.update({k:d[k] for k in DEFAULT_NET if k in d});return out
 except Exception:return dict(DEFAULT_NET)
def net_save(cfg):
 tmp=NET_FILE+'.tmp';json.dump(cfg,open(tmp,'w',encoding='utf-8'),ensure_ascii=False,indent=1);os.replace(tmp,NET_FILE)
def is_admin():
 try:
  if IS_WIN:
   import ctypes;return bool(ctypes.windll.shell32.IsUserAnAdmin())
  return os.geteuid()==0
 except Exception:return False
def run_cmd(args,timeout=20):
 try:
  r=subprocess.run(args,capture_output=True,text=True,timeout=timeout,encoding='utf-8',errors='replace');return r.returncode,(r.stdout or '')+(r.stderr or '')
 except Exception as e:return 1,str(e)
def primary_ip():
 """IPv4 that this machine currently uses on the LAN (what DHCP or the manual setting gave it)."""
 for target in ('10.255.255.255','192.168.255.255','8.8.8.8'):
  try:
   s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(0.5);s.connect((target,9));ip=s.getsockname()[0];s.close()
   if ip and not ip.startswith('127.') and not ip.startswith('0.'):return ip
  except Exception:pass
 try:
  for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
   if not ip.startswith('127.') and not ip.startswith('169.254.'):return ip
 except Exception:pass
 return ''
def list_ifaces():
 out=[]
 try:
  if IS_WIN:
   rc,txt=run_cmd(['powershell','-NoProfile','-NonInteractive','-Command','Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress,PrefixLength,PrefixOrigin | ConvertTo-Json -Compress'],15)
   if rc==0 and txt.strip():
    data=json.loads(txt);data=data if isinstance(data,list) else [data]
    for a in data:
     ip=a.get('IPAddress','')
     if ip.startswith('127.') or ip.startswith('169.254.'):continue
     out.append({'name':a.get('InterfaceAlias',''),'ip':ip,'prefix':a.get('PrefixLength'),'origin':'dhcp' if str(a.get('PrefixOrigin','')).lower()=='dhcp' else 'manual'})
  elif IS_MAC:
   rc,txt=run_cmd(['networksetup','-listallnetworkservices'])
   for line in txt.splitlines()[1:]:
    n=line.strip().lstrip('*')
    if n:out.append({'name':n,'ip':'','prefix':None,'origin':''})
  else:
   rc,txt=run_cmd(['ip','-j','-4','addr'])
   if rc==0:
    for itf in json.loads(txt):
     if itf.get('ifname')=='lo':continue
     for a in itf.get('addr_info',[]):
      if a.get('family')=='inet':out.append({'name':itf['ifname'],'ip':a['local'],'prefix':a.get('prefixlen'),'origin':'dhcp' if a.get('dynamic') else 'manual'})
 except Exception:pass
 return out
def net_status():
 cfg=net_load();ip=primary_ip();ifs=list_ifaces()
 cur=next((i for i in ifs if i['ip']==ip),None)
 return {'config':cfg,'ip':ip,'port':PORT,'url':panel_url(),'ifaces':ifs,'actual_mode':(cur or {}).get('origin',''),'actual_iface':(cur or {}).get('name',''),
  'is_admin':is_admin(),'platform':'windows' if IS_WIN else ('mac' if IS_MAC else 'linux'),'last':NET_LAST}
def panel_url(host_header=''):
 ip=primary_ip() if HOST in ('0.0.0.0','::','') else HOST
 if not ip and host_header:
  return 'http://'+host_header+'/'
 if not ip:ip='localhost'
 return 'http://%s%s/'%(ip,'' if PORT==80 else ':%d'%PORT)
def prefix_from(v):
 v=str(v).strip()
 if re.fullmatch(r'\d{1,2}',v):
  p=int(v)
  if 1<=p<=30:return p
  raise ValueError('طول پیشوند ساب‌نت باید بین ۱ تا ۳۰ باشد.')
 m=ipaddress.IPv4Address(v);b=bin(int(m))[2:].zfill(32)
 if '01' in b or b.count('1')<1 or b.count('1')>30:raise ValueError('ساب‌نت‌ماسک نامعتبر است.')
 return b.count('1')
def validate_net(d,ifaces):
 mode=str(d.get('mode','dhcp')).lower()
 if mode not in ('dhcp','manual'):raise ValueError('حالت نامعتبر است.')
 iface=str(d.get('iface','')).strip()
 names=[i['name'] for i in ifaces if i['name']]
 if iface:
  if names and iface not in names:raise ValueError('کارت شبکه نامعتبر است.')
  if not re.fullmatch(r'[\w .\-()#:]{1,64}',iface):raise ValueError('نام کارت شبکه نامعتبر است.')
 elif not names:raise ValueError('کارت شبکه‌ای پیدا نشد.')
 else:iface=(next((i['name'] for i in ifaces if i['ip']==primary_ip()),None) or names[0])
 cfg=dict(DEFAULT_NET);cfg['mode']=mode;cfg['iface']=iface
 if mode=='manual':
  try:ip=ipaddress.IPv4Address(str(d.get('ip','')).strip())
  except Exception:raise ValueError('آدرس IP نامعتبر است.')
  if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local or ip==ipaddress.IPv4Address('255.255.255.255'):raise ValueError('این آدرس IP قابل استفاده نیست.')
  try:prefix=prefix_from(d.get('prefix','24') or '24')
  except ipaddress.AddressValueError:raise ValueError('ساب‌نت‌ماسک نامعتبر است.')
  net=ipaddress.IPv4Network('%s/%d'%(ip,prefix),strict=False)
  if ip in (net.network_address,net.broadcast_address):raise ValueError('IP با این ساب‌نت آدرس شبکه/پخش است و قابل استفاده نیست.')
  gw=str(d.get('gateway','')).strip()
  if gw:
   try:g=ipaddress.IPv4Address(gw)
   except Exception:raise ValueError('آدرس گیت‌وی نامعتبر است.')
   if g not in net or g==ip:raise ValueError('گیت‌وی باید داخل همان شبکه و غیر از IP دستگاه باشد.')
  dns=d.get('dns',[]);dns=[x.strip() for x in (dns.replace('،',',').split(',') if isinstance(dns,str) else dns) if str(x).strip()][:2]
  for x in dns:
   try:ipaddress.IPv4Address(x)
   except Exception:raise ValueError('آدرس DNS نامعتبر است: '+x)
  cfg.update({'ip':str(ip),'prefix':prefix,'gateway':gw,'dns':dns})
 return cfg
def apply_net(cfg):
 """Apply to the OS. Returns (ok,message)."""
 i=cfg['iface'];manual=cfg['mode']=='manual'
 if manual:
  mask=str(ipaddress.IPv4Network('0.0.0.0/%d'%cfg['prefix']).netmask)
 if IS_WIN:
  if manual:
   a=['netsh','interface','ipv4','set','address','name='+i,'source=static','address='+cfg['ip'],'mask='+mask]
   if cfg['gateway']:a+=['gateway='+cfg['gateway']]
   rc,o=run_cmd(a)
   if rc:return False,o.strip()
   if cfg['dns']:
    rc,o=run_cmd(['netsh','interface','ipv4','set','dnsservers','name='+i,'source=static','address='+cfg['dns'][0],'validate=no'])
    if rc:return False,o.strip()
    for n,d in enumerate(cfg['dns'][1:],2):run_cmd(['netsh','interface','ipv4','add','dnsservers','name='+i,'address='+d,'index=%d'%n,'validate=no'])
   else:run_cmd(['netsh','interface','ipv4','set','dnsservers','name='+i,'source=dhcp'])
   return True,'IP دستی روی «%s» اعمال شد.'%i
  rc,o=run_cmd(['netsh','interface','ipv4','set','address','name='+i,'source=dhcp'])
  if rc:return False,o.strip()
  run_cmd(['netsh','interface','ipv4','set','dnsservers','name='+i,'source=dhcp']);run_cmd(['ipconfig','/renew'],40)
  return True,'«%s» روی DHCP قرار گرفت.'%i
 if IS_MAC:
  if manual:
   rc,o=run_cmd(['networksetup','-setmanual',i,cfg['ip'],mask,cfg['gateway'] or cfg['ip']])
   if rc:return False,o.strip()
   run_cmd(['networksetup','-setdnsservers',i]+(cfg['dns'] or ['Empty']));return True,'IP دستی اعمال شد.'
  rc,o=run_cmd(['networksetup','-setdhcp',i]);run_cmd(['networksetup','-setdnsservers',i,'Empty']);return (rc==0),(o.strip() if rc else 'روی DHCP قرار گرفت.')
 rc,conn=run_cmd(['nmcli','-g','GENERAL.CONNECTION','dev','show',i]);conn=conn.strip()
 if rc or not conn or conn=='--':return False,'پروفایل NetworkManager برای این کارت پیدا نشد (nmcli لازم است).'
 if manual:
  a=['nmcli','con','mod',conn,'ipv4.method','manual','ipv4.addresses','%s/%d'%(cfg['ip'],cfg['prefix']),'ipv4.gateway',cfg['gateway'],'ipv4.dns',' '.join(cfg['dns'])]
 else:a=['nmcli','con','mod',conn,'ipv4.method','auto','ipv4.addresses','','ipv4.gateway','','ipv4.dns','']
 rc,o=run_cmd(a)
 if rc:return False,o.strip()
 rc,o=run_cmd(['nmcli','con','up',conn],40)
 return (rc==0),('تنظیمات اعمال شد.' if rc==0 else o.strip())
def apply_net_bg(cfg):
 def job():
  time.sleep(1.5)
  with NET_LOCK:
   NET_LAST.update({'state':'applying','message':'','time':time.time()})
   try:ok,msg=apply_net(cfg)
   except Exception as e:ok,msg=False,str(e)
   time.sleep(3)
   NET_LAST.update({'state':'ok' if ok else 'error','message':msg,'time':time.time()})
 threading.Thread(target=job,daemon=True).start()
class H(http.server.BaseHTTPRequestHandler):
 server_version='GameForSale/1.0'
 def log_message(self,*a): pass
 def send(self,status,obj=None,ctype='application/json; charset=utf-8',headers=None):
  payload=json.dumps(obj,ensure_ascii=False).encode() if obj is not None else None
  gz=False
  if payload is not None and len(payload)>1024 and 'gzip' in self.headers.get('Accept-Encoding',''):payload=gzip.compress(payload,3);gz=True
  self.send_response(status);self.send_header('Content-Type',ctype)
  if gz:self.send_header('Content-Encoding','gzip')
  if payload is not None:self.send_header('Content-Length',str(len(payload)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','same-origin');self.send_header('Content-Security-Policy',"default-src 'self' data:; img-src 'self' data: blob: https: http:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'");self.send_header('Cache-Control','no-store')
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers()
  if payload is not None:self.wfile.write(payload)
 def body(self):
  raw=self.headers.get('Content-Length','0')
  try:n=int(raw)
  except ValueError: raise ValueError('invalid content length')
  if n<0 or n>2_000_000: raise ValueError('request body too large')
  d=json.loads(self.rfile.read(n) or b'{}')
  if not isinstance(d,dict): raise ValueError('body must be an object')
  return d
 def origin_ok(self):
  origin=self.headers.get('Origin')
  if origin:
   parsed=urllib.parse.urlparse(origin)
   host=self.headers.get('Host','')
   return parsed.netloc.lower()==host.lower() and parsed.scheme in ('http','https')
  return self.headers.get('Sec-Fetch-Site','same-origin') not in ('cross-site',)
 def reject_bad_origin(self):
  if not self.origin_ok():
   self.send(403,{'error':'درخواست بین‌سایتی رد شد.'});return True
  return False
 def route_path(self):return urllib.parse.urlparse(self.path).path
 def query(self):return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
 def auth(self):
  ck=SimpleCookie(self.headers.get('Cookie',''));m=ck.get('sid')
  if not m:return None
  token=m.value;th=hashlib.sha256(token.encode()).hexdigest();now=int(time.time());c=db();r=c.execute('SELECT u.*,s.expires AS sess_exp FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1',(th,now)).fetchone()
  if r and r['sess_exp']-now<SESSION_IDLE-30:c.execute('UPDATE sessions SET expires=? WHERE token_hash=?',(now+SESSION_IDLE,th));c.commit()
  c.close();return r
 def require(self,admin=False):
  u=self.auth()
  if not u:self.send(401,{'error':'ابتدا وارد شوید.'});return None
  if admin and u['role']!='admin':self.send(403,{'error':'دسترسی مدیر لازم است.'});return None
  return u
 def do_GET(self):
  p=self.route_path()
  if p=='/' or p=='/index.html':
   idx=os.path.join(BASE,'static','index.html')
   try:b=open(idx,'rb').read()
   except Exception as e:
    import traceback;traceback.print_exc()
    b=('not found: '+repr(idx)+' -- '+repr(e)).encode()
   self.send(200,None,'text/html; charset=utf-8');self.wfile.write(b);return
  if p.startswith('/static/assets/') or p.startswith('/static/fonts/'):
   sub='fonts' if p.startswith('/static/fonts/') else 'assets';name=os.path.basename(p);ext=os.path.splitext(name)[1].lower();types={'.ttf':'font/ttf','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.svg':'image/svg+xml','.webp':'image/webp'}
   fp=os.path.join(BASE,'static',sub,name)
   if ext not in types or not os.path.isfile(fp):self.send(404,{'error':'یافت نشد'});return
   b=open(fp,'rb').read();self.send(200,None,types[ext],headers={'Cache-Control':'public, max-age=3600'});self.wfile.write(b);return
  if p=='/api/panel-qr.svg':
   host=self.headers.get('Host','') or ('localhost:%d'%PORT)
   if not all(ch.isalnum() or ch in '.-:[]' for ch in host):self.send(400,{'error':'آدرس نامعتبر'});return
   url=panel_url(host)
   qr=qrcode.QRCode(version=None,error_correction=qrcode.constants.ERROR_CORRECT_Q,box_size=10,border=2);qr.add_data(url);qr.make(fit=True)
   data=qr.make_image(image_factory=SvgPathImage).to_string()
   self.send(200,None,'image/svg+xml; charset=utf-8',headers={'X-Panel-Url':url});self.wfile.write(data);return
  if p=='/api/store-info':
   u=self.require()
   if not u:return
   c=db();out=store_info(c);c.close();self.send(200,out);return
  if p=='/api/network':
   u=self.require(True)
   if not u:return
   self.send(200,net_status());return
  if p=='/api/status':
   c=db();n=c.execute('SELECT COUNT(*) FROM users WHERE role="admin" AND active=1').fetchone()[0];c.close();self.send(200,{'initialized':n>0});return
  if not p.startswith('/api/'):self.send(404,{'error':'یافت نشد'});return
  if p=='/api/me':
   u=self.auth()
   if not u:self.send(401,{'error':'unauthorized'});return
   self.send(200,{'user':user_dict(u)});return
  if p=='/api/categories':
   u=self.require()
   if not u:return
   c=db();rows=c.execute('SELECT c.*,COUNT(g.id) count FROM categories c LEFT JOIN games g ON g.category_id=c.id GROUP BY c.id ORDER BY c.rowid').fetchall();c.close();self.send(200,{'categories':[dict(r) for r in rows]});return
  if p=='/api/games':
   u=self.require()
   if not u:return
   cat=self.query().get('category',[''])[0];c=db();rows=c.execute('SELECT g.id,g.category_id,g.name,g.version,g.image,c.name category,c.price FROM games g JOIN categories c ON c.id=g.category_id WHERE g.category_id=? ORDER BY g.rowid',(cat,)).fetchall();c.close()
   out=[]
   for r in rows:
    d=dict(r);img=d.get('image') or ''
    if img.startswith('data:'):d['image']='/api/game-image/%s?v=%s'%(d['id'],hashlib.md5(img.encode()).hexdigest()[:10])
    out.append(d)
   self.send(200,{'games':out});return
  if p.startswith('/api/game-image/'):
   u=self.require()
   if not u:return
   gid=p.rsplit('/',1)[-1];c=db();r=c.execute('SELECT image FROM games WHERE id=?',(gid,)).fetchone();c.close()
   img=(r['image'] if r else '') or ''
   m=re.match(r'^data:(image/[\w.+-]+);base64,(.*)$',img,re.S)
   if not m:self.send(404,{'error':'یافت نشد'});return
   try:raw=base64.b64decode(m.group(2))
   except Exception:self.send(404,{'error':'یافت نشد'});return
   etag='"%s"'%hashlib.md5(raw).hexdigest()
   if self.headers.get('If-None-Match')==etag:
    self.send_response(304);self.send_header('ETag',etag);self.send_header('Cache-Control','private, max-age=31536000, immutable');self.end_headers();return
   self.send(200,None,m.group(1),headers={'Cache-Control':'private, max-age=31536000, immutable','ETag':etag,'Content-Length':str(len(raw))});self.wfile.write(raw);return
  if p=='/api/users':
   u=self.require(True)
   if not u:return
   c=db();rows=c.execute('SELECT id,username,role,active FROM users ORDER BY created').fetchall();c.close();self.send(200,{'users':[dict(r) for r in rows]});return
  if p=='/api/orders':
   u=self.require()
   if not u:return
   c=db()
   rows=c.execute('SELECT * FROM orders '+('ORDER BY number DESC' if u['role']=='admin' else 'WHERE user_id=? ORDER BY number DESC'),() if u['role']=='admin' else (u['id'],)).fetchall()
   out=[self.order_obj(c,r) for r in rows];c.close();self.send(200,{'orders':out});return
  if p.startswith('/api/orders/') and p.endswith('/qr.svg'):
    u=self.require()
    if not u:return
    oid=p[len('/api/orders/'): -len('/qr.svg')].strip('/')
    c=db();r=c.execute('SELECT * FROM orders WHERE id=?'+('' if u['role']=='admin' else ' AND user_id=?'),(oid,) if u['role']=='admin' else (oid,u['id'])).fetchone()
    if not r:c.close();self.send(404,{'error':'سفارش پیدا نشد'});return
    order=self.order_obj(c,r);c.close()
    payload={'invoice_number':order['number'],'date':order['date'],'customer':order.get('name') or '', 'phone':order.get('phone') or '', 'games':order['games'],'items':[{'name':e['name'],'price':e['price']} for e in order['extras'] if e['kind']=='item'],'with_device':[e['name'] for e in order['extras'] if e['kind']=='acc'],'total':order['total'],'currency':'تومان'}
    qr=qrcode.QRCode(version=None,error_correction=qrcode.constants.ERROR_CORRECT_M,box_size=5,border=3);qr.add_data(json.dumps(payload,ensure_ascii=False,separators=(',',':')));qr.make(fit=True)
    image=qr.make_image(image_factory=SvgPathImage);data=image.to_string()
    self.send(200,None,'image/svg+xml; charset=utf-8',headers={'Content-Disposition':'inline; filename="invoice-qr.svg"','Cache-Control':'no-store'});self.wfile.write(data);return
  if p.startswith('/api/orders/'):
   u=self.require()
   if not u:return
   oid=p.rsplit('/',1)[-1];c=db();r=c.execute('SELECT * FROM orders WHERE id=?'+('' if u['role']=='admin' else ' AND user_id=?'),(oid,) if u['role']=='admin' else (oid,u['id'])).fetchone()
   if not r:c.close();self.send(404,{'error':'سفارش پیدا نشد'});return
   out=self.order_obj(c,r);c.close();self.send(200,out);return
  self.send(404,{'error':'یافت نشد'})
 def recalc_total(self,c,oid):
  c.execute("UPDATE orders SET total=(SELECT COALESCE(SUM(price),0) FROM order_games WHERE order_id=?)+(SELECT COALESCE(SUM(price),0) FROM order_extras WHERE order_id=? AND kind='item') WHERE id=?",(oid,oid,oid))
 def clean_extras(self,d,u):
  if u['role']!='admin':return []
  raw=d.get('extras',[]);out=[]
  if not isinstance(raw,list) or len(raw)>100:return None
  for x in raw:
   if not isinstance(x,dict):return None
   kind=x.get('kind');name=str(x.get('name','')).strip()[:100]
   if kind not in ('item','acc') or not name:return None
   if kind=='item':
    try:price=max(0,min(int(x.get('price',0)),10**12))
    except (TypeError,ValueError):return None
   else:price=0
   out.append((kind,name,price))
  return out
 def order_obj(self,c,r):
  gs=c.execute('SELECT rowid AS rid,name,category,price,game_id,installed FROM order_games WHERE order_id=? ORDER BY rowid',(r['id'],)).fetchall();out=[]
  for g in gs:
   d=dict(g);d['installed']=bool(d.get('installed'))
   if not d.get('game_id'):
    m=c.execute("SELECT id FROM games WHERE name||CASE WHEN COALESCE(version,'')<>'' THEN ' ('||version||')' ELSE '' END=?",(d['name'],)).fetchone();d['game_id']=m['id'] if m else None
   out.append(d)
  ex=[{'rid':e['rid'],'kind':e['kind'],'name':e['name'],'price':e['price']} for e in c.execute('SELECT rowid AS rid,kind,name,price FROM order_extras WHERE order_id=? ORDER BY rowid',(r['id'],)).fetchall()]
  return {'id':r['id'],'number':str(r['number']),'name':r['name'],'phone':r['phone'],'date':r['date'],'total':r['total'],'games':out,'extras':ex}
 def do_POST(self):
  p=self.route_path()
  if self.reject_bad_origin(): return
  try:d=self.body()
  except Exception:self.send(400,{'error':'درخواست نامعتبر یا بیش از حد بزرگ'});return
  if p=='/api/network':
   u=self.require(True)
   if not u:return
   try:cfg=validate_net(d,list_ifaces())
   except ValueError as e:self.send(400,{'error':str(e)});return
   if not is_admin():self.send(403,{'error':'برای تغییر IP سیستم‌عامل، برنامه را با دسترسی Administrator (ویندوز) یا root/sudo (لینوکس/مک) اجرا کنید.'});return
   if NET_LAST.get('state')=='applying':self.send(409,{'error':'یک تغییر شبکه در حال انجام است.'});return
   net_save(cfg);apply_net_bg(cfg);self.send(200,{'ok':True,'config':cfg,'note':'تنظیمات ذخیره شد و تا چند ثانیه دیگر اعمال می‌شود؛ اگر IP تغییر کند اتصال این صفحه قطع می‌شود و باید با آدرس جدید باز شود.'});return
  if p=='/api/setup':
   un=str(d.get('username','')).strip();pw=str(d.get('password',''))
   if len(un)<1 or len(pw)<4:self.send(400,{'error':'نام کاربری حداقل ۱ کاراکتر و رمز عبور حداقل ۴ کاراکتر باشد.'});return
   c=db()
   try:
    c.execute('BEGIN IMMEDIATE')
    if c.execute('SELECT 1 FROM users WHERE role="admin"').fetchone():
     c.rollback();c.close();self.send(409,{'error':'مدیر قبلاً ساخته شده است.'});return
    uid=uuid.uuid4().hex;c.execute('INSERT INTO users VALUES(?,?,?,?,1,?)',(uid,un,pw_hash(pw),'admin',datetime.date.today().isoformat()));c.commit();c.close();self.login_session(uid);return
   except sqlite3.IntegrityError:
    c.rollback();c.close();self.send(409,{'error':'ساخت مدیر هم‌زمان انجام شده یا نام کاربری تکراری است.'});return
  if p=='/api/login':
   ip=self.client_address[0]; now=time.time(); attempts=[t for t in LOGIN_ATTEMPTS.get(ip,[]) if now-t<LOGIN_WINDOW]; LOGIN_ATTEMPTS[ip]=attempts
   if len(attempts)>=LOGIN_MAX:self.send(429,{'error':'تلاش‌های ورود زیاد است؛ چند دقیقه دیگر دوباره امتحان کنید.'},headers={'Retry-After':str(max(1,int(LOGIN_WINDOW-(now-attempts[0]))))});return
   un=str(d.get('username','')).strip();pw=str(d.get('password',''));c=db();u=c.execute('SELECT * FROM users WHERE username=? AND active=1',(un,)).fetchone();c.close()
   if not u or not pw_ok(pw,u['password_hash']):
    attempts.append(now);LOGIN_ATTEMPTS[ip]=attempts;self.send(401,{'error':'نام کاربری یا رمز عبور اشتباه است.'});return
   LOGIN_ATTEMPTS.pop(ip,None);self.login_session(u['id']);return
  if p=='/api/logout':
   ck=SimpleCookie(self.headers.get('Cookie',''));m=ck.get('sid')
   if m:
    c=db();c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(m.value.encode()).hexdigest(),));c.commit();c.close()
   self.send(200,{'ok':True},headers={'Set-Cookie':'sid=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'});return
  if p=='/api/users':
   u=self.require(True)
   if not u:return
   un=str(d.get('username','')).strip();pw=str(d.get('password',''));role=d.get('role')
   if len(un)<1 or len(pw)<4 or role not in ('admin','customer'):self.send(400,{'error':'نام کاربری حداقل ۱ کاراکتر، رمز حداقل ۴ کاراکتر و نقش معتبر لازم است.'});return
   c=db()
   try:c.execute('INSERT INTO users VALUES(?,?,?,?,1,?)',(uuid.uuid4().hex,un,pw_hash(pw),role,datetime.date.today().isoformat()));c.commit()
   except sqlite3.IntegrityError:c.close();self.send(409,{'error':'این نام کاربری قبلاً وجود دارد.'});return
   c.close();self.send(201,{'ok':True});return
  m=re.match(r'^/api/orders/([^/]+)/extras$',p)
  if m:
   u=self.require(True)
   if not u:return
   oid=m.group(1);ex=self.clean_extras({'extras':[d]},u)
   if not ex:self.send(400,{'error':'مورد اضافه نامعتبر است.'});return
   c=db()
   if not c.execute('SELECT 1 FROM orders WHERE id=?',(oid,)).fetchone():c.close();self.send(404,{'error':'سفارش پیدا نشد'});return
   if c.execute('SELECT COUNT(*) FROM order_extras WHERE order_id=?',(oid,)).fetchone()[0]>=100:c.close();self.send(400,{'error':'تعداد موارد اضافه بیش از حد است.'});return
   try:
    c.execute('BEGIN IMMEDIATE');c.execute('INSERT INTO order_extras(order_id,kind,name,price) VALUES(?,?,?,?)',(oid,ex[0][0],ex[0][1],ex[0][2]));self.recalc_total(c,oid);c.commit()
   except sqlite3.Error:
    c.rollback();c.close();self.send(503,{'error':'ثبت انجام نشد؛ دوباره تلاش کنید.'});return
   r=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone();out=self.order_obj(c,r);c.close();self.send(201,out);return
  if p=='/api/orders':
   u=self.require()
   if not u:return
   ids=d.get('games',[]);extras=self.clean_extras(d,u)
   if extras is None:self.send(400,{'error':'موارد اضافه نامعتبر است.'});return
   if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids) or (not ids and not extras) or len(ids)>500:self.send(400,{'error':'بازی یا مورد اضافه انتخاب کنید.'});return
   c=db();marks=','.join('?' for _ in ids);rows=c.execute(f'SELECT g.id,g.name,g.version,c.name category,c.price FROM games g JOIN categories c ON c.id=g.category_id WHERE g.id IN ({marks})',ids).fetchall() if ids else []
   if len(rows)!=len(set(ids)):c.close();self.send(400,{'error':'بعضی بازی‌ها معتبر نیستند.'});return
   total=sum(int(r['price']) for r in rows)+sum(e[2] for e in extras);oid=uuid.uuid4().hex;today=datetime.date.today().isoformat();name=str(d.get('name','')).strip()[:100];phone=str(d.get('phone','')).strip()[:40]
   if not name or not phone:c.close();self.send(400,{'error':'نام مشتری و شماره تلفن اجباری است.'});return
   try:
    # Serialize invoice-number allocation and order creation across concurrent clients.
    c.execute('BEGIN IMMEDIATE')
    number=c.execute('SELECT COALESCE(MAX(number),0)+1 FROM orders').fetchone()[0]
    c.execute('INSERT INTO orders VALUES(?,?,?,?,?,?,?)',(oid,number,u['id'],name,phone,today,total))
    for r in rows:c.execute('INSERT INTO order_games(order_id,name,category,price,game_id) VALUES(?,?,?,?,?)',(oid,r['name']+(' ('+r['version']+')' if r['version'] else ''),r['category'],int(r['price']),r['id']))
    for e in extras:c.execute('INSERT INTO order_extras(order_id,kind,name,price) VALUES(?,?,?,?)',(oid,e[0],e[1],e[2]))
    c.commit();r=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone();out=self.order_obj(c,r);c.close();self.send(201,out);return
   except sqlite3.Error:
    c.rollback();c.close();self.send(503,{'error':'ثبت سفارش موقتاً با مشکل مواجه شد؛ دوباره تلاش کنید.'});return
  if p=='/api/categories':
   u=self.require(True)
   if not u:return
   name=str(d.get('name','')).strip()[:60];icon=(str(d.get('icon','')).strip()[:8] or '🎮')
   try:price=max(0,min(int(d.get('price',0) or 0),10**12))
   except (TypeError,ValueError):self.send(400,{'error':'قیمت نامعتبر'});return
   if not name:self.send(400,{'error':'نام دسته‌بندی لازم است.'});return
   c=db()
   if c.execute('SELECT 1 FROM categories WHERE name=?',(name,)).fetchone():c.close();self.send(409,{'error':'دسته‌بندی با این نام وجود دارد.'});return
   cid='cat'+uuid.uuid4().hex[:10];c.execute('INSERT INTO categories(id,name,icon,price) VALUES(?,?,?,?)',(cid,name,icon,price));c.commit();c.close();self.send(201,{'ok':True,'id':cid});return
  if p=='/api/games':
   u=self.require(True)
   if not u:return
   name=str(d.get('name','')).strip()[:160];cat=str(d.get('category_id',''));version=str(d.get('version',''))[:50];image=str(d.get('image',''))
   if not name or len(image)>1500000:self.send(400,{'error':'نام بازی لازم است و تصویر بیش از حد بزرگ است.'});return
   if image and not (image.startswith('data:image/') or image.startswith('http://') or image.startswith('https://') or image.startswith('/static/')):self.send(400,{'error':'آدرس یا فایل تصویر نامعتبر است.'});return
   c=db();ok=c.execute('SELECT 1 FROM categories WHERE id=?',(cat,)).fetchone()
   if not ok:c.close();self.send(400,{'error':'دسته‌بندی نامعتبر'});return
   gid=uuid.uuid4().hex;c.execute('INSERT INTO games VALUES(?,?,?,?,?)',(gid,cat,name,version,image));c.commit();c.close();self.send(201,{'ok':True});return
  self.send(404,{'error':'یافت نشد'})
 def login_session(self,uid):
  token=secrets.token_urlsafe(32);c=db();c.execute('DELETE FROM sessions WHERE expires<?',(int(time.time()),));c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),uid,int(time.time())+SESSION_IDLE));c.commit();c.close();self.send(200,{'ok':True},headers={'Set-Cookie':f'sid={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'})
 def do_PATCH(self):
  p=self.route_path()
  if self.reject_bad_origin(): return
  u=self.require(True)
  if not u:return
  try:d=self.body()
  except Exception:self.send(400,{'error':'درخواست نامعتبر یا بیش از حد بزرگ'});return
  if p.startswith('/api/categories/'):
   cid=p.rsplit('/',1)[-1];c=db();row=c.execute('SELECT * FROM categories WHERE id=?',(cid,)).fetchone()
   if not row:c.close();self.send(404,{'error':'دسته پیدا نشد'});return
   name=row['name'];icon=row['icon'];price=row['price']
   if 'name' in d:
    name=str(d.get('name','')).strip()[:60]
    if not name:c.close();self.send(400,{'error':'نام دسته‌بندی لازم است.'});return
    if c.execute('SELECT 1 FROM categories WHERE name=? AND id<>?',(name,cid)).fetchone():c.close();self.send(409,{'error':'دسته‌بندی با این نام وجود دارد.'});return
   if 'icon' in d:icon=str(d.get('icon','')).strip()[:8] or '🎮'
   if 'price' in d:
    try:price=max(0,min(int(d.get('price') or 0),10**12))
    except (TypeError,ValueError):c.close();self.send(400,{'error':'قیمت نامعتبر'});return
   c.execute('UPDATE categories SET name=?,icon=?,price=? WHERE id=?',(name,icon,price,cid));c.commit();c.close();self.send(200,{'ok':True});return
  m=re.match(r'^/api/orders/([^/]+)/games/(\d+)$',p)
  if m:
   oid,rid=m.group(1),int(m.group(2));inst=1 if d.get('installed') else 0
   c=db();cur=c.execute('UPDATE order_games SET installed=? WHERE order_id=? AND rowid=?',(inst,oid,rid));c.commit();c.close();self.send(200,{'ok':True,'installed':bool(inst)} if cur.rowcount else {'error':'ردیف پیدا نشد'});return
  if p.startswith('/api/games/'):
   gid=p.rsplit('/',1)[-1]
   if 'name' in d:
    name=str(d.get('name','')).strip()[:120]
    if not name:self.send(400,{'error':'نام بازی را بنویسید.'});return
    c=db();cur=c.execute('UPDATE games SET name=? WHERE id=?',(name,gid));c.commit();c.close();self.send(200,{'ok':True} if cur.rowcount else {'error':'بازی پیدا نشد'});return
   image=str(d.get('image',''))
   if len(image)>1500000:self.send(400,{'error':'تصویر بیش از حد بزرگ است.'});return
   if image and not (image.startswith('data:image/') or image.startswith('http://') or image.startswith('https://') or image.startswith('/static/')):self.send(400,{'error':'آدرس یا فایل تصویر نامعتبر است.'});return
   c=db();cur=c.execute('UPDATE games SET image=? WHERE id=?',(image,gid));c.commit();c.close();self.send(200,{'ok':True} if cur.rowcount else {'error':'بازی پیدا نشد'});return
  self.send(404,{'error':'یافت نشد'})
 def do_PUT(self):
  p=self.route_path()
  if self.reject_bad_origin(): return
  u=self.require(True)
  if not u:return
  try:d=self.body()
  except Exception:self.send(400,{'error':'درخواست نامعتبر یا بیش از حد بزرگ'});return
  if p=='/api/store-info':
   if not isinstance(d,dict):self.send(400,{'error':'درخواست نامعتبر'});return
   phone=str(d.get('phone','')).strip();card=str(d.get('card','')).strip()
   if not re.fullmatch(r'[0-9۰-۹٠-٩+\-\s()]{0,40}',phone):self.send(400,{'error':'شماره تماس فروشگاه نامعتبر است.'});return
   if not re.fullmatch(r'[0-9۰-۹٠-٩\s-]{0,40}',card):self.send(400,{'error':'شماره کارت فقط می‌تواند عدد باشد.'});return
   c=db()
   try:
    c.execute('BEGIN IMMEDIATE')
    for k,v in (('phone',phone),('card',card)):c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,v))
    c.commit()
   except sqlite3.Error:
    c.rollback();c.close();self.send(503,{'error':'ذخیره انجام نشد؛ دوباره تلاش کنید.'});return
   out=store_info(c);c.close();self.send(200,out);return
  if p.startswith('/api/orders/'):
   oid=p.rsplit('/',1)[-1];ids=d.get('games',[]);extras=self.clean_extras(d,u)
   if extras is None:self.send(400,{'error':'موارد اضافه نامعتبر است.'});return
   if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids) or (not ids and not extras) or len(ids)>500:self.send(400,{'error':'بازی یا مورد اضافه انتخاب کنید.'});return
   c=db();old=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
   if not old:c.close();self.send(404,{'error':'سفارش پیدا نشد'});return
   marks=','.join('?' for _ in ids);rows=c.execute(f'SELECT g.id,g.name,g.version,c.name category,c.price FROM games g JOIN categories c ON c.id=g.category_id WHERE g.id IN ({marks})',ids).fetchall() if ids else []
   if len(rows)!=len(set(ids)):c.close();self.send(400,{'error':'بعضی بازی‌ها معتبر نیستند.'});return
   total=sum(int(r['price']) for r in rows)+sum(e[2] for e in extras);name=str(d.get('name','')).strip()[:100];phone=str(d.get('phone','')).strip()[:40]
   if not name or not phone:c.close();self.send(400,{'error':'نام مشتری و شماره تلفن اجباری است.'});return
   try:
    inst={x['game_id'] for x in c.execute('SELECT game_id FROM order_games WHERE order_id=? AND installed=1',(oid,)).fetchall()}
    c.execute('BEGIN IMMEDIATE');c.execute('UPDATE orders SET name=?,phone=?,total=? WHERE id=?',(name,phone,total,oid));c.execute('DELETE FROM order_games WHERE order_id=?',(oid,));c.execute('DELETE FROM order_extras WHERE order_id=?',(oid,))
    for e in extras:c.execute('INSERT INTO order_extras(order_id,kind,name,price) VALUES(?,?,?,?)',(oid,e[0],e[1],e[2]))
    for r in rows:c.execute('INSERT INTO order_games(order_id,name,category,price,game_id,installed) VALUES(?,?,?,?,?,?)',(oid,r['name']+(' ('+r['version']+')' if r['version'] else ''),r['category'],int(r['price']),r['id'],1 if r['id'] in inst else 0))
    c.commit();r=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone();out=self.order_obj(c,r);c.close();self.send(200,out);return
   except sqlite3.Error:
    c.rollback();c.close();self.send(503,{'error':'ویرایش سفارش موقتاً با مشکل مواجه شد؛ دوباره تلاش کنید.'});return
  self.send(404,{'error':'یافت نشد'})
 def do_DELETE(self):
  p=self.route_path()
  if self.reject_bad_origin(): return
  u=self.require(True)
  if not u:return
  c=db()
  if p.startswith('/api/users/'):
   uid=p.rsplit('/',1)[-1]
   target=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
   if not target:c.close();self.send(404,{'error':'کاربر پیدا نشد'});return
   if target['id']==u['id']:c.close();self.send(400,{'error':'نمی‌توانید حساب خودتان را غیرفعال کنید.'});return
   if target['role']=='admin' and c.execute('SELECT COUNT(*) FROM users WHERE role="admin" AND active=1').fetchone()[0]<=1:c.close();self.send(400,{'error':'حداقل یک مدیر فعال باید باقی بماند.'});return
   c.execute('UPDATE users SET active=0 WHERE id=?',(uid,));c.execute('DELETE FROM sessions WHERE user_id=?',(uid,));c.commit();c.close();self.send(200,{'ok':True});return
  m=re.match(r'^/api/orders/([^/]+)/extras/(\d+)$',p)
  if m:
   oid,rid=m.group(1),int(m.group(2))
   try:
    c.execute('BEGIN IMMEDIATE');cur=c.execute('DELETE FROM order_extras WHERE order_id=? AND rowid=?',(oid,rid));self.recalc_total(c,oid);c.commit()
   except sqlite3.Error:
    c.rollback();c.close();self.send(503,{'error':'حذف انجام نشد'});return
   if not cur.rowcount:c.close();self.send(404,{'error':'مورد پیدا نشد'});return
   r=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone();out=self.order_obj(c,r) if r else {'ok':True};c.close();self.send(200,out);return
  if p.startswith('/api/orders/'):
   oid=p.rsplit('/',1)[-1]
   try:
    c.execute('BEGIN IMMEDIATE');c.execute('DELETE FROM order_games WHERE order_id=?',(oid,));c.execute('DELETE FROM order_extras WHERE order_id=?',(oid,));cur=c.execute('DELETE FROM orders WHERE id=?',(oid,));c.commit()
   except sqlite3.Error:c.rollback();c.close();self.send(503,{'error':'حذف انجام نشد'});return
   c.close();self.send(200,{'ok':True}) if cur.rowcount else self.send(404,{'error':'سفارش پیدا نشد'});return
  if p.startswith('/api/games/'):
   gid=p.rsplit('/',1)[-1];c.execute('DELETE FROM games WHERE id=?',(gid,));c.commit();c.close();self.send(200,{'ok':True});return
  c.close();self.send(404,{'error':'یافت نشد'})
def _guard(fn):
 def w(self,*a,**k):
  try:return fn(self,*a,**k)
  except (BrokenPipeError,ConnectionResetError):pass
  except Exception:
   import traceback;traceback.print_exc()
   try:self.send(500,{'error':'خطای داخلی سرور'})
   except Exception:pass
 return w
for _m in ('do_GET','do_POST','do_PATCH','do_PUT','do_DELETE'):setattr(H,_m,_guard(getattr(H,_m)))
if __name__=='__main__':
 init();print(f'Game For Sale listening on http://{HOST}:{PORT}', flush=True);print('Panel address (QR):',panel_url(), flush=True);print('First run: open the URL and create the first admin account.', flush=True);http.server.ThreadingHTTPServer((HOST,PORT),H).serve_forever()
