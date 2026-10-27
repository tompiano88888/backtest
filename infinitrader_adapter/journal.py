"""Fsync'ed hash chain; torn writes and configuration drift stop recovery."""
import hashlib
import json
import os
from pathlib import Path


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)


class Journal:
    def __init__(self,path,identity):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.identity=hashlib.sha256(canonical(identity).encode()).hexdigest()
        self.events=[];self.tail='0'*64
        if self.path.exists():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                row=json.loads(line);digest=row.pop('hash')
                expected=hashlib.sha256(canonical(row).encode()).hexdigest()
                if (row['sequence']!=len(self.events) or row['previous']!=self.tail or
                    row['identity']!=self.identity or digest!=expected):
                    raise ValueError('Journal hash, sequence or configuration mismatch')
                self.events.append(row['event']);self.tail=digest

    def append(self,event):
        row=dict(sequence=len(self.events),previous=self.tail,identity=self.identity,event=event)
        row['hash']=hashlib.sha256(canonical(row).encode()).hexdigest()
        with self.path.open('a',encoding='utf-8',newline='\n') as h:
            h.write(canonical(row)+'\n');h.flush();os.fsync(h.fileno())
        self.events.append(event);self.tail=row['hash']
