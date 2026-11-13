"""Run the remaining already-authorized finite batches; no retry on error."""
import ast
import subprocess
from pathlib import Path

root = Path('/home/orange/Work/enzyflow')
python = '/home/orange/.local/opt/thonny/bin/python3.10'
command = [python, str(root/'tools/board_tool.py'), 'exec', '/tmp/enzyflow_id1_test10_batch.py']
log = Path('/tmp/enzyflow_long101_run.log')
for batch in range(12):
    if Path('/tmp/enzyflow-stop-long101').exists():
        raise RuntimeError('Stop requested; no more movement batches sent')
    result = subprocess.run(command,cwd=root,capture_output=True,text=True,timeout=35)
    with log.open('a') as f:
        f.write(result.stdout + result.stderr)
    print(result.stdout,flush=True)
    if result.returncode:
        raise RuntimeError('Board batch failed, no retry: '+result.stderr[-1000:])
    rows = [s for s in result.stdout.splitlines() if s.startswith('TEST10_BATCH ')]
    if len(rows) != 1:
        raise RuntimeError('Ambiguous result; no retry')
    state = ast.literal_eval(rows[0].split(' ',1)[1])
    assert state['selected_id'] == 1 and state['target_mm'] == 101.0
    assert state['step_attempts'] <= 55 and state['feedback_mm'] <= 101.1
    assert abs(state['encoders'][2] - 17076) <= 640
    assert abs(state['encoders'][3] + 9775) <= 640
    if state['state'] == 'done':
        assert state['all_locked']
        print('LONG101_FINISHED',state,flush=True)
        break
    if state['state'] != 'ready_for_batches':
        raise RuntimeError('Session is not ready; no further commands')
else:
    raise RuntimeError('Host batch budget exhausted; no further commands')
