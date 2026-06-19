def add(a, b):
    # BUG: broken again for new test - multiplication instead of addition
    return a * b

def test_add():
    assert add(2, 3) == 5
