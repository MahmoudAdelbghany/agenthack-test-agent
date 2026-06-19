def add(a, b):
    # BUG: broken in this PR - multiplication instead of addition
    return a * b

def test_add():
    assert add(2, 3) == 5
