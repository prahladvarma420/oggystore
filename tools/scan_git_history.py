#!/usr/bin/env python3
"""Read-only credential-pattern scan of all unique reachable fetched Git blobs.

Reports locations/types only, never matched values; does not rewrite history.
Exit 1 means findings, 2 scan failure, 0 no pattern matches. Binary/LFS/unfetched
history and arbitrary passwords/seed phrases are outside this limited scan.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
try:
    from . import scan_secrets
except ImportError:  # Direct CLI invocation.
    import scan_secrets

ROOT=Path(__file__).resolve().parents[1]

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,help='Optional metadata-only JSON report')
    args=parser.parse_args(argv)
    findings=[];scanned=0;binary=0
    try:
        rows=subprocess.check_output(['git','rev-list','--objects','--all'],cwd=ROOT,text=True).splitlines()
        with subprocess.Popen(['git','cat-file','--batch'],cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE) as proc:
            for row in rows:
                parts=row.split(' ',1)
                if len(parts)<2:continue
                oid,name=parts
                proc.stdin.write((oid+'\n').encode());proc.stdin.flush()
                header=proc.stdout.readline().decode().split()
                if len(header)!=3:raise ValueError('Unreadable Git object')
                data=proc.stdout.read(int(header[2]));proc.stdout.read(1)
                if header[1]!='blob':continue
                if b'\x00' in data:binary+=1;continue
                try:text=data.decode('utf-8')
                except UnicodeDecodeError:binary+=1;continue
                scanned+=1
                entries=scan_secrets.scan_text(text)
                if Path(name).name in {'.env','m365_session.json','canva_session.json'} and text.strip():
                    entries.append((1,'sensitive-file-history'))
                for line,category in sorted(set(entries)):
                    findings.append(dict(path=name,blob=oid,line=line,category=category))
            proc.stdin.close()
            if proc.wait()!=0:raise ValueError('Git scan failed')
    except (OSError,subprocess.SubprocessError,ValueError):
        print('History scan failed; findings are not complete.',file=sys.stderr);return 2
    result=dict(text_blobs=scanned,binary_or_non_utf8_blobs=binary,findings=findings,
                scope='All unique reachable blobs in fetched refs; patterns only; binary/LFS/unfetched objects not cleared.')
    if args.output:args.output.write_text(json.dumps(result,indent=2)+'\n')
    for item in findings:
        print(f"{item['path']} blob={item['blob']} line={item['line']} category={item['category']} (value withheld)")
    print(f'History scan: {scanned} text blobs, {binary} binary/non-UTF8, {len(findings)} findings.')
    return 1 if findings else 0

if __name__=='__main__':raise SystemExit(main())
