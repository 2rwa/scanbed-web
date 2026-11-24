"""Read-only overlap diagnostics. Positive correction moves tile B right/down.

No corrections are applied to mosaics. Confidence is a conservative heuristic,
not a probability. Repeated patterns and textureless areas are rejected.
"""
import itertools
from pathlib import Path
import cv2
import numpy as np
from PIL import Image
from mosaic import layout

MAX_SIDE=1024
MAX_PAIRS=512

def estimate(a,b):
    if a.shape!=b.shape or a.ndim!=2: raise ValueError("matching grayscale shapes required")
    h,w=a.shape
    r=min(16,min(h,w)//4)
    if r<2 or min(h,w)-2*r<12:
        return {"status":"insufficient_overlap","searchRadius":r}
    a=np.ascontiguousarray(a,dtype=np.float32)
    b=np.ascontiguousarray(b,dtype=np.float32)
    template=a[r:h-r,r:w-r]
    if float(template.std())<3 or float(b.std())<3:
        return {"status":"low_texture","searchRadius":r}
    scores=cv2.matchTemplate(b,template,cv2.TM_CCOEFF_NORMED)
    _,peak,_,(x,y)=cv2.minMaxLoc(scores)
    alternative=scores.copy()
    alternative[max(0,y-2):y+3,max(0,x-2):x+3]=-2
    second=float(alternative.max())
    margin=float(peak)-second
    # Match in B at (x,y); move B in the opposite direction.
    dx,dy=r-x,r-y
    status="reliable_candidate"
    if min(x,y,2*r-x,2*r-y)==0: status="search_boundary"
    elif peak<.80: status="low_correlation"
    elif margin<.06: status="ambiguous"
    return {"status":status,"searchRadius":r,"score":round(float(peak),5),
            "peakMargin":round(margin,5),"correctionSamplePx":[dx,dy]}

def intersection(a,b):
    x0=max(a[0],b[0]);y0=max(a[1],b[1])
    x1=min(a[2],b[2]);y1=min(a[3],b[3])
    return (x0,y0,x1,y1) if x1>x0 and y1>y0 else None

def sample(folder,t,overlap,size):
    x0,y0,x1,y1=t["box"];a,b,c,d=overlap
    sw,sh=t["sourceSize"]
    box=((a-x0)*sw/(x1-x0),(b-y0)*sh/(y1-y0),
         (c-x0)*sw/(x1-x0),(d-y0)*sh/(y1-y0))
    with Image.open(Path(folder)/t["file"]) as im:
        if list(im.size)!=t["sourceSize"]: raise ValueError("source dimensions changed")
        # Resize from fractional source rectangle; avoid full-resolution
        # numpy arrays. PIL may decode a full source tile.
        with im.resize(size,Image.Resampling.BILINEAR,box=box) as small:
            return np.array(small.convert("L"))

def diagnose(job,folder,roi):
    info=layout(job,roi,False)
    pairs=[]
    for a,b in itertools.combinations(info["tiles"],2):
        overlap=intersection(a["box"],b["box"])
        if overlap is None: continue
        if len(pairs)>=MAX_PAIRS: raise ValueError("too many overlapping pairs")
        x0,y0,x1,y1=overlap;w=x1-x0;h=y1-y0
        scale=min(1,MAX_SIDE/max(w,h))
        sw=max(1,round(w*scale));sh=max(1,round(h*scale))
        if min(sw,sh)<24:
            result={"status":"insufficient_overlap","searchRadius":0}
        else:
            result=estimate(sample(folder,a,overlap,(sw,sh)),sample(folder,b,overlap,(sw,sh)))
        if "correctionSamplePx" in result:
            dx,dy=result["correctionSamplePx"]
            result["correctionNominalPx"]=[round(dx*w/sw,4),round(dy*h/sh,4)]
            result["correctionMm"]=[round(dx*w/sw*25.4/info["outputDpiX"],5),
                                    round(dy*h/sh*25.4/info["outputDpiY"],5)]
        pairs.append({"tileA":a["index"],"tileB":b["index"],"overlapNominalPx":list(overlap),
                      "sampleSize":[sw,sh],**result})
    return {"roiIndex":roi,"pairs":pairs,"reliableCount":sum(p["status"]=="reliable_candidate" for p in pairs),
            "method":"normalized cross correlation, integer sample shifts",
            "correctionConvention":"move B relative to fixed A; right/down positive",
            "applied":False,"calibrated":False}
