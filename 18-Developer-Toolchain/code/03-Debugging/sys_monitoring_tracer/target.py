"""Code under observation. Line numbers inside each function are reported
relative to its `def` line, so editing this file shifts nothing else."""


def classify(n):
    if n < 0:
        return "negative"
    if n == 0:
        return "zero"
    return "positive"


def fib(n):
    if n < 2:
        return n
    return fib(n - 1) + fib(n - 2)


def countdown(n):
    while n > 0:
        yield n
        n -= 1


def hot_loop(n):
    t = 0
    for i in range(n):
        if i % 3 == 0:
            t += i
        else:
            t -= 1
    return t


def risky(n):
    caught = 0
    for i in range(n):
        try:
            raise KeyError(i)
        except KeyError:
            caught += 1
    return caught


def tiny():
    x = 1
    return x


def call_loop(n):
    t = 0
    for _ in range(n):
        t += tiny()
    return t


def inner(n):
    raise KeyError(n)


def middle(n):
    try:
        inner(n)
    finally:
        pass


def outer(n):
    try:
        middle(n)
    except KeyError:
        return "handled"


def echo():
    try:
        yield 1
    except ValueError:
        yield 2


def throw_into_echo():
    gen = echo()
    next(gen)
    return gen.throw(ValueError("thrown"))


class Box:
    def get(self, key):
        return key


def call_kinds():
    box = Box()
    len("abc")
    tiny()
    box.get(5)
    try:
        int("x")
    except ValueError:
        pass
