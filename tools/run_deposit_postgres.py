#!/usr/bin/env python3
"""Native integration gate: creates and destroys a PRIVATE local PostgreSQL cluster.

Requires initdb/pg_ctl/createdb installed and project Python dependencies. Does not
accept or inherit production DATABASE_URL. No TCP listener, only a mode-0700 Unix
socket directory. Run as an ordinary non-root user from any working directory.
"""
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile


def main():
    root=Path(__file__).resolve().parents[1]
    def find_binary(name):
        direct=shutil.which(name)
        if direct:return direct
        for folder in sorted(Path('/usr/lib/postgresql').glob('*/bin'),reverse=True):
            candidate=folder/name
            if candidate.is_file() and os.access(candidate,os.X_OK):return str(candidate)
        return None
    bins={name:find_binary(name) for name in ('initdb','pg_ctl','createdb')}
    if not all(bins.values()) or not hasattr(os,'geteuid') or os.geteuid()==0:
        print('Native PostgreSQL gate requires installed server tools and a non-root user.')
        return 2
    with tempfile.TemporaryDirectory(prefix='oggy-native-pg-') as tmp:
        work=Path(tmp);os.chmod(work,0o700)
        data=work/'data';socket=work/'socket';socket.mkdir(mode=0o700)
        nonce=secrets.token_hex(24);(work/'owner-marker').write_text(nonce)
        env={key:value for key,value in os.environ.items() if key in {
            'PATH','HOME','LANG','LC_ALL','LC_CTYPE','TMPDIR','LD_LIBRARY_PATH'}}
        for key in list(env):
            if key.startswith('PG') or key in ('DATABASE_URL','BOT_TOKEN'):
                env.pop(key,None)
        env.update(PYTHON_DOTENV_DISABLED='1',BOT_TOKEN='',ADMIN_ID='0',
                   DATA_ENCRYPTION_KEY='test-only-native-pg-encryption')
        started=False
        try:
            with (work/'init.log').open('w') as log:
                subprocess.run([bins['initdb'],'-D',str(data),'-A','trust','-U','oggy_test','--no-locale','-E','UTF8'],
                               env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=60)
            subprocess.run([bins['pg_ctl'],'-D',str(data),'-l',str(work/'server.log'),'-o',
                            f"-k {socket} -c listen_addresses='' -c max_connections=30",'-w','start'],
                           env=env,check=True,timeout=60,stdout=subprocess.DEVNULL)
            started=True
            subprocess.run([bins['createdb'],'-h',str(socket),'-U','oggy_test','oggystore_invoice_test'],
                           env=env,check=True,timeout=30)
            env.update(DATABASE_URL=f'postgresql:///oggystore_invoice_test?host={socket}&user=oggy_test',
                       OGGY_NATIVE_PG_ROOT=str(work),OGGY_NATIVE_PG_NONCE=nonce)
            result=subprocess.run([sys.executable,'-m','unittest','-v','tests.test_deposit_postgres'],
                                  cwd=root,env=env,timeout=240)
            return result.returncode
        except (subprocess.SubprocessError,OSError):
            print('Native PostgreSQL gate failed; no production database was used.')
            for name in ('init.log','server.log'):
                path=work/name
                if path.exists():print('\n'.join(path.read_text(errors='replace').splitlines()[-8:]))
            return 1
        finally:
            if started or (data/'postmaster.pid').exists():
                subprocess.run([bins['pg_ctl'],'-D',str(data),'-m','immediate','-w','stop'],
                               env=env,timeout=30,stdout=subprocess.DEVNULL,check=False)


if __name__=='__main__':raise SystemExit(main())
