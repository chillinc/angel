import os

def terminal_stdout_supports_color():
    return os.isatty(1)

def terminal_stderr_supports_color():
    return os.isatty(2)

def terminal_get_size():
    ''' Return a tuple of (width, height, is_true_size); where is_true_size is false when the size is only a guess. '''
    # Based on http://stackoverflow.com/questions/566746/how-to-get-console-window-width-in-python
    env = os.environ
    is_true_size = True
    def ioctl_GWINSZ(fd):
        try:
            import fcntl, termios, struct, os
            cr = struct.unpack('hh', fcntl.ioctl(fd, termios.TIOCGWINSZ, '1234'))
        except:
            return
        return cr
    cr = ioctl_GWINSZ(0) or ioctl_GWINSZ(1) or ioctl_GWINSZ(2)
    if not cr:
        try:
            fd = os.open(os.ctermid(), os.O_RDONLY)
            cr = ioctl_GWINSZ(fd)
            os.close(fd)
        except:
            pass
    if not cr:
        cr = (env.get('LINES', 25), env.get('COLUMNS', 80))
        if 'LINES' not in env or 'COLUMNS' not in env:
            is_true_size = False
    return int(cr[1]), int(cr[0]), is_true_size


def terminal_width():
    """Return the width of the terminal, or best guess when we can't detect it."""
    return terminal_get_size()[0]

def terminal_height():
    """Return the height of the terminal, or the best guess when we can't detect it."""
    return terminal_get_size()[1]

def terminal_width_is_true_size():
    """Return true if our width/height functions are returning best-guesses, instead of detecting it correctly."""
    return terminal_get_size()[2]