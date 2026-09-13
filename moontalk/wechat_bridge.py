from __future__ import annotations
import ctypes, json, os, random, tempfile, threading, time
from collections import defaultdict, deque
import requests

_UIA_SENDER_LOCK = threading.Lock()


class _UiaSender:
    WECHAT_TITLES = ('微信', 'WeChat')

    def __init__(self):
        self.auto = self.window = None
        self.last_contact = ''
        self.last_error = ''
        try:
            import uiautomation as auto
            self.auto = auto
            self._find_window()
        except Exception as exc:
            self.last_error = f'UIA 初始化失败: {exc}'

    def _is_wechat(self, control):
        try:
            if control.ClassName in ('Chrome_WidgetWin_1', 'CabinetWClass'):
                return False
            name = str(control.Name or '')
            return any(title in name for title in self.WECHAT_TITLES)
        except Exception:
            return False

    def _find_window(self):
        if self.auto is None:
            return
        self.window = None
        root = self.auto.GetRootControl()

        def scan(control):
            if self._is_wechat(control):
                self.window = control
                return True
            try:
                children = control.GetChildren()
            except Exception:
                return False
            for child in children:
                if scan(child):
                    return True
            return False

        for top_window in root.GetChildren():
            if scan(top_window):
                return
        for title in self.WECHAT_TITLES:
            window = self.auto.WindowControl(Name=title, searchDepth=3)
            if window.Exists(0, 0):
                self.window = window
                return

    def _ensure_window(self):
        if self.window and self.window.Exists(.2):
            return True
        for _ in range(3):
            self._find_window()
            if self.window and self.window.Exists(.2):
                return True
            time.sleep(.3)
        self.last_error = '未找到微信窗口，请确认微信桌面版已登录并运行'
        return False

    def _get_wechat_hwnd(self):
        for class_name in ('Qt51514QWindowIcon', 'WeChatMainWndForPC'):
            hwnd = ctypes.windll.user32.FindWindowW(class_name, None)
            if hwnd:
                return hwnd
        return 0

    def _activate(self):
        try:
            self.window.SetActive()
        except Exception:
            try:
                self.window.SwitchToThisWindow()
            except Exception:
                pass
        hwnd = self._get_wechat_hwnd()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            ctypes.windll.user32.BringWindowToTop(hwnd)
        time.sleep(.25)

    def _switch_contact(self, contact):
        self._activate()
        hwnd = self._get_wechat_hwnd()
        if not hwnd:
            self.last_error = '未取得微信主窗口句柄'
            return False
        wechat_thread = ctypes.windll.user32.GetWindowThreadProcessId(hwnd, None)
        current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = bool(ctypes.windll.user32.AttachThreadInput(current_thread, wechat_thread, True))
        try:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            ctypes.windll.user32.BringWindowToTop(hwnd)
            self.auto.SendKeys('{Ctrl}f')
            time.sleep(.4)
            self.auto.SendKeys('{Ctrl}a')
            import pyperclip
            pyperclip.copy(contact)
            time.sleep(.1)
            self.auto.SendKeys('{Ctrl}v')
            time.sleep(.4)
            self.auto.SendKeys('{Enter}')
            time.sleep(.7)
            return True
        finally:
            if attached:
                ctypes.windll.user32.AttachThreadInput(current_thread, wechat_thread, False)

    def send_text(self, contact, text):
        with _UIA_SENDER_LOCK:
            self.last_error = ''
            com_initialized = False
            try:
                ctypes.windll.ole32.CoInitialize(None)
                com_initialized = True
                self.window = None
                if not self._ensure_window():
                    return False
                if contact:
                    if not self._switch_contact(contact):
                        return False
                    self.last_contact = contact
                else:
                    self._activate()
                import pyperclip
                time.sleep(random.uniform(.3, .8))
                pyperclip.copy(text)
                time.sleep(random.uniform(.1, .3))
                self.auto.SendKeys('{Ctrl}v')
                time.sleep(min(len(text) * .02, 2) + random.uniform(.3, .6))
                self.auto.SendKeys('{Enter}')
                return True
            except Exception as exc:
                self.last_error = str(exc)
                self.window = None
                return False
            finally:
                self.window = None
                if com_initialized:
                    ctypes.windll.ole32.CoUninitialize()

