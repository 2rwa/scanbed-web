"""Nominal physical geometry. Internal coordinates are integer 0.1mm ticks."""
import math

DPI = (100,150,200,300,400,600,1200,2400,4800,9600)
MODES = ("24bit Color","24bit Color[Fast]","True Gray","Black & White","Gray[Error Diffusion]")
BED = (2100,2910)
LIMIT = 35_000_000
SAFE = 30_000_000
KEYS = ("left","top","width","height")

def ticks(v):
    if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v):
        raise ValueError("finite numeric millimeters required")
    if abs(v)>10000: raise ValueError("coordinate too large")
    return round(v*10)

def pixels(w,h,dpi):
    # Round up nominal pixel dimensions. Actual driver dimensions may differ.
    return math.ceil(w*dpi/254)*math.ceil(h*dpi/254)

def rect(x,y,w,h):
    return dict(zip(KEYS,(x/10,y/10,w/10,h/10)))

def plan(obj):
    if not isinstance(obj,dict): raise ValueError("JSON object required")
    d=obj.get("dpi",300)
    if type(d) is not int or d not in DPI: raise ValueError("unsupported dpi")
    mode=obj.get("mode","24bit Color")
    fmt=obj.get("format","png")
    method=obj.get("method","auto")
    if mode not in MODES or fmt not in ("png","jpeg") or method not in ("auto","single","split"):
        raise ValueError("unsupported setting")
    x,y,w,h=(ticks(obj.get(k)) for k in KEYS)
    if min(x,y)<0 or min(w,h)<30 or x+w>BED[0] or y+h>BED[1]:
        raise ValueError("ROI outside 210 x 291 mm bed or smaller than 3 mm")
    total=pixels(w,h,d)
    if total>1_000_000_000: raise ValueError("job exceeds 1 billion nominal pixels; select a smaller region")
    if method=="single" and total>SAFE: raise ValueError("single scan exceeds conservative 30 MP budget")
    overlap=20 # 2mm total between adjacent tiles
    tiles=[]
    def split(a,b,c,e,force=False):
        if pixels(c,e,d)<=SAFE and not force:
            tiles.append({**rect(a,b,c,e),"pixels":pixels(c,e,d)})
            return
        # Split longest axis, with overlap wholly inside parent.
        if c>=e and c>=80:
            m=c//2
            split(a,b,m+overlap//2,e)
            split(a+m-overlap//2,b,c-m+overlap//2,e)
        elif e>=80:
            m=e//2
            split(a,b,c,m+overlap//2)
            split(a,b+m-overlap//2,c,e-m+overlap//2)
        elif c>=80:
            m=c//2
            split(a,b,m+overlap//2,e)
            split(a+m-overlap//2,b,c-m+overlap//2,e)
        else:
            if pixels(c,e,d)>SAFE: raise ValueError("cannot split safely")
            tiles.append({**rect(a,b,c,e),"pixels":pixels(c,e,d)})
    split(x,y,w,h,method=="split")
    if len(tiles)>256: raise ValueError("too many tiles")
    for i,t in enumerate(tiles,1): t["index"]=i
    return {"region":rect(x,y,w,h),"dpi":d,"mode":mode,"format":fmt,"method":method,
            "pixels":total,"safePixels":SAFE,"hardPixels":LIMIT,"overlapMm":2,
            "maxWidth":min((BED[0]-x)/10,math.floor(SAFE/(math.ceil(h*d/254))*254/d)/10),
            "maxHeight":min((BED[1]-y)/10,math.floor(SAFE/(math.ceil(w*d/254))*254/d)/10),
            "tiles":tiles}
