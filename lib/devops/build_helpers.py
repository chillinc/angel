
# Helper functions for caching build output for future runs.

import hashlib
import os
import re
import shutil
import sys
import time

import angel.util.checksum


def devops_build_cache_create(name, checksum, input_dir):
    ''' Given a build system (dojo, virtualenv, css), a unique checksum, and an input dir,
        create a cache that can be restored with future calls to devops_build_cache_expand. '''
    safe_name = re.sub(r'\W+', '', name)
    cache_file = '/tmp/angel-build-cache-%s-%s-1.tgz' % (safe_name, checksum)
    if os.path.exists(cache_file):
        print >>sys.stderr, "Warning: cache file %s already exists; skipping." % cache_file
        devops_build_add_checksum_files(input_dir, checksum)
        return 0
    cache_file_tmp = "%s-tmp-create-%s.%s" % (cache_file, int(time.time()), os.getpid())
    print >>sys.stderr, 'Build cache: creating %s build cache at %s' % (name, cache_file)
    devops_build_add_checksum_files(input_dir, checksum)
    ret_val = os.system('tar -C "%s" -czf "%s" .' % (input_dir, cache_file_tmp))
    if ret_val:
        print >>sys.stderr, "Error: unable to create cache file '%s'." % cache_file_tmp
        return 1
    os.rename(cache_file_tmp, cache_file)
    return 0


def devops_build_cache_expand(name, checksum, output_dir, delete_existing_dir=False):
    ''' Given a build system (dojo, virtualenv, css) and a unique checksum,
        expand a cached version of the files to output_dir and return 0.
        Returns non-zero on failure, including non-existence of cache.

        If delete_existing_dir is false, any existing dir is moved aside and renamed.

        '''
    safe_name = re.sub(r'\W+', '', name)
    cache_file = '/tmp/angel-build-cache-%s-%s-1.tgz' % (safe_name, checksum)
    if not os.path.exists(cache_file):
        return 1
    if os.path.isdir(output_dir):
        if devops_build_is_checksum_okay(output_dir, checksum):
            return 0
        print >>sys.stderr, "Build cache: moving stale output dir '%s' aside." % output_dir
        try:
            if delete_existing_dir:
                shutil.rmtree(output_dir)  # Note that deleting a venv on a running system causes python to freak out
            else:
                os.rename(output_dir, "%s-tmp-stale-%s.%s" % (output_dir, int(time.time()), os.getpid()))
        except Exception as e:
            print >>sys.stderr, "Error removing stale output dir (%s)." % e # Possibly a file permission issue?
            return 1
    if output_dir[-1] == '/': output_dir = output_dir[:-1]
    output_dir_tmp = '%s-tmp-restore-%s.%s' % (output_dir, int(time.time()), os.getpid())
    os.makedirs(output_dir_tmp)
    print >>sys.stderr, "Restoring %s from cache %s" % (name, checksum)
    ret_val = os.system('tar -C "%s" -xzf "%s"' % (output_dir_tmp, cache_file))
    if ret_val:
        print >>sys.stderr, "Error: build cache failed to expand %s -> %s" % (cache_file, output_dir_tmp)
        return 1
    try:
        os.rename(output_dir_tmp, output_dir)
    except Exception as e:
        print >>sys.stderr, "Error: build cache failed to rename %s -> %s" % (output_dir_tmp, output_dir)
        return 1
    # Touch the cache file, so that we can track when cache files were last useful:
    try:
        os.utime(cache_file, None)
    except:
        pass
    return 0

    

def devops_build_is_checksum_okay(input_dir, checksum_value):
    return os.path.isfile(os.path.join(input_dir, ".build-checksum-%s" % checksum_value))


