import struct, sys, os

CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts', b'dinf', b'udta', b'mvex', b'moof', b'traf'}

def scan_top(f, size):
    pos = 0
    out = []
    guard = 0
    while pos + 8 <= size and guard < 10000:
        guard += 1
        f.seek(pos)
        hdr = f.read(16)
        if len(hdr) < 8:
            break
        sz = struct.unpack('>I', hdr[:4])[0]
        typ = hdr[4:8]
        hs = 8
        if sz == 1:
            if len(hdr) < 16:
                break
            sz = struct.unpack('>Q', hdr[8:16])[0]
            hs = 16
        elif sz == 0:
            sz = size - pos
        out.append({'type': typ.decode('latin-1', 'replace'), 'pos': pos, 'size': sz, 'hdr': hs})
        if sz < hs or pos + sz > size:
            out[-1]['bad'] = True
            break
        pos += sz
    return out

def walk_box(data, start, end, depth=0):
    pos = start
    out = []
    while pos + 8 <= end:
        sz = struct.unpack('>I', data[pos:pos+4])[0]
        typ = data[pos+4:pos+8]
        hs = 8
        if sz == 1:
            if pos + 16 > end:
                break
            sz = struct.unpack('>Q', data[pos+8:pos+16])[0]
            hs = 16
        elif sz == 0:
            sz = end - pos
        node = {'type': typ, 'pos': pos, 'size': sz, 'hdr': hs}
        if typ in CONTAINERS:
            node['children'] = walk_box(data, pos + hs, pos + sz, depth + 1)
        if sz < hs or pos + sz > end:
            node['bad'] = True
            out.append(node)
            break
        out.append(node)
        pos += sz
    return out

def find(node, name):
    out = []
    if node['type'] == name:
        out.append(node)
    for c in node.get('children', []):
        out.extend(find(c, name))
    return out

def probe(path):
    size = os.path.getsize(path)
    print(f"=== {os.path.basename(path)} ===")
    print(f"bytes: {size:,}  ({size/1048576:.2f} MB)")
    with open(path, 'rb') as f:
        top = scan_top(f, size)
        print("--- top-level boxes ---")
        for b in top:
            print(f"  {b['type']:<6} off={b['pos']:>14,} size={b['size']:>14,}" + ('  <BAD/TRUNCATED>' if b.get('bad') else ''))

        moovb = next((b for b in top if b['type'] == 'moov'), None)
        if not moovb:
            print("!! NO MOOV -> not playable")
            return
        f.seek(moovb['pos'])
        m = f.read(moovb['size'])
        moov = walk_box(m, 0, len(m))
        if len(moov) == 1 and moov[0]['type'] == b'moov':
            mr = moov[0]
        else:
            mr = {'type': b'moov', 'children': moov, 'pos': 0, 'size': len(m), 'hdr': 0}

        traks = [c for c in mr.get('children', []) if c['type'] == b'trak']
        print(f"--- tracks: {len(traks)} ---")
        for i, tr in enumerate(traks):
            hdlr = find(tr, b'hdlr')
            codec = '?'
            wh = ''
            hname = '?'
            if hdlr:
                hp = hdlr[0]['pos']
                hname = m[hp+16:hp+20].decode('latin-1', 'replace').strip('\x00')
            stsd = find(tr, b'stsd')
            if stsd:
                sp = stsd[0]['pos']
                try:
                    codec = m[sp+20:sp+24].decode('latin-1', 'replace')
                    w = struct.unpack('>H', m[sp+32:sp+34])[0]
                    h = struct.unpack('>H', m[sp+34:sp+36])[0]
                    wh = f" {w}x{h}"
                except Exception:
                    pass
            mdhd = find(tr, b'mdhd')
            durstr = ''
            if mdhd:
                mp = mdhd[0]['pos']
                ver = m[mp+8]
                if ver == 0:
                    ts = struct.unpack('>I', m[mp+20:mp+24])[0]
                    du = struct.unpack('>I', m[mp+24:mp+28])[0]
                else:
                    ts = struct.unpack('>I', m[mp+28:mp+32])[0]
                    du = struct.unpack('>Q', m[mp+32:mp+40])[0]
                d = du / ts if ts else 0
                durstr = f"  duration={d:.2f}s ({d/60:.2f}min) timescale={ts}"
            stsz = find(tr, b'stsz')
            nsamp = ''
            if stsz:
                zp = stsz[0]['pos']
                nsamp = f"  samples={struct.unpack('>I', m[zp+16:zp+20])[0]}"
            print(f"  trak[{i}] handler={hname} codec={codec}{wh}{durstr}{nsamp}")
        mvhd = find(mr, b'mvhd')
        if mvhd:
            p = mvhd[0]['pos']
            ver = m[p+8]
            if ver == 0:
                ts = struct.unpack('>I', m[p+20:p+24])[0]
                du = struct.unpack('>I', m[p+24:p+28])[0]
            else:
                ts = struct.unpack('>I', m[p+28:p+32])[0]
                du = struct.unpack('>Q', m[p+32:p+40])[0]
            d = du / ts if ts else 0
            print(f"--- mvhd: duration={d:.2f}s ({d/60:.2f}min) timescale={ts} ---")
    print()

if __name__ == '__main__':
    for p in sys.argv[1:]:
        probe(p)
