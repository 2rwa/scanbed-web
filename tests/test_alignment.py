import unittest,tempfile
from pathlib import Path
import numpy as np
from PIL import Image
from alignment import estimate,diagnose
from geometry import plan
from mosaic import layout

class AlignmentTests(unittest.TestCase):
    def test_known_translation(self):
        rng=np.random.default_rng(42)
        scene=rng.integers(0,256,(200,240),dtype=np.uint8)
        a=scene[25:153,25:185]
        b=scene[27:155,21:181]
        result=estimate(a,b)
        self.assertEqual(result["status"],"reliable_candidate")
        self.assertEqual(result["correctionSamplePx"],[-4,2])
        self.assertGreater(result["score"],.99)
    def test_flat_and_periodic(self):
        a=np.full((128,128),128,dtype=np.uint8)
        self.assertEqual(estimate(a,a)["status"],"low_texture")
        a=np.tile(np.array([0,255,0,255,255,0,0,255],dtype=np.uint8),(128,16))
        self.assertNotEqual(estimate(a,a)["status"],"reliable_candidate")
    def test_unrelated(self):
        rng=np.random.default_rng(8)
        a=rng.integers(0,256,(128,128),dtype=np.uint8)
        b=rng.integers(0,256,(128,128),dtype=np.uint8)
        self.assertNotEqual(estimate(a,b)["status"],"reliable_candidate")
    def test_actual_pair_crop_and_sign(self):
        p=plan(dict(left=0,top=0,width=40,height=30,dpi=600,method="split"))
        job={"state":"complete","plan":p,"results":[{"index":t["index"],"file":str(t["index"])+".png",
          "widthPx":round(t["width"]*600/25.4),"heightPx":round(t["height"]*600/25.4)} for t in p["tiles"]]}
        info=layout(job,1)
        rng=np.random.default_rng(123)
        scene=rng.integers(0,256,(info["height"]+30,info["width"]+30),dtype=np.uint8)
        with tempfile.TemporaryDirectory() as tmp:
            for t in info["tiles"]:
                x0,y0,x1,y1=t["box"]
                dx,dy=(3,-2) if t["index"]==2 else (0,0)
                crop=scene[y0+10+dy:y1+10+dy,x0+10+dx:x1+10+dx]
                im=Image.fromarray(crop)
                im.resize(tuple(t["sourceSize"]),Image.Resampling.BILINEAR).save(Path(tmp)/t["file"])
            out=diagnose(job,tmp,1)
            self.assertEqual(len(out["pairs"]),1)
            pair=out["pairs"][0]
            self.assertEqual(pair["status"],"reliable_candidate")
            self.assertEqual(pair["correctionNominalPx"],[3,-2])
            self.assertFalse(out["applied"])
