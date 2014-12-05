
import hashlib
import os
import sys


def get_checksum(string):
    return hashlib.md5(string).hexdigest()


def get_md5_of_path_filestats(p, symlink_loop_detection=20):
    ''' Given a path to a file or directory, return an md5 checksum based on file sizes and file names.
        This will FAIL if there is a change to a file that's identical in size. For virtualenv stuff,
        that shouldn't happen so this should be safe. Using mtime doesn't work well because git checkout
        will update the times on the files. '''
    p = os.path.realpath(os.path.abspath(p))
    checksum_string = None
    if symlink_loop_detection < 0:
        print >>sys.stderr, "Error: symlink loop for path %s? Bailing." % (p)
        return None
    if os.path.isfile(p):
        try:
            s = os.stat(p)
            checksum_string = ','.join((str(s.st_size), os.path.basename(p)))
        except Exception as e:
            print >>sys.stderr, "Warning: can't access file '%s' during checksum lookup; ignoring it (%s)." % (p, e)
    elif os.path.isdir(p):
        try:
            checksum_string = ''
            for f in sorted(os.listdir(p)):
                checksum = get_md5_of_path_filestats(os.path.join(p,f), symlink_loop_detection=symlink_loop_detection-1)
                if checksum is not None:
                    checksum_string += checksum
        except OSError as e:
            print >>sys.stderr, "Warning: can't access directory '%s' during checksum lookup; ignoring it (%s)." % (p, e)
    if checksum_string is None:
        print >>sys.stderr, "Warning: unknown file type at '%s' during checksum lookup; ignoring it." % (p)
        return None
    return hashlib.md5(checksum_string).hexdigest()
    

def get_md5_of_path_contents(path):
    if os.path.isfile(path): return _get_md5_of_file(path)
    if os.path.isdir(path): return _get_md5_of_dir(path)
    print >>sys.stderr, "get_md5_of_path_contents: invalid path '%s'" % path
    return None


def _get_md5_of_file(file):
    ''' Return an md5 hex digest for the given file, or None if not a file. '''
    try:
        md5 = hashlib.md5()
        with open(file, 'rb') as f:  # Skip check if os.path.isfile() to avoid syscall; open() will throw an IOError if it's not a file
            for chunk in iter(lambda: f.read(65536), ''):
                md5.update(chunk)
        return md5.hexdigest()
    except IOError as e:
        print >>sys.stderr, "Error: get_md5_of_file unable to read file %s (%s)." % (file, e)
        return None


def _get_md5_of_dir(path, offset_into_path_string=None):
    # Given a path, return an MD5 hex digest of the contents under that path.
    # Filename changes or file content changes should change the hash;
    # but relocating the path itself most not change the hash.
    if path[-1] == '/': path = path[:-1] # Chomp trailing slash so that checksums work out the same
    if offset_into_path_string is None:
        offset_into_path_string = len(path)

    if not os.path.isdir(path):
        print >>sys.stderr, "Error: _get_md5_of_dir given non-dir path '%s'" % path
        return None 

    checksum_string = ''

    for f in sorted(os.listdir(path)):
        this_path = os.path.join(path,f)
        md5_value = None

        if os.path.isfile(this_path):
            md5_value = _get_md5_of_file(this_path)
        elif os.path.isdir(this_path):
            md5_value = _get_md5_of_dir(this_path, offset_into_path_string=offset_into_path_string)
        else:
            print >>sys.stderr, "Error: unknown file type in _get_md5_of_dir() for file %s" % this_path

        if md5_value is None: return None
        checksum_string += "%s=%s\n" % (this_path[offset_into_path_string:], md5_value)

    return hashlib.md5(checksum_string).hexdigest()

