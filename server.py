"""Standalone HTTP API. Default mock; no hardware discovery or access on startup."""
import argparse, contextlib, fcntl, hashlib, io, json, os, re, shutil, subprocess, tempfile, threading, time, uuid, zipfile
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from PIL import Image, ImageDraw
from geometry import plan, batch_plan, DPI, MODES, KEYS, LIMIT
from mosaic import layout, compose
from alignment import diagnose

STATIC=Path(__file__).resolve().parent/"static"
ID=re.compile(r"[0-9a-f]{32}")
class Busy(Exception): pass
class Cancelled(Exception): pass

def digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

class Scanner:
    def __init__(self,root,device=None,lock_path=None):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.device=device
        self.lock_path=Path(lock_path or self.root/"scanner.lock")
        self.guard=threading.Lock()
        self.active=None
        self.stop=threading.Event()
        self.mosaic_guard=threading.Lock()
        # Never recover a live job automatically: a process restart is not permission to scan.
        # A stale running manifest may be explicitly resumed when the shared lock is free.

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
        p=batch_plan(obj['regions']) if 'regions' in obj else plan(obj)
        with self.guard:
            if self.active: raise Busy("scanner busy")
            if shutil.disk_usage(self.root).free < sum(t["pixels"] for t in p["tiles"])*4+50_000_000:
                raise ValueError("insufficient disk space")
            j={"id":uuid.uuid4().hex,"created":datetime.now(timezone.utc).isoformat(),
               "backend":"sane" if self.device else "mock","device":self.device,"state":"running","plan":p,"results":[],"attempts":1}
            (self.root/j["id"]).mkdir()
            self.save(j)
            self.stop.clear()
            self.active=j["id"]
            threading.Thread(target=self.worker,args=(j,),daemon=True).start()
        return j["id"]

    def resume(self,id,confirmed):
        if confirmed is not True: raise ValueError("confirm unchanged original position")
        with self.guard:
            if self.active: raise Busy("scanner busy")
            # Check the OS lock before inspecting or editing a stale manifest.
            # The worker also acquires this lock, so a competing scanner never overlaps.
            with self.lock_path.open("a") as lock:
                try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: raise Busy("shared scanner lock busy")
                j=self.read(id)
                if j["state"] not in ("failed","cancelled","running"):
                    raise ValueError("job cannot be resumed")
                if j["backend"]!=("sane" if self.device else "mock") or (self.device and j.get("device")!=self.device):
                    raise ValueError("scanner backend or device changed")
                folder=self.root/id
                done=set()
                for t in j["results"]:
                    path=folder/t["file"]
                    if not path.is_file() or path.stat().st_size!=t["bytes"]:
                        raise ValueError("completed original missing or changed; start a new job")
                    with Image.open(path) as im:
                        if im.size!=(t["widthPx"],t["heightPx"]): raise ValueError("original dimensions changed")
                        im.verify()
                    checksum=digest(path)
                    if t.get("sha256") and t["sha256"]!=checksum:
                        raise ValueError("completed original checksum mismatch")
                    # Older manifests did not record checksums: require new capture rather than assume integrity.
                    if not t.get("sha256"):
                        raise ValueError("legacy original has no acquisition checksum; start a new job")
                    done.add(t["index"])
                pending=[t for t in j["plan"]["tiles"] if t["index"] not in done]
                if not pending: raise ValueError("no missing tiles")
                for t in pending:
                    p=t.get("settings",j["plan"])
                    ext="jpg" if p["format"]=="jpeg" else "png"
                    if (folder/f'tile-{t["index"]:03d}.{ext}').exists():
                        raise ValueError("untracked original exists; refusing overwrite")
                if shutil.disk_usage(self.root).free < sum(t["pixels"] for t in pending)*4+50_000_000:
                    raise ValueError("insufficient disk space")
                j.setdefault("previousAttempts",[]).append({"state":j["state"],"error":j.get("error"),"failedTile":j.get("failedTile"),"ended":j.get("ended")})
                j["attempts"]=j.get("attempts",1)+1
                j["resumed"]=datetime.now(timezone.utc).isoformat()
                j["state"]="running"
                for k in ("error","failedTile","ended"): j.pop(k,None)
                self.save(j)
                self.stop.clear()
                self.active=id
            threading.Thread(target=self.worker,args=(j,),daemon=True).start()
        return id

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
                done={r["index"] for r in j["results"]}
                for t in j["plan"]["tiles"]:
                    if t["index"] in done: continue
                    if self.stop.is_set(): raise Cancelled()
                    j["failedTile"]=t["index"]
                    p=t.get("settings",j["plan"])
                    ext="jpg" if p["format"]=="jpeg" else "png"
                    name=f'tile-{t["index"]:03d}.{ext}'
                    temp=folder/(name+".part")
                    try:
                        if (folder/name).exists(): raise ValueError("refusing to overwrite an original")
                        self.capture(p,t,temp)
                        if self.stop.is_set(): raise Cancelled()
                        with Image.open(temp) as im:
                            w,h=im.size
                            if w*h>LIMIT: raise ValueError("actual image exceeds 35 MP")
                            im.verify()
                        checksum=digest(temp)
                        # Atomic publication without overwrite, even if an unexpected file appears.
                        os.link(temp,folder/name)
                        j["results"].append({"index":t["index"],"file":name,"widthPx":w,"heightPx":h,
                                             "bytes":(folder/name).stat().st_size,"sha256":checksum,"region":{k:t[k] for k in KEYS},
                                             "dpi":p["dpi"],"mode":p["mode"],"roiIndex":t.get("roiIndex",1)})
                        j.pop("failedTile",None)
                        self.save(j)
                    finally: temp.unlink(missing_ok=True)
                j["state"]="complete"
        except Cancelled: j["state"]="cancelled"
        except Exception as e:
            j["state"]="failed";j["error"]=str(e)[-600:]
        finally:
            j["ended"]=datetime.now(timezone.utc).isoformat()
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
            d=t.get("dpi",j["plan"].get("dpi"))
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
            if p=="/api/alignment":
                j=self.scanner.read(q["id"][0]);roi=int(q.get("roi",[1])[0])
                if not self.scanner.mosaic_guard.acquire(blocking=False): raise Busy("derived image processing busy")
                try: return self.send(diagnose(j,self.scanner.root/j["id"],roi))
                finally: self.scanner.mosaic_guard.release()
            if p in ("/api/mosaic-plan","/api/mosaic"):
                j=self.scanner.read(q["id"][0]);roi=int(q.get("roi",[1])[0])
                scale=q.get("scale",["preview"])[0]
                if scale not in ("preview","full"): raise ValueError("invalid mosaic scale")
                if p=="/api/mosaic-plan": return self.send(layout(j,roi,scale=="preview"))
                if not self.scanner.mosaic_guard.acquire(blocking=False): raise Busy("mosaic export busy")
                try:
                    with tempfile.TemporaryDirectory(dir=self.scanner.root) as tmp:
                        fmt=q.get("format",["png"])[0]
                        quality=int(q.get("quality",[95])[0]);sampling=int(q.get("sampling",[0])[0])
                        path,info=compose(j,self.scanner.root/j["id"],tmp,roi,scale=="preview",fmt,quality,sampling)
                        ext="jpg" if fmt=="jpeg" else "png"
                        name=f'roi-{roi:02d}-nominal-{scale}.{ext}' if q.get("download")==["1"] else None
                        return self.stream(path,"image/jpeg" if fmt=="jpeg" else "image/png",name)
                finally: self.scanner.mosaic_guard.release()
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
            if self.path=="/api/batch-plan": return self.send(batch_plan(obj.get("regions")))
            if self.path=="/api/scan": return self.send({"id":self.scanner.start(obj)},status=202)
            if self.path=="/api/resume": return self.send({"id":self.scanner.resume(obj["id"],obj.get("confirmedUnmoved"))},status=202)
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
