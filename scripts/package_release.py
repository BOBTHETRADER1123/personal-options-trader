"""Create a source release from an explicit allowlist, excluding all runtime data."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import hashlib
import json

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT.parent/'PersonalOptionsTrader-Source.zip'
TOP={'.gitignore','.gitattributes','.env.example','.python-version','.dockerignore','Dockerfile','pytest.ini','render.yaml','README.md','START-HERE.md','TEST-REPORT.md'}
FRONT={'package.json','pnpm-lock.yaml','pnpm-workspace.yaml','tsconfig.json','vite.config.ts','index.html','vercel.json','.env.example'}
VM={'install.sh','configure.py','Caddyfile.template','app.env.example','personal-options-trader.service',
    'personal-options-trader-proxy.service','personal-options-trader-backup.service',
    'personal-options-trader-backup.timer','backup_daily.py','test_vm_deploy.py'}
RAMNAYM={'import_workspace.py','main.py','runtime.py','start.py','safe_worker.py','gunicorn_conf.py','requirements.txt',
         'package_trial.py','build_frontend.mjs','test_ramnaym.py'}

def allowed(path,root=ROOT):
    parts=path.relative_to(root).parts
    name=path.name
    if '__pycache__' in parts or name.endswith(('.pyc','.tsbuildinfo')):return False
    if name.startswith('.env') and name!='.env.example':return False
    if len(parts)==1:return name in TOP or name.endswith('.cmd')
    if parts[0]=='frontend':return (len(parts)==2 and name in FRONT) or parts[1] in {'src','public','dist'}
    if parts[0]=='backend':return (len(parts)==2 and name in {'requirements.txt','requirements-brokers.txt','.python-version','.env.example','alembic.ini'}) or (parts[1]=='app' and name.endswith('.py')) or (parts[1]=='tests' and len(parts)==3 and name.endswith('.py')) or (parts[1]=='calendars' and len(parts)==3 and name.endswith('.json')) or (parts[1]=='migrations' and name.endswith(('.py','.mako')))
    if parts[0]=='companion':return len(parts)==2 and (name.endswith(('.py','.bat','.txt')) or name=='.env.example')
    if parts[0]=='docs':return name.endswith(('.md','.txt','.png','.svg'))
    if parts[0]=='scripts':return len(parts)==2 and name.endswith('.py')
    if parts[0]=='deploy':return len(parts)==3 and ((parts[1]=='vm' and name in VM) or (parts[1]=='ramnaym' and name in RAMNAYM))
    return False

def main():
    files=sorted(p for p in ROOT.rglob('*') if p.is_file() and allowed(p))
    if not (ROOT/'frontend/dist/index.html').is_file():raise RuntimeError('Build the website before packaging.')
    manifest={str(p.relative_to(ROOT)).replace('\\','/'):{'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files}
    with ZipFile(OUT,'w',ZIP_DEFLATED,compresslevel=9) as archive:
        for path in files:archive.write(path,Path('personal-options-trader')/path.relative_to(ROOT))
        archive.writestr('personal-options-trader/RELEASE-MANIFEST.json',json.dumps(manifest,indent=2))
    with ZipFile(OUT) as archive:
        assert archive.testzip() is None
        names=archive.namelist()
        assert not any('/node_modules/' in n or '/.venv/' in n or '/.qa-' in n or '/OptionsTraderData/' in n or n.endswith(('/.env','.sqlite3','.db','.tmp')) for n in names)
        assert 'personal-options-trader/START-HERE.md' in names
    print(f'Verified source package: {OUT}')
    print(f'{len(files)} files plus checksum manifest; {OUT.stat().st_size:,} bytes; no credentials or runtime database included.')

if __name__=='__main__':main()
