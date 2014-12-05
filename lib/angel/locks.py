import os
import sys


def get_lock_filename(config, lockname):
    if lockname is None or len(lockname) < 1:
        print >>sys.stderr, 'No lockname given.'
        return None
    return os.path.expanduser(config['LOCK_DIR']) + '/hsn-lock-' + lockname + '.lock'
