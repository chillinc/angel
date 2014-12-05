
# This is a very light-weight persistent cache setup for devops use. This cache setup stores one file 
# per key/value pair, typically under tmpfs (ram-backed / reset on reboot) so that data can be stored and accessed 
# between runs, and without relying on any running services.
#
# Key/value pairs are expected to be simple types (strings, ints, or booleans). We're not pickling 
# anything here specifically so that non-python processes can read the data, should we ever need to. We 
# do, however, store the type info and cast back to those types, so that programming errors for failing 
# to cast to ints don't cause unexpected bugs.
#
# This is primarily meant for monitoring-level data, where we can't rely on services. (If we can't rely 
# on the filesystem, we have other problems...)
#
# It is not meant to be used for large numbers of keys or rapidly changing data.


import os
import random
import string
import sys
import time


def simple_cache_set(key, value, ttl_in_seconds=None):
    ''' Set the given key to the given value, optionally marking an expiry time. Give a value of None to clear a previous cache set. '''
    f = _simple_cache_get_filename(key)
    if f is None:
        return -1
    if value is None:
        if os.path.isfile(f):
            try:
                os.remove(f)
            except Exception as e:
                print >>sys.stderr, "Error: unable to remove simple cache file %s: %s" % (f, e)
                return -2
        return 0
    set_time = int(time.time())
    expiry = ''
    if ttl_in_seconds is not None:
        expiry = int(time.time() + ttl_in_seconds)
    type_str = type(value).__name__
    try:
        tmp_file = "%s-%s" % (f, str(random.random())[2:])   # Avoid race condition of partially-written file being read; avoid symlink overwrite scenario
        open(tmp_file, 'w').write("%s\n%s\n%s\n%s" % (set_time, expiry, type_str, value))
        os.rename(tmp_file, f)
    except Exception as e:
        print >>sys.stderr, "Error: unable to update simple cache file %s: %s" % (f, e)
        return -3
    return 0


def simple_cache_get(key, max_allowed_age_in_seconds=None, default=None, get_function=None, get_function_args=(), get_function_kwargs={}, get_function_ttl_in_seconds=None):
    ''' Get the given key from simple cache.
        If the key isn't set and get_function is defined, call get_function(); if get_function returns a value, store the key for future gets, and return it.
        If max_allowed_age_in_seconds is given, and the value was set before that time, default is returned. Note that this does not clear the cache value; future calls with longer durations will still return the stored value.
        If default is given, then it is returned when no value can be found; it is never stored.
    '''

    if get_function is not None:
        # Recurse through without the function:
        value = simple_cache_get(key)
        if value is not None:
            return value
        # If we get here, then we have a function to get the value, and it is not currently set.
        value = get_function(*get_function_args, **get_function_kwargs)
        if value is None:
            return default
        simple_cache_set(key, value, ttl_in_seconds=get_function_ttl_in_seconds)
        return value

    f = _simple_cache_get_filename(key)
    if f is None:
        return default
    try:
        if os.getuid() != os.stat(f).st_uid:
            print >>sys.stderr, "Error: simple cache file %s owner mismatch (%s/%s)." % (f, os.getuid(), os.stat(f).st_uid)
            return default
    except:
        # There's a race condition where checking if the file exists can work, but the cache can then timeout.
        # Don't bother checking if file exists before stating it.
        return default

    raw_data = None
    try:
        raw_data = open(f).read()
    except:
        # Likewise, race condition where the file might have just been deleted
        return default

    try:
        set_time, expiry, type_str, value = raw_data.split('\n',3)
        if len(expiry):
            if time.time() > int(expiry):
                # Race condition of another process removing it; so remove; ignore errors; then check if it exists
                try:
                    os.remove(f)
                except Exception as e:
                    pass
                if os.path.isfile(f):
                    print >>sys.stderr, "Error: unable to remove expired simple cache file %s (maybe another process re-added it?)" % (f)
                return default
        if max_allowed_age_in_seconds is not None:
            current_age = time.time() - int(set_time)
            if current_age > max_allowed_age_in_seconds:
                return default
        return eval(type_str)(value)
    except Exception as e:
        try:
            os.remove(f)
            print >>sys.stderr, "Error: unable to parse simple cache file %s (%s); removing it." % (f, e)
        except Exception as e2:
            print >>sys.stderr, "Error: unable to parse simple cache file %s (%s); unable to remove it (%s)." % (f, e, e2)
            
    return default


def _simple_cache_get_filename(key):
    if key is None:
        print >>sys.stderr, "Error: simple cache given 'None' key."
        return None
    base_dir = '/dev/shm'
    if not os.path.isdir(base_dir):
        if 'TMPDIR' in os.environ:
            base_dir = os.environ['TMPDIR']
    if not os.path.isdir(base_dir):
        base_dir = '/tmp'
    valid_chars = "-_%s%s" % (string.ascii_letters, string.digits)
    key = key.lower().replace('/','-')
    filename_safe_key = ''.join(c for c in key if c in valid_chars)
    return os.path.join(base_dir, 'angel-simplecache-1-%s-%s' % (os.getuid(), filename_safe_key[0:64]))
