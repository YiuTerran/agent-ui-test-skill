"""Isolated cooperative subprocess fixture; never runs product tests."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def worker(root, identifier):
    deadline = time.monotonic() + 15
    storage = bytearray(64 * 1024)
    try:
        while not (root / (identifier + '.drain')).exists() and time.monotonic() < deadline:
            time.sleep(.02)
            storage[0] = 1
    finally:
        temporary = root / (identifier + '.cleanup.pending')
        temporary.write_text('verified disposable fixture cleanup')
        os.replace(temporary, root / (identifier + '.cleanup'))


def adapter(root):
    root.mkdir(parents=True, exist_ok=True)
    request = json.load(sys.stdin)
    path = root / 'pool.json'
    state = json.loads(path.read_text()) if path.exists() else {
        'controller_id': request['controller_id'], 'workers': [], 'actions': {}}
    if state['controller_id'] != request['controller_id']:
        raise ValueError('fixture owner differs')
    action = request['action']
    response = {'controller_id': state['controller_id']}
    if action == 'inspect':
        rows = [row for row in state['workers']
                if not (root / (row['id'] + '.cleanup')).exists()]
        response.update(workers=rows, pending_tasks=100, available_slots=max(0, 32 - len(rows)))
    else:
        action_id = request['action_id']
        if action_id not in state['actions']:
            if action == 'spawn':
                identifier = 'worker-' + action_id
                process = subprocess.Popen(
                    [sys.executable, __file__, 'worker', str(root), identifier],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                state['workers'].append({'id': identifier, 'pid': process.pid, 'state': 'active',
                                         'started_at': datetime.now(timezone.utc).isoformat()})
            elif action == 'drain':
                row = next(row for row in state['workers'] if row['id'] == request['worker_id'])
                row['state'] = 'draining'
                (root / (row['id'] + '.drain')).touch()
            else:
                raise ValueError('invalid fixture action')
            state['actions'][action_id] = action
            path.write_text(json.dumps(state))
        response.update(ok=True, action_id=action_id)
    print(json.dumps(response))


if __name__ == '__main__':
    if sys.argv[1] == 'worker':
        worker(Path(sys.argv[2]), sys.argv[3])
    else:
        adapter(Path(sys.argv[1]))
