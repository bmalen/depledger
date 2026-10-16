import json
import re
import subprocess
import sys

base_ref, repo = sys.argv[1], sys.argv[2]


def show(path):
    return subprocess.run(['git', '-C', repo, 'show', f'{base_ref}:{path}'],
                          check=True, capture_output=True, text=True).stdout


def dump(path, data):
    with open(f'{repo}/{path}', 'w') as fh:
        fh.write(json.dumps(data, indent=4, ensure_ascii=False) + '\n')


composer = json.loads(show('composer.json'))
lock = json.loads(show('composer.lock'))
locked = {p['name']: p['version'].lstrip('v') for p in lock['packages'] + lock['packages-dev']}
for section in ('require', 'require-dev'):
    for name, constraint in composer.get(section, {}).items():
        if name == 'php' or name.startswith('ext-'):
            print(f'composer {name:38} {constraint:12} kept (platform)')
            continue
        pinned = locked[name]
        if pinned != constraint:
            print(f'composer {name:38} {constraint:12} -> {pinned}')
        composer[section][name] = pinned
dump('composer.json', composer)

package = json.loads(show('package.json'))
bun_lock = show('bun.lock')
hoisted = {m[0]: m[2] for m in re.findall(r'^\s{4}"(@?[^"/]+(?:/[^"/@]+)?)": \["(@?[^"@]+)@([^"]+)"', bun_lock, re.M)}
for section in ('dependencies', 'devDependencies'):
    for name, constraint in package.get(section, {}).items():
        pinned = hoisted[name]
        if pinned != constraint:
            print(f'npm      {name:38} {constraint:12} -> {pinned}')
        package[section][name] = pinned
dump('package.json', package)
