"""CI-only failure injection; never opens physical scanner."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Scanner,make_server
class FaultScanner(Scanner):
    failed=False
    def capture(self,p,t,path):
        if p["dpi"]==400 and t["index"]==2 and not self.failed:
            self.failed=True
            path.write_bytes(b"partial")
            raise RuntimeError("simulated I/O error")
        return super().capture(p,t,path)
make_server("127.0.0.1",18766,FaultScanner(sys.argv[1])).serve_forever()
