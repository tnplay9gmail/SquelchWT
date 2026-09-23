"""Build a portable, windowed Windows release ZIP."""
import shutil
import subprocess
import sys
import os
import hashlib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if sys.platform != 'win32':
    raise SystemExit('Windows is required for the Windows release build')

subprocess.run([sys.executable, str(ROOT / 'tools' / 'make_icon.py')], check=True)
dist = ROOT / 'dist'
clean_env = os.environ.copy()
windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
clean_env['PATH'] = os.pathsep.join(map(str, (
    Path(sys.base_prefix), Path(sys.base_prefix) / 'Scripts',
    windows / 'System32', windows,
)))
subprocess.run([
    sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--noupx',
    '--onedir', '--name', 'SquelchWT', '--icon', str(ROOT / 'radio.ico'),
    '--add-data', f'{ROOT / "radio.ico"};.',
    '--add-data', f'{ROOT / "slang.json"};.',
    '--add-data', f'{ROOT / "muted_phrases.json"};.',
    '--add-data', f'{ROOT / "ui" / "chevron.svg"};ui',
    '--collect-data', 'piper',
    '--distpath', str(dist), '--workpath', str(ROOT / 'build'),
    '--specpath', str(ROOT), str(ROOT / 'server.py'),
], check=True, cwd=ROOT, env=clean_env)

package = dist / 'SquelchWT'
for name in ('LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md'):
    shutil.copy2(ROOT / name, package / name)
(package / 'docs').mkdir(exist_ok=True)
shutil.copy2(ROOT / 'docs' / 'screenshot.png', package / 'docs' / 'screenshot.png')
licenses = package / 'licenses'
licenses.mkdir(exist_ok=True)
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    shutil.copy2(python_license, licenses / 'Python-LICENSE.txt')
for distribution in metadata.distributions():
    name = distribution.metadata.get('Name', 'unknown').replace('/', '-')
    for entry in distribution.files or ():
        rel = str(entry).replace('\\', '/')
        if '.dist-info/licenses/' not in rel:
            continue
        source = Path(distribution.locate_file(entry))
        if source.is_file():
            target = licenses / name / rel.split('.dist-info/licenses/', 1)[1]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
archive = shutil.make_archive(str(dist / 'SquelchWT-Windows-x64'), 'zip',
                              root_dir=dist, base_dir='SquelchWT')
digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
(dist / 'SquelchWT-Windows-x64.zip.sha256').write_text(
    f'{digest}  SquelchWT-Windows-x64.zip\n', encoding='ascii')
print(f'Release archive: {archive}')
