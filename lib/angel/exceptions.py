
class AngelException(Exception):
    """ Base class for all Angel exceptions."""
    pass


class AngelExpectedException(AngelException):
    """Thrown when we encounter an error that happens isn't a coding error."""
    pass


class AngelUnexpectedException(AngelException):
    """Thrown when we encounter an error that we don't expect -- e.g. where we want to see the stacktrace to debug."""
    pass


class AngelVersionException(AngelExpectedException):
    """Thrown for errors around version management."""
    pass


class AngelSettingsException(AngelExpectedException):
    """Thrown when there is an error handling settings."""
    pass


class AngelArgException(AngelExpectedException):
    """Thrown when command-line input options aren't valid."""
    pass


