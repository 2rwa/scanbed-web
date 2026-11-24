"""Synthetic full-bed 2400 dpi benchmark. No scanner, no private data."""
import hashlib,json,resource,shutil,struct,subprocess,tempfile,time,zlib
from pathlib import Path
from PIL import Image
from geometry import plan
from mosaic import compose,layout

def sha(path):
    with path.open("rb") as f:return hashlib.file_digest(f,"sha256").hexdigest()

def validate_png(path,info,colors):
    # Our writer uses filter 0. Decode every row and sample the entire canvas.
    w,h=info["width"],info["height"]
    stride=1+w*3
    xs=sorted({0,w-1,*[round(i*(w-1)/12) for i in range(13)]})
    ys=sorted({0,h-1,*[round(i*(h-1)/12) for i in range(13)]})
    wanted=set(ys);row=0;buf=bytearray();decomp=zlib.decompressobj()
    def consume(data):
        nonlocal row,buf
        buf.extend(data)
        pos=0
        while len(buf)-pos>=stride:
            assert buf[pos]==0
            if row in wanted:
                for x in xs:
                    t=next(t for t in reversed(info["tiles"]) if t["box"][0]<=x<t["box"][2] and t["box"][1]<=row<t["box"][3])
                    i=pos+1+x*3
                    assert tuple(buf[i:i+3])==colors[t["index"]],(x,row)
            row+=1;pos+=stride
        if pos: del buf[:pos]
    with path.open("rb") as f:
        assert f.read(8)==b"\x89PNG\r\n\x1a\n"
        while True:
            n=struct.unpack(">I",f.read(4))[0];kind=f.read(4)
            data=f.read(n);crc=struct.unpack(">I",f.read(4))[0]
            assert zlib.crc32(data,zlib.crc32(kind))&0xffffffff==crc
            if kind==b"IHDR":assert struct.unpack(">II",data[:8])==(w,h)
            if kind==b"IDAT":consume(decomp.decompress(data))
            if kind==b"IEND":break
        assert f.read()==b""
    consume(decomp.flush())
    assert row==h and not buf and decomp.eof
    return len(xs)*len(ys)

def main():
    p=plan(dict(left=0,top=0,width=210,height=291,dpi=2400))
    job={"state":"complete","plan":p,"results":[]}
    for t in p["tiles"]:
        job["results"].append(dict(index=t["index"],file=f'tile-{t["index"]:03d}.png',
            widthPx=round(t["width"]*2400/25.4),heightPx=round(t["height"]*2400/25.4)))
    info=layout(job,1)
    w,h=info["width"],info["height"]
    print("BENCHMARK",w,h,w*h,len(p["tiles"]),flush=True)
    report={"source":"synthetic solid-color tile images","width":w,"height":h,"pixels":w*h,"tiles":len(p["tiles"]),"formats":{}}
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);src=root/"source";src.mkdir()
        colors={t["index"]:((t["index"]*47)%256,(t["index"]*97)%256,(t["index"]*151)%256) for t in info["tiles"]}
        for t in job["results"]:
            with Image.new("RGB",(t["widthPx"],t["heightPx"]),colors[t["index"]]) as im:
                im.save(src/t["file"],compress_level=1)
        hashes={f.name:sha(f) for f in src.iterdir()}
        report["sourceBytes"]=sum(f.stat().st_size for f in src.iterdir())
        for fmt in ("png","jpeg"):
            with tempfile.TemporaryDirectory(dir=root) as work:
                free_before=shutil.disk_usage(work).free
                start=time.monotonic()
                path,got=compose(job,src,work,1,False,fmt,95,0)
                sec=time.monotonic()-start
                outsize=path.stat().st_size
                assert (got["width"],got["height"])==(w,h)
                if fmt=="png":
                    checks=validate_png(path,info,colors)
                    validation=f"CRC, all scanlines, {checks} exact RGB samples"
                else:
                    # This trusted synthetic file exceeds Pillow's default decompression-bomb guard.
                    old=Image.MAX_IMAGE_PIXELS
                    try:
                        Image.MAX_IMAGE_PIXELS=None
                        with Image.open(path) as im:
                            from PIL.JpegImagePlugin import get_sampling
                            assert im.size==(w,h) and get_sampling(im)==0
                            assert im.info["dpi"]==(2400,2400)
                    finally:Image.MAX_IMAGE_PIXELS=old
                    subprocess.run(["djpeg","-outfile","/dev/null",str(path)],check=True,timeout=1800)
                    validation="header, DPI, 4:4:4, full decoder"
                assert not (Path(work)/"raster.ppm").exists()
                report["formats"][fmt]={"composeSeconds":round(sec,3),"bytes":outsize,
                    "validation":validation,"freeDiskBeforeBytes":free_before,
                    "estimatedRawPlusOutputBytes":w*h*3+outsize,
                    "pythonPeakRssKiB":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    "childPeakRssKiB":resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}
                print("RESULT",fmt,json.dumps(report["formats"][fmt]),flush=True)
        assert hashes=={f.name:sha(f) for f in src.iterdir()}
    report["originalsUnchanged"]=True
    Path("benchmark-result.json").write_text(json.dumps(report,indent=2)+"\n")
    print("BENCHMARK PASS",flush=True)
if __name__=="__main__":main()
