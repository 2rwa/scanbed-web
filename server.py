"""Standalone HTTP API. Default mock; no hardware discovery or access on startup."""
import argparse, contextlib, fcntl, io, json, os, re, shutil, subprocess, tempfile, threading, time, uuid, zipfile
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from PIL import Image, ImageDraw
from geometry import plan, DPI, MODES, KEYS, LIMIT

STATIC=Path(__file__).resolve().parent/"static"
ID=re.compile(r"[0-9a-f]{32}")
class Busy(Exception): pass
class Cancelled(Exception): pass

class Scanner:
    def __init__(self,root,device=None,lock_path=None):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.device=device
        self.lock_path=Path(lock_path or self.root/"scanner.lock")
        self.guard=threading.Lock()
        self.active=None
        self.stop=threading.Event()

    def read(self,id):
        if not ID.fullmatch(id): raise ValueError("invalid job ID")
        return json.loads((self.root/id/"manifest.json").read_text())

    def save(self,j):
        folder=self.root/j["id"]
        tmp=folder/"manifest.tmp"
        tmp.write_text(json.dumps(j,ensure_ascii=False,indent=2))
        tmp.replace(folder/"manifest.json")

    def history(self):
        out=[]
        for f in self.root.glob("*/manifest.json"):
            try: out.append(self.read(f.parent.name))
            except (ValueError,OSError): pass
        return sorted(out,key=lambda j:j["created"],reverse=True)

    def start(self,obj):
        p=plan(obj)
        with self.guard:
            if self.active: raise Busy("scanner busy")
            if shutil.disk_usage(self.root).free < sum(t["pixels"] for t in p["tiles"])*4+50_000_000:
                raise ValueError("insufficient disk space")
            j={"id":uuid.uuid4().hex,"created":datetime.now(timezone.utc).isoformat(),
               "backend":"sane" if self.device else "mock","state":"running","plan":p,"results":[]}
            (self.root/j["id"]).mkdir()
            self.save(j)
            self.stop.clear()
            self.active=j["id"]
            threading.Thread(target=self.worker,args=(j,),daemon=True).start()
        return j["id"]

    def cancel(self,id):
        with self.guard:
            if self.active!=id: raise ValueError("job not active")
            self.stop.set()

    def worker(self,j):
        folder=self.root/j["id"]
        try:
            with self.lock_path.open("a") as lock:
                try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: raise Busy("shared scanner lock busy")
                for t in j["plan"]["tiles"]:
                    if self.stop.is_set(): raise Cancelled()
                    p=j["plan"]
                    ext="jpg" if p["format"]=="jpeg" else "png"
                    name=f'tile-{t["index"]:03d}.{ext}'
                    temp=folder/(name+".part")
                    try:
                        self.capture(p,t,temp)
                        if self.stop.is_set(): raise Cancelled()
                        with Image.open(temp) as im:
                            w,h=im.size
                            if w*h>LIMIT: raise ValueError("actual image exceeds 35 MP")
                            im.verify()
                        temp.replace(folder/name)
                        j["results"].append({"index":t["index"],"file":name,"widthPx":w,"heightPx":h,
                                             "bytes":(folder/name).stat().st_size,"region":{k:t[k] for k in KEYS}})
                        self.save(j)
                    finally: temp.unlink(missing_ok=True)
                j["state"]="complete"
        except Cancelled: j["state"]="cancelled"
        except Exception as e:
            j["state"]="failed";j["error"]=str(e)[-600:]
        finally:
            with self.guard:
                try: self.save(j)
                finally: self.active=None

    def capture(self,p,t,path):
        d=p["dpi"]
        if not self.device:
            w=max(1,round(t["width"]*d/25.4));h=max(1,round(t["height"]*d/25.4))
            im=Image.new("RGB",(w,h),"#f3f0e8")
            draw=ImageDraw.Draw(im)
            for axis,start,length in (("x",t["left"],t["width"]),("y",t["top"],t["height"])):
                for mm in range(0,300,10):
                    if start<=mm<=start+length:
                        v=round((mm-start)*d/25.4)
                        draw.line((v,0,v,h) if axis=="x" else (0,v,w,v),fill="#a1a8ad")
            draw.text((8,8),"MOCK / no hardware",fill="black")
            if p["mode"] in ("True Gray","Gray[Error Diffusion]","Black & White"):
                im=im.convert("L" if p["mode"]=="True Gray" else "1")
            if p["format"]=="jpeg": im.convert("RGB").save(path,format="JPEG",quality=95,subsampling=0,dpi=(d,d))
            else: im.save(path,format="PNG",dpi=(d,d))
            return
        cmd=["scanimage","-d",self.device,"--source","FlatBed","--mode",p["mode"],"--resolution",str(d),
             "-l",str(t["left"]),"-t",str(t["top"]),"-x",str(t["width"]),"-y",str(t["height"]),
             "--format="+p["format"]]
        with path.open("wb") as out, open(os.devnull,"wb") as err:
            proc=subprocess.Popen(cmd,stdout=out,stderr=err)
            deadline=time.monotonic()+600
            try:
                while proc.poll() is None:
                    if self.stop.wait(.1): raise Cancelled()
                    if time.monotonic()>deadline: raise TimeoutError("scan timeout")
                if proc.returncode: raise RuntimeError("scanimage exit "+str(proc.returncode))
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try: proc.wait(5)
                    except subprocess.TimeoutExpired: proc.kill();proc.wait()

    def export(self,id,index,fmt,quality=95,sampling=0):
        j=self.read(id)
        t=next((x for x in j["results"] if x["index"]==index),None)
        if t is None: raise FileNotFoundError("tile not found")
        path=self.root/id/t["file"]
        if fmt=="original": return path,None,path.name
        if fmt not in ("jpeg","png","pdf") or not 1<=quality<=100 or sampling not in (0,1,2):
            raise ValueError("invalid export settings")
        ext="jpg" if fmt=="jpeg" else fmt
        buf=io.BytesIO()
        with Image.open(path) as im:
            d=j["plan"]["dpi"]
            if fmt=="jpeg": im.convert("RGB").save(buf,format="JPEG",quality=quality,subsampling=sampling,dpi=(d,d))
            elif fmt=="pdf": im.convert("RGB").save(buf,format="PDF",resolution=d)
            else: im.save(buf,format="PNG",dpi=(d,d))
        return None,buf.getvalue(),f"tile-{index:03d}.{ext}"

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    @property
    def scanner(self): return self.server.scanner
    def send(self,data,mime="application/json",status=200,name=None):
        if not isinstance(data,bytes): data=json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type",mime)
        self.send_header("Content-Length",str(len(data)))
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Cache-Control","no-store")
        if name: self.send_header("Content-Disposition",'attachment; filename="'+name+'"')
        self.end_headers();self.wfile.write(data)
    def stream(self,path,mime,name=None):
        self.send_response(200);self.send_header("Content-Type",mime)
        self.send_header("Content-Length",str(path.stat().st_size))
        self.send_header("X-Content-Type-Options","nosniff")
        if name: self.send_header("Content-Disposition",'attachment; filename="'+name+'"')
        self.end_headers()
        with path.open("rb") as f: shutil.copyfileobj(f,self.wfile)
    def error(self,e):
        status=409 if isinstance(e,Busy) else 404 if isinstance(e,FileNotFoundError) else 400 if isinstance(e,(ValueError,TypeError,KeyError)) else 500
        self.send({"error":str(e) if status!=500 else "internal error"},status=status)
    def do_GET(self):
        try:
            u=urlsplit(self.path);q=parse_qs(u.query);p=u.path
            if p in ("/","/app.js","/style.css"):
                filename="index.html" if p=="/" else p[1:]
                mime={"index.html":"text/html; charset=utf-8","app.js":"text/javascript","style.css":"text/css"}[filename]
                return self.send((STATIC/filename).read_bytes(),mime)
            if p=="/api/config": return self.send({"backend":"sane" if self.scanner.device else "mock","dpi":DPI,"modes":MODES,"bed":[210,291],"safePixels":30_000_000})
            if p=="/api/history": return self.send(self.scanner.history())
            if p=="/api/status": return self.send({"active":self.scanner.active})
            if p=="/api/job": return self.send(self.scanner.read(q["id"][0]))
            if p=="/api/zip":
                j=self.scanner.read(q["id"][0])
                if not j["results"]: raise FileNotFoundError("no completed tiles")
                with tempfile.TemporaryDirectory(dir=self.scanner.root) as tmp:
                    path=Path(tmp)/"originals.zip"
                    with zipfile.ZipFile(path,"w",compression=zipfile.ZIP_STORED,allowZip64=True) as z:
                        for t in j["results"]: z.write(self.scanner.root/j["id"]/t["file"],t["file"])
                        z.writestr("manifest.json",json.dumps(j,ensure_ascii=False,indent=2))
                    return self.stream(path,"application/zip","scan-"+j["id"]+".zip")
            if p in ("/api/image","/api/download"):
                fmt=q.get("format",["original"])[0] if p=="/api/download" else "original"
                path,data,name=self.scanner.export(q["id"][0],int(q["tile"][0]),fmt,int(q.get("quality",[95])[0]),int(q.get("sampling",[0])[0]))
                mime={"jpg":"image/jpeg","png":"image/png","pdf":"application/pdf"}[name.split(".")[-1]]
                attachment=name if p=="/api/download" else None
                return self.stream(path,mime,attachment) if path else self.send(data,mime,name=attachment)
            raise FileNotFoundError("not found")
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception as e: self.error(e)
    def do_POST(self):
        try:
            origin=self.headers.get("Origin")
            if origin and urlsplit(origin).netloc!=self.headers.get("Host"): raise ValueError("cross-origin request denied")
            if self.headers.get_content_type()!="application/json": raise ValueError("JSON required")
            n=int(self.headers.get("Content-Length","0"))
            if not 0<n<=16384: raise ValueError("invalid body size")
            obj=json.loads(self.rfile.read(n))
            if not isinstance(obj,dict): raise ValueError("object required")
            if self.path=="/api/plan": return self.send(plan(obj))
            if self.path=="/api/scan": return self.send({"id":self.scanner.start(obj)},status=202)
            if self.path=="/api/cancel":
                self.scanner.cancel(obj["id"]);return self.send({"ok":True})
            raise FileNotFoundError("not found")
        except Exception as e: self.error(e)

def make_server(host,port,scanner):
    s=ThreadingHTTPServer((host,port),Handler);s.scanner=scanner;return s

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--host",default="127.0.0.1");p.add_argument("--port",type=int,default=8766)
    p.add_argument("--data",default="data");p.add_argument("--device");p.add_argument("--allow-hardware",action="store_true");p.add_argument("--lock")
    a=p.parse_args()
    if a.device and not (a.allow_hardware and a.lock): p.error("real scanner requires --allow-hardware and explicit shared --lock after migration")
    if a.allow_hardware and not a.device: p.error("explicit --device required")
    make_server(a.host,a.port,Scanner(a.data,a.device,a.lock)).serve_forever()
if __name__=="__main__": main()