class WeChatBridge:
    MEDIA_CONTENTS = ('[图片]', '[动画表情]', '[表情]')

    def __init__(self, on_message=None, on_vision=None):
        self.on_message=on_message; self.on_vision=on_vision; self._config={}; self._stop=threading.Event(); self._thread=None; self._sender=None
        self._logs=deque(maxlen=200); self._status={'running':False,'connected':False,'messages':0,'last_error':''}; self._buffers=defaultdict(list); self._timers={}
    def log(self, message): self._logs.append(f'[{time.strftime("%H:%M:%S")}] {message}')
    def logs(self): return list(self._logs)
    def status(self): return {**self._status,'logs':self.logs()}
    def start(self, config):
        if self._thread and self._thread.is_alive(): return {'ok':True,'message':'微信桥接已在运行'}
        self._config=dict(config or {})
        if not self._config.get('access_token'): return {'ok':False,'error':'未配置 WeFlow Access Token'}
        self._stop.clear(); self._status={'running':True,'connected':False,'messages':0,'last_error':''}
        self._thread=threading.Thread(target=self._run,daemon=True); self._thread.start(); return {'ok':True,'message':'微信桥接已启动'}
    def stop(self):
        self._stop.set()
        for t in self._timers.values(): t.cancel()
        self._timers.clear(); self._buffers.clear()
        if self._thread and self._thread.is_alive(): self._thread.join(3)
        self._status['running']=False; self._status['connected']=False; return {'ok':True,'message':'微信桥接已停止'}
    def _run(self):
        base=(self._config.get('weflow_base_url') or 'http://127.0.0.1:5031').rstrip('/'); endpoint=base+'/api/v1/push/messages'
        while not self._stop.is_set():
            try:
                with requests.get(endpoint,params={'access_token':self._config['access_token']},headers={'Accept':'text/event-stream'},stream=True,timeout=None) as r:
                    r.raise_for_status(); self._status['connected']=True; self.log('已连接 WeFlow 推送')
                    for raw in r.iter_lines(decode_unicode=True):
                        if self._stop.is_set(): break
                        if not raw or not raw.startswith('data:'): continue
                        try: e=json.loads(raw[5:].strip())
                        except json.JSONDecodeError: continue
                        if self._accept(e): self._buffer(e)
            except Exception as exc:
                self._status['connected']=False; self._status['last_error']=str(exc); self.log(f'SSE 异常: {exc}'); self._stop.wait(5)
    def _is_group(self,e): return e.get('sessionType')=='group' or bool(e.get('groupName')) or '@chatroom' in str(e.get('sessionId') or '')
    def _accept(self,e):
        text=str(e.get('content') or e.get('text') or '').strip(); names=self._config.get('bot_nicknames') or []
        if not text or e.get('type') in (34,) or '[语音]' in text or e.get('sourceName') in names: return False
        if self._config.get('bot_wxid') and e.get('talkerId')==self._config['bot_wxid']: return False
        return not self._is_group(e) or self._config.get('group_reply_mode','mention')=='all' or any(f'@{n}' in text or f'＠{n}' in text for n in names if n)
    def _buffer(self,e):
        content=str(e.get('content') or e.get('text') or '').strip()
        if content in self.MEDIA_CONTENTS:
            threading.Thread(target=self._buffer_media,args=(e,),daemon=True).start()
            return
        self._append_buffer(e,content)
    def _append_buffer(self,e,content):
        group=self._is_group(e); contact=str(e.get('groupName') or e.get('sessionId') or e.get('sourceName') or '') if group else str(e.get('sourceName') or e.get('sessionId') or '')
        key=str(e.get('sessionId') or contact); self._buffers[key].append((contact,content,e))
        if key in self._timers: self._timers[key].cancel()
        self._timers[key]=threading.Timer(float(self._config.get('buffer_seconds',5)),self._flush,args=(key,)); self._timers[key].start()
    def _fetch_media(self,e):
        base=(self._config.get('weflow_base_url') or 'http://127.0.0.1:5031').rstrip('/')
        session=str(e.get('sessionId') or e.get('talkerId') or e.get('sourceName') or '')
        talker=session if self._is_group(e) else str(e.get('talkerId') or session)
        response=requests.get(base+'/api/v1/messages',params={'access_token':self._config['access_token'],'talker':talker,'media':'true','limit':5},timeout=15)
        response.raise_for_status()
        payload=response.json(); messages=payload if isinstance(payload,list) else payload.get('messages',payload.get('data',[]))
        if not isinstance(messages,list): messages=[]
        media=next((item for item in messages if item.get('mediaType') in ('image','sticker','emoji') and item.get('mediaUrl')),None)
        if not media: raise RuntimeError('WeFlow 未返回可下载的图片或表情')
        media_url=str(media['mediaUrl']); separator='&' if '?' in media_url else '?'
        image=requests.get(f"{media_url}{separator}access_token={self._config['access_token']}",timeout=30)
        image.raise_for_status()
        content_type=(image.headers.get('Content-Type') or '').split(';',1)[0].lower()
        suffix={'image/png':'.png','image/gif':'.gif','image/webp':'.webp'}.get(content_type,'.jpg')
        descriptor,path=tempfile.mkstemp(suffix=suffix)
        with os.fdopen(descriptor,'wb') as file: file.write(image.content)
        return path,content_type or 'image/jpeg'
    def _buffer_media(self,e):
        label='图片' if str(e.get('content') or e.get('text'))=='[图片]' else '表情'
        path=''
        try:
            if not self.on_vision: raise RuntimeError('未配置视觉模型处理器')
            path,content_type=self._fetch_media(e)
            self.log(f'已下载微信{label}，正在调用视觉模型')
            description=str(self.on_vision(path,content_type,label) or '').strip()
            if not description: raise RuntimeError('视觉模型未返回描述')
            self.log(f'视觉模型已描述微信{label}: {description[:80]}')
            self._append_buffer(e,f'[{label}内容]\n{description}')
        except Exception as exc:
            detail=f'微信{label}识别失败: {exc}'
            self._status['last_error']=detail; self.log(detail)
            self._append_buffer(e,f'[{label}内容无法识别]')
        finally:
            if path:
                try: os.unlink(path)
                except OSError: pass
    def _flush(self,key):
        items=self._buffers.pop(key,[]); self._timers.pop(key,None)
        if not items: return
        contact=items[-1][0]; text='\n'.join(i[1] for i in items); self._status['messages']+=1; self.log(f'收到微信消息: {text[:80]}')
        if self.on_message:
            try: reply=self.on_message(text,items[-1][2])
            except Exception as exc: self.log(f'消息处理异常: {exc}'); return
            if reply:
                if self._sender is None: self._sender=_UiaSender()
                if self._sender.send_text(contact,str(reply)):
                    self.log(f'[UIA] 已发送至 {contact}')
                    self._status['last_error']=''
                else:
                    detail=self._sender.last_error or '未知错误'
                    self._status['last_error']=detail
                    self.log(f'[UIA] 发送失败至 {contact}: {detail}')
