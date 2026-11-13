import time
assert finish101.state == 'done'
assert all(a.reference is None for a in finish101.controller.axes.values())
assert MARGIN_MM == 10.0
first={i:continuation_bus.read_pulses(i) for i in (1,2,3)}
time.sleep_ms(200)
final_counts={i:continuation_bus.read_pulses(i) for i in (1,2,3)}
for i,expected in ((1,-630662),(2,17076),(3,-9775)):
    assert abs(final_counts[i]-expected)<=640
    assert abs(final_counts[i]-first[i])<=640
print('FINAL_ENCODERS',final_counts)
print('LONG_POSITION_MM',51.0-(final_counts[1]-9341)/12800.0)
print('DONE_ALL_LOCKED_NO_OTHER_AXIS_MOTION')
