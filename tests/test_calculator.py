""
def add(a, b):
    # Bug: returns multiplication instead of addition
    return a * b

def test_add():
    assert add(2, 3) == 5
""
