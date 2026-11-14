"""Live feed + zone editor over HTTP, for a camera board with no screen (Arduino UNO Q, Pi).

  python main.py --stream-port 8090 ...   then open  http://<board>:8090  on the laptop

  /            live view with the bin boxes drawn on it; click corners to draw a box, name it, Save
  /stream      MJPEG of the annotated feed (green box = idle, red = motion)
  /frame.jpg   one raw frame
  /zones       GET the zones; POST new zones (main.py swaps them in without a restart)

If the upload token is set, saving zones needs it too (?key=...), since the board sits on shared Wi-Fi.
"""
from __future__ import annotations

import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

ITEMS = ["pepperoni", "cheese", "dough", "sauce"]   # default; main.py passes the live list from the API


class StreamState:
    def __init__(self, zones: list[dict], width: int, height: int, key: str = "", items: list[str] | None = None):
        self.lock = threading.Lock()
        self.items = list(items or ITEMS)
        self.zones = zones
        self.size = (width, height)
        self.key = key
        self.jpeg: bytes | None = None
        self.raw: bytes | None = None
        self.pending_zones: list[dict] | None = None   # set by POST /zones, consumed by main loop
        self.status = ""
        self._last = 0.0

    def publish(self, frame, active: dict[str, bool], status: str, fps: float = 6.0):
        """Called from the camera loop. Encodes at most `fps` times a second (cheap on a small board)."""
        now = time.time()
        if now - self._last < 1.0 / fps:
            return
        self._last = now
        raw = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
        view = frame.copy()
        with self.lock:
            zones = list(self.zones)
        for z in zones:
            pts = np.array(z["points"], np.int32)
            color = (0, 0, 255) if active.get(z["name"]) else (0, 200, 0)
            cv2.polylines(view, [pts], True, color, 3)
            x, y = pts.min(axis=0)
            cv2.putText(view, z["name"], (int(x), max(20, int(y) - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        cv2.putText(view, status, (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
        jpeg = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
        with self.lock:
            self.jpeg, self.raw, self.status = jpeg, raw, status

    def take_pending(self) -> list[dict] | None:
        with self.lock:
            z, self.pending_zones = self.pending_zones, None
            return z

    def validate(self, zones) -> str | None:
        if not isinstance(zones, list):
            return "zones must be a list"
        w, h = self.size
        names = set()
        for z in zones:
            name, pts = z.get("name"), z.get("points")
            if name not in self.items:
                return f"zone name must be one of {self.items} (got {name!r})"
            if name in names:
                return f"two boxes named {name}"
            names.add(name)
            if not isinstance(pts, list) or len(pts) < 3:
                return f"{name}: needs at least 3 corners"
            for p in pts:
                if not (isinstance(p, list) and len(p) == 2 and 0 <= p[0] < w and 0 <= p[1] < h):
                    return f"{name}: corner {p} is outside the {w}x{h} frame"
        return None


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Camera</title><style>
:root{color-scheme:light dark;--bg:#111;--fg:#eee;--mut:#999;--acc:#e5484d;--ok:#30a46c}
body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
main{max-width:1100px;margin:auto;padding:16px}
.wrap{position:relative;width:100%}
img,canvas{width:100%;display:block;border-radius:8px}
canvas{position:absolute;inset:0;cursor:crosshair}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}
button,select{font:inherit;padding:7px 12px;border-radius:8px;border:1px solid #444;background:#222;color:var(--fg);cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
#msg{color:var(--mut)} .err{color:var(--acc)!important} .good{color:var(--ok)!important}
ol{color:var(--mut);margin:4px 0 0 18px;padding:0}
</style></head><body><main>
<h2 style="margin:4px 0 2px">Bin camera</h2>
<div id="msg">Green box = idle, red = motion. To draw a box: click its corners on the picture, pick the ingredient, then "Finish box".</div>
<div class="bar">
 <select id="name">ITEM_OPTIONS</select>
 <button id="finish">Finish box</button><button id="undo">Undo corner</button><button id="clear">Clear all boxes</button>
 <button id="save" class="primary">Save boxes</button>
</div>
<div class="wrap"><img id="feed" src="/stream"><canvas id="cv"></canvas></div>
<ol id="list"></ol>
</main><script>
const W=FRAME_W,H=FRAME_H;
let zones=[],cur=[],dirty=false;const cv=document.getElementById('cv'),ctx=cv.getContext('2d'),msg=document.getElementById('msg');
function say(t,c){msg.textContent=t;msg.className=c||''}
function fit(){const r=document.getElementById('feed').getBoundingClientRect();cv.width=r.width;cv.height=r.height;draw()}
function draw(){const s=cv.width/W;ctx.clearRect(0,0,cv.width,cv.height);
 const poly=(p,c,close)=>{if(!p.length)return;ctx.strokeStyle=c;ctx.lineWidth=2;ctx.setLineDash(close?[6,4]:[]);ctx.beginPath();
  p.forEach(([x,y],i)=>i?ctx.lineTo(x*s,y*s):ctx.moveTo(x*s,y*s));if(close)ctx.closePath();ctx.stroke();
  p.forEach(([x,y])=>{ctx.fillStyle=c;ctx.fillRect(x*s-3,y*s-3,6,6)})};
 zones.forEach(z=>{poly(z.points,'#4cc9f0',true);ctx.fillStyle='#4cc9f0';ctx.font='14px system-ui';ctx.fillText(z.name+(dirty?' (not saved yet)':''),z.points[0][0]*s+4,z.points[0][1]*s-6)});
 poly(cur,'#ffd60a',false);
 document.getElementById('list').innerHTML=zones.map(z=>'<li>'+z.name+': '+z.points.length+' corners</li>').join('')}
cv.addEventListener('click',e=>{const r=cv.getBoundingClientRect(),s=W/r.width;cur.push([Math.round((e.clientX-r.left)*s),Math.round((e.clientY-r.top)*s)]);draw()});
document.getElementById('undo').onclick=()=>{cur.pop();draw()};
document.getElementById('clear').onclick=()=>{zones=[];cur=[];dirty=true;draw();say('Cleared. Draw new boxes, then Save.')};
document.getElementById('finish').onclick=()=>{const n=document.getElementById('name').value;
 if(cur.length<3)return say('Click at least 3 corners first.','err');
 zones=zones.filter(z=>z.name!==n);zones.push({name:n,points:cur});cur=[];dirty=true;draw();say(n+' box ready. Add more, or Save.')};
document.getElementById('save').onclick=async()=>{const r=await fetch('/zones',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(zones)});
 const j=await r.json();if(r.ok){dirty=false;draw()}if(r.ok)say('Saved '+zones.length+' box(es). The camera is using them now.','good');else say(j.error||'Save failed','err')};
fetch('/zones').then(r=>r.json()).then(z=>{zones=z;draw()});
window.addEventListener('resize',fit);document.getElementById('feed').addEventListener('load',fit,{once:true});setTimeout(fit,800);
</script></body></html>"""


def serve(state: StreamState, port: int) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            """With a key set (public link), every page needs it: ?key=... once, then a cookie."""
            if not state.key:
                return True
            q = parse_qs(urlparse(self.path).query).get("key", [""])[0]
            cookie = next((c.split("=", 1)[1] for c in (self.headers.get("cookie") or "").split("; ")
                           if c.startswith("camkey=")), "")
            return hmac.compare_digest(q, state.key) or hmac.compare_digest(cookie, state.key)

        def _login(self):
            body = (b"<!doctype html><meta name=viewport content='width=device-width'><title>Camera</title>"
                    b"<body style='font:16px system-ui;background:#111;color:#eee;display:grid;place-items:center;height:90vh'>"
                    b"<form><p>Camera key</p><input name=key type=password autofocus style='font:inherit;padding:8px'>"
                    b" <button style='font:inherit;padding:8px 14px'>Open</button></form>")
            self._send(401, body, "text/html; charset=utf-8")

        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if not self._authed():
                return self._login() if path == "/" else self._send(401, b"key required", "text/plain")
            if state.key and "key=" in (urlparse(self.path).query or ""):   # remember it, drop it from the URL
                self.send_response(303)
                self.send_header("set-cookie", f"camkey={state.key}; HttpOnly; SameSite=Lax; Path=/; Max-Age=43200")
                self.send_header("location", path)
                self.end_headers()
                return
            if path == "/":
                opts = "".join(f"<option>{i}</option>" for i in state.items)
                page = PAGE.replace("ITEM_OPTIONS", opts).replace("FRAME_W", str(state.size[0])).replace("FRAME_H", str(state.size[1]))
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if path == "/zones":
                with state.lock:
                    return self._send(200, json.dumps(state.zones).encode(), "application/json")
            if path == "/frame.jpg":
                with state.lock:
                    raw = state.raw
                return self._send(200, raw, "image/jpeg") if raw else self._send(503, b"no frame yet", "text/plain")
            if path == "/stream":
                self.send_response(200)
                self.send_header("content-type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("cache-control", "no-store")
                self.end_headers()
                last = None
                try:
                    while True:
                        with state.lock:
                            jpeg = state.jpeg
                        if jpeg is not None and jpeg is not last:
                            self.wfile.write(b"--frame\r\ncontent-type: image/jpeg\r\ncontent-length: "
                                             + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                            last = jpeg
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    return
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path != "/zones":
                return self._send(404, b"not found", "text/plain")
            if not self._authed():
                return self._send(403, json.dumps({"error": "camera key required"}).encode(), "application/json")
            try:
                zones = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
            except Exception:
                return self._send(400, json.dumps({"error": "bad JSON"}).encode(), "application/json")
            err = state.validate(zones)
            if err:
                return self._send(400, json.dumps({"error": err}).encode(), "application/json")
            with state.lock:
                state.pending_zones = zones
            return self._send(200, json.dumps({"ok": True, "zones": len(zones)}).encode(), "application/json")

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="stream").start()
    return srv
