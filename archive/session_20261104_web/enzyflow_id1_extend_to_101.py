# Operator observed ID1 is the long axis and confirmed forward motion.
# Continue the ORIGINAL 51 mm ruler anchor to 101 mm; test travel is included.
# Keep the same live encoder reference and the other axes' original baselines.
# This file only extends the authorized endpoint; it sends no movement.
try:
    assert id1_test10.state == 'ready_for_batches'
    assert id1_test10.long_id == 1
    assert id1_test10.start_mm == 51.0 and id1_test10.target_mm == 61.0
    assert id1_test10.steps == 6
    assert MARGIN_MM == 10.0
    assert id1_test10.controller.axes[1].reference == 9341
    assert id1_test10.baselines == {1: 9341, 2: 17076, 3: -9775}
    id1_test10._check_others()
    current = id1_test10._current()
    assert 51.0 <= current <= 61.0
    id1_test10.controller.axes[1].check_target(101.0)
    id1_test10.target_mm = 101.0
    id1_test10.MAX_STEPS = 55
    print('CONFIRMED_LONG_AXIS_TOTAL_50MM_NO_NEW_MOTION', id1_test10.status())
except BaseException:
    id1_test10._fault()
    raise