def devops_build_add_checksum_files(input_dir, checksum_value):
    checksum_cache_file = os.path.join(input_dir, ".build-checksum")
    try:
        open(checksum_cache_file, "wt").write(checksum_value)
    except:
        # Failures on the writes shouldn't every happen; but if it does, we'll ignore it; it'll just mean no caching.
        print >>sys.stderr, "Warning: failed to write build_hash file '%s'." % checksum_cache_file

    # Store a second cop whose name includes the hash -- this gives us a unique filename we can key off of elsewhere:
    checksum_cache_file_with_hash = os.path.join(input_dir, ".build-checksum-%s" % checksum_value)
    try:
        open(checksum_cache_file_with_hash, "wt").write(checksum_value)
    except:
        print >>sys.stderr, "Warning: failed to write build_hash file '%s'." % checksum_cache_file_with_hash


def devops_build_calculate_checksum(strings=(), files=(), build_dir=None, use_stat_instead_of_md5=False):
    ''' Given a list of strings and a list of files, return an md5 checksum value that
        changes if any string or file content changes.
        Files can contain dirs.
        If build_dir is provided, then we'll cut some corners and check
        mtimes on all files as compared to a checksum cache file.
        If use_stat_instead_of_md5 is true, then we'll use file size and file time info instead of cacluating an MD5 sum.
        This can be faster, but potentially miss changes.
    '''
    build_hash = None
    cached_build_hash = None
    checksum_cache_file = None
    if build_dir:
        checksum_cache_file = os.path.join(build_dir, ".build-checksum")

    # Check if our cache file allows us to skip generating an checksum:
    if checksum_cache_file and os.path.isfile(checksum_cache_file):
        # As optimization to the below checksum logic, we look at file modification times to skip
        # generating the checksum when we can. We check if there are any files that are newer than the cache file.
        cache_file_is_newest_file = True
        checksum_cache_file_mtime = get_latest_mtime(checksum_cache_file)
        for f in files:
            if get_latest_mtime(f) > checksum_cache_file_mtime:
                cache_file_is_newest_file = False
        if cache_file_is_newest_file:
            cached_build_hash = open(checksum_cache_file).readline()[0:12] # slice to get rid of trailing newline
            checksum_cache_file_with_hash = "%s-%s" % (checksum_cache_file, cached_build_hash)
            if os.path.isfile(checksum_cache_file_with_hash):
                # Then our checksum cache is newer than any existing file, and it's not stale, fast return:
                return cached_build_hash
            
    # Generate checksum:
    checksum_string = str(strings)
    for f in sorted(files):
        if use_stat_instead_of_md5:
            h = angel.util.checksum.get_md5_of_path_filestats(f)
        else:
            h = angel.util.checksum.get_md5_of_path_contents(f)
        if h is None:
            print >>sys.stderr, "Error: devops_build_get_checksum can't generate checksum for path '%s'." % f
            return None
        checksum_string += h
    build_hash = hashlib.md5(checksum_string).hexdigest()

    # We're extra clever here: if we have an additional file that's checksum_cache_file + "-" + hash, touch ourselves
    # so that future mtime checks can be used (speeds up check from ~0.7 seconds to ~0.03 seconds, so we can run this on every user-issued command).
    # This will lead to problems if spurious hash files exist, so make sure "make clean" removes them.
    checksum_cache_file_with_hash = "%s-%s" % (checksum_cache_file, build_hash)
    if os.path.isfile(checksum_cache_file_with_hash):
        try:
            os.utime(checksum_cache_file, None)
            os.utime(checksum_cache_file_with_hash, None)
        except:
            print >>sys.stderr, "Warning: unable to update utime on checksum file; check file ownership?" # Failing to update isn't a fatal error; just odd.

    # Finally, return the hash, regardless of source:
    return build_hash[0:12]


def get_latest_mtime(path):
    if os.path.isfile(path):
        return os.stat(path).st_mtime
    if os.path.isdir(path):
        newest_mtime = 0
        for entry in os.listdir(path):
            m = get_latest_mtime('%s/%s' % (path, entry))
            if m > newest_mtime:
                newest_mtime = m
        return newest_mtime
    print >>sys.stderr, "Error: get_latest_mtime given invalid path '%s'." % path
    return None
