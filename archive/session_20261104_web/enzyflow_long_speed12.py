# Explicitly requested speed increase: 6 -> 12 RPM for this bus instance only.
# Keep finite <=1mm moves, conservative existing settling waits and 101mm goal.
try:
    assert id1_test10.state == 'ready_for_batches'
    assert id1_test10.start_mm == 51.0 and id1_test10.target_mm == 101.0
    assert id1_test10.steps == 21
    axis = id1_test10.controller.axes[1]
    assert axis.reference == 9341 and axis.reference_mm == 51.0
    assert axis.sign == -1 and axis.lead_mm == 4
    assert MARGIN_MM == 10.0
    id1_test10._check_others()
    id1_test10._current()
    assert id1_test10_bus.SPEED_RPM == 6
    id1_test10_bus.SPEED_RPM = 12
    print('SPEED_RPM',id1_test10_bus.SPEED_RPM,'POSITION',id1_test10.status())
except BaseException:
    id1_test10._fault()
    raise
