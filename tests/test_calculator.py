""
def add(a, b):
    # Fix: returns the sum of two numbers instead of their product
    return a + b

def test_add():
    assert add(2, 3) == 5
""