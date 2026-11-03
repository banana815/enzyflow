import importlib.util
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'firmware/lib/enzyflow/processing.py'
spec=importlib.util.spec_from_file_location('processing',p)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
d=m.BlockDetector(100)
assert [d.update(v) for v in (19,21,19,19,19)]==[False,False,False,False,True]
assert [d.update(v) for v in (25,34,36,34,36,36,36)]==[True,True,True,True,True,True,False]
assert m.estimate_rgb((0,)*8)==(0,0,0)
assert m.estimate_rgb((10,)*8)==(255,255,255)
assert m.estimate_rgb((0,0,0,0,0,10,10,10))==(255,0,0)
print('Processing tests passed: debounce, hysteresis, dark/neutral/red inputs')
