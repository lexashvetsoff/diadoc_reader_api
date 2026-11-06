from __future__ import annotations

import re
import struct
from pathlib import Path

_END = 0xFFFFFFFE
_FREE = 0xFFFFFFFF

def _u16(b, o=0): return struct.unpack_from('<H', b, o)[0]
def _u32(b, o=0): return struct.unpack_from('<I', b, o)[0]
def _i32(b, o=0): return struct.unpack_from('<i', b, o)[0]


def _workbook_stream(source):
    b = source if isinstance(source, bytes) else Path(source).read_bytes()
    if b[:8] != bytes.fromhex('d0cf11e0a1b11ae1'):
        raise ValueError('Not OLE2 legacy .xls')
    sector_size = 1 << _u16(b,30)
    mini_size = 1 << _u16(b,32)
    def sec(s): return b[(s+1)*sector_size:(s+2)*sector_size]
    difat = [_u32(b,76+i*4) for i in range(109)]
    difat = [s for s in difat if s != _FREE]
    next_difat = _u32(b,68)
    for _ in range(_u32(b,72)):
        part=sec(next_difat)
        difat.extend(_u32(part,i*4) for i in range(sector_size//4-1) if _u32(part,i*4)!=_FREE)
        next_difat=_u32(part,sector_size-4)
    fat=b''.join(sec(s) for s in difat)
    def nxt(s): return _u32(fat,s*4)
    def chain(first,size=None):
        out=[]; s=first; seen=set()
        while s < 0xfffffffa and s not in seen:
            seen.add(s); out.append(sec(s)); s=nxt(s)
        data=b''.join(out)
        return data[:size] if size is not None else data
    directory=chain(_u32(b,48))
    entries=[]
    for off in range(0,len(directory),128):
        e=directory[off:off+128]
        if len(e)!=128 or not e[66]:continue
        namelen=_u16(e,64)
        name=e[:namelen-2].decode('utf-16le',errors='replace') if namelen>=2 else ''
        entries.append((name,e[66],_u32(e,116),struct.unpack_from('<Q',e,120)[0]))
    target=next((e for e in entries if e[0].casefold() in ('workbook','book')),None)
    if target is None: raise ValueError('Workbook stream not found')
    if target[3]>=_u32(b,56): return chain(target[2],target[3])
    root=next(e for e in entries if e[1]==5)
    minifat_stream=chain(_u32(b,60),_u32(b,64)*sector_size)
    ministream=chain(root[2],root[3]); out=[]; s=target[2]; seen=set()
    while s<0xfffffffa and s not in seen:
        seen.add(s); out.append(ministream[s*mini_size:(s+1)*mini_size]); s=_u32(minifat_stream,s*4)
    return b''.join(out)[:target[3]]


def _records(data):
    pos=0
    while pos+4<=len(data):
        typ,length=struct.unpack_from('<HH',data,pos); pos+=4
        if pos+length>len(data): break
        yield typ,data[pos:pos+length],pos-4
        pos+=length


def _shared_strings(parts):
    # Each CONTINUE record may restart a string with a fresh compression flag.
    # For standard BIFF8 files the strings are commonly contained in a single
    # SST payload. Decode both ASCII-compressed and UTF-16 variants.
    data=b''.join(parts)
    if len(data)<8:return []
    count=_u32(data,4); p=8; result=[]
    for _ in range(min(count,1000000)):
        if p+3>len(data):break
        length=_u16(data,p); opts=data[p+2]; p+=3
        rich=_u16(data,p) if opts&8 else 0
        if opts&8:p+=2
        ext=_u32(data,p) if opts&4 else 0
        if opts&4:p+=4
        charsize=2 if opts&1 else 1
        raw=data[p:p+length*charsize]
        result.append(raw.decode('utf-16le' if charsize==2 else 'latin1',errors='replace'))
        p+=length*charsize+rich*4+ext
    return result


def _col(n):
    s=''; n+=1
    while n: n,k=divmod(n-1,26); s=chr(65+k)+s
    return s


def _rk(raw):
    val=raw>>2
    if raw&2:
        if val&0x20000000: val-=0x40000000
        value=float(val)
    else: value=struct.unpack('<d',struct.pack('<Q',(raw&0xfffffffc)<<32))[0]
    return value/100 if raw&1 else value


def _text_num(x):
    return str(int(x)) if isinstance(x,(float,int)) and float(x).is_integer() else str(x)


def read_sheets(source):
    stream=_workbook_stream(source)
    records=list(_records(stream))
    # SST is global; collect all CONTINUE fragments immediately following SST.
    strings=[]; sheets=[]
    for i,(typ,payload,_) in enumerate(records):
        if typ==0x00fc:
            chunks=[payload]; j=i+1
            while j<len(records) and records[j][0]==0x003c:
                chunks.append(records[j][1]); j+=1
            strings=_shared_strings(chunks)
    rows={}; in_sheet=False
    for typ,d,_ in records:
        if typ==0x0809 and len(d)>=4:
            kind=_u16(d,2)
            if kind==0x0010:
                rows={};in_sheet=True
            elif in_sheet:
                sheets.append([(r,{_col(c):v for c,v in sorted(vals.items())}) for r,vals in sorted(rows.items()) if vals]); in_sheet=False
            continue
        if not in_sheet:continue
        if typ==0x000a:
            sheets.append([(r,{_col(c):v for c,v in sorted(vals.items())}) for r,vals in sorted(rows.items()) if vals]); in_sheet=False;continue
        def put(r,c,v):
            if v is not None and str(v).strip(): rows.setdefault(r+1,{})[c]=str(v).strip()
        if typ==0x00fd and len(d)>=10:
            r,c,xf,idx=struct.unpack_from('<HHHI',d)
            put(r,c,strings[idx] if idx<len(strings) else '')
        elif typ==0x0203 and len(d)>=14:
            r,c,xf,val=struct.unpack_from('<HHHd',d);put(r,c,_text_num(val))
        elif typ==0x027e and len(d)>=10:
            r,c,xf,raw=struct.unpack_from('<HHHI',d);put(r,c,_text_num(_rk(raw)))
        elif typ==0x00bd and len(d)>=6:
            r,cfirst=struct.unpack_from('<HH',d); clast=_u16(d,len(d)-2)
            for c in range(cfirst,clast+1):
                off=4+(c-cfirst)*6
                if off+6<=len(d)-2: put(r,c,_text_num(_rk(_u32(d,off+2))))
        elif typ==0x0204 and len(d)>=8:
            r,c,xf,length=struct.unpack_from('<HHHH',d);put(r,c,d[8:8+length].decode('latin1',errors='replace'))
        elif typ==0x0006 and len(d)>=14:
            r,c,xf=struct.unpack_from('<HHH',d)
            # Formula can return special markers; retain only numeric values
            if d[12:14]!=b'\xff\xff':put(r,c,_text_num(struct.unpack_from('<d',d,6)[0]))
    if in_sheet: sheets.append([(r,{_col(c):v for c,v in sorted(vals.items())}) for r,vals in sorted(rows.items()) if vals])
    return sheets
