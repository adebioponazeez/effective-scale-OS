import hashlib
from pathlib import Path

def sha256_file(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
    return h.hexdigest()

def snapshot(path):
    p=Path(path)
    return {"path":str(p),"exists":p.exists(),"hash":sha256_file(p) if p.is_file() else None}
