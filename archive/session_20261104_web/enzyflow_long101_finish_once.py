
try:
    for batch_number in range(10):
        result = finish101.batch()
        print('CONTINUATION_BATCH',result)
        if result['state'] == 'done':
            break
    else:
        raise ValueError('Continuation batch budget exhausted')
    assert finish101.state == 'done'
    print('LONG101_FINISHED_ALL_LOCKED',finish101.status())
except BaseException:
    finish101._fault()
    raise
