def float_round(value, precision_digits=None):
    return round(value, precision_digits or 2)


def float_compare(value1, value2, precision_digits=None):
    """Compare two floats at the given precision."""
    delta = float_round(value1 - value2, precision_digits)
    return (delta > 0) - (delta < 0)
