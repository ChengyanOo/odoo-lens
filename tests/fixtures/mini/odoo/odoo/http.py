class Controller:
    pass


def route(route=None, **kw):
    return lambda f: f


request = None
