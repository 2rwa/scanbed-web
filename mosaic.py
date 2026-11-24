"""Derived nominal-coordinate mosaic, never a calibrated optical reconstruction.

Full PNG is streamed from a disk-backed RGB raster. Only one source tile and
a bounded number of rows are in memory at once. Overlap uses plan order
(last tile wins); no implicit registration, blending or original changes.
"""
import math, shutil, struct, subprocess, zlib
from pathlib import Path
from PIL import Image

MAX_PIXELS=1_000_000_000
PREVIEW_SIDE=1600

def layout(job,roi_index,preview=False):
    if job["state"]!="complete": raise ValueError("complete scan required")
    p=job["plan"]
    regions=p.get("regions") or [p]
    if not 1<=roi_index<=len(regions): raise ValueError("invalid ROI")
    r=regions[roi_index-1]
    g=r["region"];d=r["dpi"]
    full_w=round(g["width"]*d/25.4);full_h=round(g["height"]*d/25.4)
    if min(full_w,full_h)<1 or full_w*full_h>MAX_PIXELS:
        raise ValueError("mosaic exceeds size limit")
    scale=min(1,PREVIEW_SIDE/max(full_w,full_h)) if preview else 1
    w=max(1,round(full_w*scale));h=max(1,round(full_h*scale))
    results={t["index"]:t for t in job["results"]}
    tiles=[]
    for t in p["tiles"]:
        if t.get("roiIndex",1)!=roi_index: continue
        a=results.get(t["index"])
        if a is None: raise ValueError("missing tile")
        # Shared edges use the same integer rounding relative to ROI origin.
        x0=round((t["left"]-g["left"])*w/g["width"])
        y0=round((t["top"]-g["top"])*h/g["height"])
        x1=round((t["left"]+t["width"]-g["left"])*w/g["width"])
        y1=round((t["top"]+t["height"]-g["top"])*h/g["height"])
        if not (0<=x0<x1<=w and 0<=y0<y1<=h): raise ValueError("tile outside mosaic")
        tiles.append({"index":t["index"],"box":[x0,y0,x1,y1],"file":a["file"],
                      "sourceSize":[a["widthPx"],a["heightPx"]],
                      "resampled":a["widthPx"]!=x1-x0 or a["heightPx"]!=y1-y0})
    if not tiles: raise ValueError("no tiles")
    # Sweep the tile boundaries to prove all output pixels are covered.
    ys=sorted({0,h,*[y for t in tiles for y in (t["box"][1],t["box"][3])]})
    for lo,hi in zip(ys,ys[1:]):
        spans=sorted((x0,x1) for x0,y0,x1,y1 in (t["box"] for t in tiles) if y0<=lo and y1>=hi)
        end=0
        for a,b in spans:
            if a>end: raise ValueError("gap in mosaic")
            end=max(end,b)
        if end!=w: raise ValueError("gap in mosaic")
    return {"roiIndex":roi_index,"width":w,"height":h,"nominalDpi":d,
            "outputDpiX":w*25.4/g["width"],"outputDpiY":h*25.4/g["height"],
            "preview":preview,"tiles":tiles,"method":"nominal coordinates, last tile wins",
            "calibrated":False}

def chunk(f,kind,data):
    f.write(struct.pack(">I",len(data)));f.write(kind);f.write(data)
    f.write(struct.pack(">I",zlib.crc32(data,zlib.crc32(kind))&0xffffffff))

def jpeg_density(path,dx,dy):
    # cjpeg writes JFIF APP0 immediately after SOI. Verify before editing metadata.
    with path.open("r+b") as f:
        header=f.read(20)
        if header[:4]!=b"\xff\xd8\xff\xe0" or header[6:11]!=b"JFIF\x00" or len(header)<20:
            raise ValueError("unexpected JPEG header")
        f.seek(13)
        f.write(struct.pack(">BHH",1,max(1,round(dx)),max(1,round(dy))))

def compose(job,folder,work,roi_index,preview=False,fmt="png",quality=95,sampling=0):
    info=layout(job,roi_index,preview)
    w,h=info["width"],info["height"]
    if fmt not in ("png","jpeg") or not 1<=quality<=100 or sampling not in (0,1,2):
        raise ValueError("invalid mosaic export settings")
    if fmt=="jpeg":
        if max(w,h)>65500: raise ValueError("JPEG dimension limit; use PNG")
        if not shutil.which("cjpeg"): raise ValueError("JPEG mosaic requires cjpeg (libjpeg-turbo-progs)")
    work=Path(work)
    # A raw RGB raster plus worst-case PNG and compression overhead.
    if shutil.disk_usage(work).free<w*h*7+50_000_000: raise ValueError("insufficient mosaic disk space")
    raw=work/"raster.ppm";out=work/("mosaic.jpg" if fmt=="jpeg" else "mosaic.png")
    header=f"P6\n{w} {h}\n255\n".encode("ascii")
    offset=len(header)
    with raw.open("w+b") as f:
        f.write(header)
        f.truncate(offset+w*h*3)
        for t in info["tiles"]:
            x0,y0,x1,y1=t["box"];tw=x1-x0;th=y1-y0
            path=Path(folder)/t["file"]
            with Image.open(path) as im:
                if list(im.size)!=t["sourceSize"]: raise ValueError("source dimensions changed")
                rgb=im.convert("RGB")
                if rgb.size!=(tw,th):
                    resized=rgb.resize((tw,th),Image.Resampling.LANCZOS)
                    rgb.close();rgb=resized
                # Write 16-row stripes to avoid a second full-tile byte buffer.
                try:
                    for y in range(0,th,16):
                        n=min(16,th-y)
                        with rgb.crop((0,y,tw,y+n)) as band: data=band.tobytes()
                        for k in range(n):
                            f.seek(offset+((y0+y+k)*w+x0)*3)
                            f.write(data[k*tw*3:(k+1)*tw*3])
                finally: rgb.close()
        f.flush()
        if fmt=="jpeg":
            subs=("1x1,1x1,1x1","2x1,1x1,1x1","2x2,1x1,1x1")[sampling]
            # Baseline sequential compression avoids the full-image coefficient buffer.
            result=subprocess.run(["cjpeg","-quality",str(quality),"-sample",subs,
                                   "-baseline","-outfile",str(out),str(raw)],
                                  capture_output=True,timeout=1800)
            if result.returncode: raise ValueError("JPEG encoder failed: "+result.stderr.decode(errors="replace")[-300:])
            jpeg_density(out,info["outputDpiX"],info["outputDpiY"])
            raw.unlink()
            return out,info
        f.seek(offset)
        with out.open("wb") as png:
            png.write(b"\x89PNG\r\n\x1a\n")
            chunk(png,b"IHDR",struct.pack(">IIBBBBB",w,h,8,2,0,0,0))
            chunk(png,b"pHYs",struct.pack(">IIB",round(info["outputDpiX"]/0.0254),round(info["outputDpiY"]/0.0254),1))
            chunk(png,b"tEXt",b"Comment\x00Derived nominal mosaic; uncalibrated; overlap last tile wins")
            c=zlib.compressobj(3)
            for _ in range(h):
                row=f.read(w*3)
                if len(row)!=w*3: raise ValueError("incomplete raster")
                data=c.compress(b"\x00"+row)
                if data: chunk(png,b"IDAT",data)
            data=c.flush()
            if data: chunk(png,b"IDAT",data)
            chunk(png,b"IEND",b"")
    raw.unlink()
    return out,info
