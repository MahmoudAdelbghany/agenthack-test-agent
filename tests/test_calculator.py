def add(a, b):
    # Fixed by self-healing agent: now returns the sum
    return a + b

def test_add():
    assert add(2, 3) == 5
