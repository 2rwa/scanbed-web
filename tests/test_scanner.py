import io,json,tempfile,threading,time,unittest,zipfile,hashlib
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from PIL import Image
from geometry import plan,SAFE
from server import Scanner,make_server

def region(**kw):
    return dict(left=5,top=5,width=10,height=12,dpi=300,**kw)

class GeometryTests(unittest.TestCase):
    def test_split_preserves_request_and_covers(self):
        r=dict(left=2,top=3,width=205,height=286,dpi=2400)
        p=plan(r)
        self.assertEqual(p["region"],{k:r[k] for k in ("left","top","width","height")})
        self.assertGreater(len(p["tiles"]),1)
        for t in p["tiles"]:
            self.assertLessEqual(t["pixels"],SAFE)
            self.assertGreaterEqual(t["left"],2)
            self.assertGreaterEqual(t["top"],3)
            self.assertLessEqual(t["left"]+t["width"],207.00001)
            self.assertLessEqual(t["top"]+t["height"],289.00001)
        for x in range(2,208):
            for y in range(3,290,3):
                self.assertTrue(any(t["left"]<=x<=t["left"]+t["width"] and t["top"]<=y<=t["top"]+t["height"] for t in p["tiles"]))
    def test_invalid(self):
        for r in [dict(region(),left=-1),dict(region(),width=0),dict(region(),top=290),dict(region(),dpi=9601),dict(region(),dpi=True),dict(region(),width=float("nan")),dict(region(),width=200,height=200,dpi=2400,method="single")]:
            with self.assertRaises(ValueError):plan(r)
    def test_thin_rectangle(self):
        p=plan(dict(left=0,top=0,width=180,height=10,dpi=2400))
        self.assertEqual(len(p["tiles"]),1)
        self.assertGreater(p["maxWidth"],180)

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.scanner=Scanner(self.tmp.name)
        self.server=make_server("127.0.0.1",0,self.scanner)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base="http://127.0.0.1:"+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.tmp.cleanup()
    def get(self,path,data=None):
        req=Request(self.base+path,data=None if data is None else json.dumps(data).encode(),headers={"Content-Type":"application/json"})
        with urlopen(req,timeout=5) as f:return f.read(),f.headers
    def job(self,r):
        j=json.loads(self.get("/api/scan",r)[0])
        for _ in range(100):
            j=json.loads(self.get("/api/job?id="+j["id"])[0])
            if j["state"]!="running":return j
            time.sleep(.03)
        self.fail("timeout")
    def test_png_jpeg_and_pdf(self):
        j=self.job(region())
        self.assertEqual(j["state"],"complete")
        self.assertEqual(j["backend"],"mock")
        q="?id="+j["id"]+"&tile=1"
        a,_=self.get("/api/image"+q)
        b,headers=self.get("/api/download"+q+"&format=jpeg&quality=95&sampling=0")
        with Image.open(io.BytesIO(a)) as png,Image.open(io.BytesIO(b)) as jpg:
            self.assertEqual(png.size,jpg.size)
            self.assertEqual(jpg.format,"JPEG")
            self.assertEqual(jpg.info["dpi"],(300,300))
        self.assertIn("attachment",headers["Content-Disposition"])
        self.assertEqual(a,self.get("/api/image"+q)[0])
        self.assertTrue(self.get("/api/download"+q+"&format=pdf")[0].startswith(b"%PDF"))
    def test_split_and_history(self):
        j=self.job(dict(region(),method="split"))
        self.assertEqual(j["state"],"complete")
        self.assertEqual(len(j["results"]),2)
        with zipfile.ZipFile(io.BytesIO(self.get("/api/zip?id="+j["id"])[0])) as z:
            self.assertEqual(set(z.namelist()),{"tile-001.png","tile-002.png","manifest.json"})
            self.assertEqual(z.read("tile-001.png"),self.get("/api/image?id="+j["id"]+"&tile=1")[0])
        self.assertEqual(json.loads(self.get("/api/history")[0])[0]["id"],j["id"])
    def test_validation(self):
        with self.assertRaises(HTTPError) as e:self.get("/api/scan",dict(region(),left=999))
        self.assertEqual(e.exception.code,400)
        with self.assertRaises(HTTPError):self.get("/api/job?id=../")
    def test_batch_mixed_conditions(self):
        a=region()
        b=dict(region(),left=50,dpi=600,format="jpeg",method="split")
        j=self.job({"regions":[a,b]})
        self.assertEqual(j["state"],"complete")
        self.assertEqual([t["roiIndex"] for t in j["results"]],[1,2,2])
        self.assertEqual([t["dpi"] for t in j["results"]],[300,600,600])
        self.assertTrue(j["results"][1]["file"].endswith(".jpg"))
        raw=self.get("/api/download?id="+j["id"]+"&tile=2&format=jpeg")[0]
        with Image.open(io.BytesIO(raw)) as im:self.assertEqual(im.info["dpi"],(600,600))
        with self.assertRaises(HTTPError) as e:self.get("/api/scan",{"regions":[a,dict(b,left=999)]})
        self.assertEqual(e.exception.code,400)
        self.assertEqual(len(json.loads(self.get("/api/history")[0])),1)
    def test_resume_preserves_original_and_only_captures_missing(self):
        capture=self.scanner.capture
        calls=[]
        def fail_once(p,t,path):
            calls.append(t["index"])
            if t["index"]==2 and calls.count(2)==1:
                path.write_bytes(b"partial")
                raise RuntimeError("simulated I/O error")
            return capture(p,t,path)
        self.scanner.capture=fail_once
        j=self.job(dict(region(),method="split"))
        self.assertEqual(j["state"],"failed")
        self.assertEqual(j["failedTile"],2)
        self.assertEqual(calls,[1,2])
        q="?id="+j["id"]+"&tile=1"
        raw=self.get("/api/image"+q)[0]
        self.assertEqual(j["results"][0]["sha256"],hashlib.sha256(raw).hexdigest())
        original=Path(self.tmp.name)/j["id"]/j["results"][0]["file"]
        stamp=original.stat().st_mtime_ns
        self.assertFalse(list(original.parent.glob("*.part")))
        with self.assertRaises(HTTPError): self.get("/api/resume",{"id":j["id"]})
        self.get("/api/resume",{"id":j["id"],"confirmedUnmoved":True})
        for _ in range(100):
            j=json.loads(self.get("/api/job?id="+j["id"])[0])
            if j["state"]!="running":break
            time.sleep(.03)
        self.assertEqual(j["state"],"complete")
        self.assertEqual(j["attempts"],2)
        self.assertEqual(calls,[1,2,2])
        self.assertEqual(stamp,original.stat().st_mtime_ns)
        self.assertEqual(raw,self.get("/api/image"+q)[0])
        self.assertEqual(j["previousAttempts"][0]["failedTile"],2)
        with self.assertRaises(HTTPError): self.get("/api/resume",{"id":j["id"],"confirmedUnmoved":True})

    def test_resume_rejects_tampered_original(self):
        capture=self.scanner.capture
        def fail(p,t,path):
            if t["index"]==2:raise RuntimeError("simulated failure")
            return capture(p,t,path)
        self.scanner.capture=fail
        j=self.job(dict(region(),method="split"))
        path=Path(self.tmp.name)/j["id"]/j["results"][0]["file"]
        with path.open("ab") as f:f.write(b"changed")
        with self.assertRaises(HTTPError) as e:
            self.get("/api/resume",{"id":j["id"],"confirmedUnmoved":True})
        self.assertEqual(e.exception.code,400)

    def test_jpeg_capture(self):
        j=self.job(dict(region(),format="jpeg"))
        self.assertEqual(j["state"],"complete")
        self.assertTrue(j["results"][0]["file"].endswith(".jpg"))

if __name__=="__main__":unittest.main()
