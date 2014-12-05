import os
import shutil
import stat
import sys
import time

import angel.exceptions
from angel.util.checksum import get_md5_of_path_contents



# import angel.util.dedup_files
# import time
# start_time = time.time(); file_checksums = angel.util.dedup_files.dedup_calculate_checksums('.'); end_time = time.time(); print end_time - start_time;
# start_time = time.time(); angel.util.dedup_files.dedup_create_copy('~/test-src/', '~/test-dest/v1', '~/test-linkdir', file_checksums=file_checksums); end_time = time.time(); print end_time - start_time;

# angel.util.dedup_files.dedup_create_copy('~/test-src/', '~/test-dest/v1', '~/test-linkdir', file_checksums=file_checksums)

def dedup_calculate_checksums(src_path):
    ''' Given a path, return a dictionary of file->checksums for those files that can be dedupped.
        Unsupported files (e.g. symlinks) are not included in the checksum map. '''
    src_path = os.path.abspath(os.path.expanduser(src_path))
    src_path_len = len(src_path)
    checksums = {}
    for (path, dirs, files) in os.walk(src_path):
        for file in files:
            file_srcpath = os.path.join(path, file)
            file_relpath = file_srcpath[(1+src_path_len):]
            file_stat = os.lstat(file_srcpath)
            if file_stat.st_mode & stat.S_ISUID:
                # We shouldn't ever have any setuid bits; explictily check for them and skip files that have it set.
                print >>sys.stderr, "Warning: setuid permissions not supported (%s: %s)" % (file_srcpath, file_stat.st_mode)
                continue
            if stat.S_ISLNK(file_stat.st_mode):
                continue  # Silently skip symlinks -- we see them in python virtual envs
            if not stat.S_ISREG(file_stat.st_mode):
                print >>sys.stderr, "Warning: unknown file type at %s; skipping file." % file_srcpath
                continue
            checksum_filename = dedup_get_checksum_based_name(get_md5_of_path_contents(file_srcpath), file_stat.st_size, file_stat.st_mode)
            if checksum_filename is None:
                print >>sys.stderr, "Warning: unable to get checksum name for %s; skipping file." % file_srcpath
                continue
            checksums[file_relpath] = checksum_filename
        for dir in dirs:
            dir_srcpath = os.path.join(path, dir)
            dir_relpath = dir_srcpath[(1+src_path_len):]
            dir_stat = os.lstat(dir_srcpath)
            checksums[dir_relpath] = dedup_get_checksum_based_name(0,0,dir_stat.st_mode)
    return checksums


def dedup_get_info_from_checksum(file_checksum):
    ''' Given a checksum, return some info about the file (see dedup_get_checksum_based_name). '''
    try:
        (file_checksum, file_size, file_mode) = file_checksum.split('.')
        return {'checksum': file_checksum, 'size': int(file_size), 'mode': int(file_mode)}
    except Exception as e:
        print >>sys.stderr, "Error: unable to parse checksum %s." % file_checksum
        return None


def dedup_get_checksum_based_name(file_checksum, file_size, file_mode):
    ''' Return a checksum string for files. We use file_size as a minor extra check; we need file_mode because hard links can only have one permission set across all instances.
        Use '0' for a file_checksum of a directory.
    '''
    if file_checksum is None:
        return None
    return '%s.%s.%s' % (file_checksum, file_size, file_mode)


def dedup_load_checksum_file(checksum_file):
    ''' Given a file that has one checksum entry per line, "<checksum><space><path>", return a dict of path to checksum.
        Note that path may contain spaces! '''
    if not os.path.isfile(checksum_file):
        return None
    checksums = {}
    try:
        for line in open(checksum_file, 'r').read().split('\n'):
            if not len(line): continue
            (checksum, file) = line.split(' ', 1)
            checksums[file] = checksum
    except Exception as e:
        print >>sys.stderr, "Error parsing %s: %s" % (checksum_file, e)
        return None
    return checksums


def dedup_get_unknown_checksums_in_manifest(file_checksums, hardlink_checksum_dir):
    ''' Given a checksum dictionary (path to checksum), return a list of all checksum files that don't exist in hardlink_checksum_dir. '''
    # The eventual intent here is that, given a checksum manifest for a build, we can generate a list of the files
    # that we don't know about, pull them down from a central repo, and then trigger a version install with dedup_create_copy_from_manifest.
    # This would greatly speed things up and would also mean that "version diffs" wouldn't require any sort of sequential roll-out;
    # just a "here's what's missing to create the requested version."
    if not os.path.isdir(hardlink_checksum_dir):
        print >>sys.stderr, "Warning: no directory found at %s." % hardlink_checksum_dir
        return set(file_checksums.keys())
    return set(file_checksums.keys()) - set(os.listdir(hardlink_checksum_dir))


def dedup_create_copy_from_manifest(file_checksums, dest_path, hardlink_checksum_dir, sleep_ratio=0):
    ''' Given a checksum dictionary, generate a directory at dest_path with hardlinks to the checksummed files found in hardlink_checksum_dir.
        hardlink_checksum_dir MUST contain all files listed in file_checksums before this is called. '''
    if file_checksums is None or len(file_checksums) == 0:
        print >>sys.stderr, "Error: invalid file checksum list."
        return 1
    if os.path.exists(dest_path):
        print >>sys.stderr, "Error: dest path '%s' already exists."
        return 2
    dest_path_tmp = os.path.join(os.path.dirname(dest_path), ".dedup_creating_%s" % os.path.basename(dest_path))
    if os.path.exists(dest_path_tmp):
        print >>sys.stderr, "Error: tmp dest path '%s' exists." % dest_path_tmp
        return 3
    try:
        os.makedirs(dest_path_tmp)
    except Exception as e:
        print >>sys.stderr, "Error: unable to make tmp dir %s: %s" % (dest_path_tmp, e)
        return 4
    try:
        count = 0
        start_time = time.time()
        seconds_slept = 0
        for path in sorted(file_checksums):
            count += 1
            if count % 400 == 0:
                seconds_slept += _dedup_microsleep(start_time, seconds_slept, sleep_ratio)
            full_path = os.path.join(dest_path_tmp, path)
            path_info = dedup_get_info_from_checksum(file_checksums[path])
            if path_info['mode'] & stat.S_ISUID:
                print >>sys.stderr, "Error: setuid bit set for path %s." % path
                return 5
            if stat.S_ISDIR(path_info['mode']):
                os.mkdir(full_path, stat.S_IMODE(path_info['mode']))
            elif stat.S_ISREG(path_info['mode']):
                hardlink_src = os.path.join(hardlink_checksum_dir, file_checksums[path])
                try:
                    os.link(hardlink_src, full_path)
                except Exception as e:
                    print >>sys.stderr, "Error: unable to create link %s -> %s: %s" % (hardlink_src, full_path, e)
                    return 6
            else:
                print >>sys.stderr, "Error: unknown file type (%s: %s)" % (path, path_info['mode'])
                return 7

        # Only move the new dir in place after everything has been created:
        try:
            os.rename(dest_path_tmp, dest_path)
        except Exception as e:
            print >>sys.stderr, "Error: unable to move %s -> %s: %s" % (dest_path_tmp, dest_path, e)
            return 8

    except KeyboardInterrupt:
        print >>sys.stderr, "Error: interrupt received; removing new version."

    finally:
        if os.path.isdir(dest_path_tmp):
            shutil.rmtree(dest_path_tmp)

    return 0


def dedup_create_copy(src_path, dest_path, hardlink_checksum_dir, file_checksums=None, sleep_ratio=0):
    ''' Given a src path, create a versioned copy of it under dest_path; throws exception on any error
        The directory at hardlink_checksum_dir is used to create hardlinks for the copies; it must be on the same partition as dest_path.

        If file_checksums is given (as a {}), checksums for files found in the hash will be used instead of calculating them.
        Checksums for files are a string based on a checksum of file contents and file metadata.
        Files in the list must NOT start with "./" -- e.g. "foo.txt" -> 4d622415ef92bd8d53ac23688f61d873, "bar/foo.txt" -> 525....
        (Using this hash will speed up deploys where the checksums can be calculated in advance, e.g. on a build server.)

        '''

    if sleep_ratio > 0.999:
        print >>sys.stderr, "Warning: sleep ratio '%s' too large; using 0.99" % sleep_ratio
        sleep_ratio = 0.99
    if sleep_ratio < 0:
        sleep_ratio = 0

    file_checksums = file_checksums or {}
    src_path = os.path.abspath(os.path.expanduser(src_path))
    dest_path_final = os.path.abspath(os.path.expanduser(dest_path))
    dest_path_tmp = os.path.join(os.path.dirname(dest_path_final), ".dedup_creating_%s" % os.path.basename(dest_path_final))
    hardlink_checksum_dir = os.path.abspath(os.path.expanduser(hardlink_checksum_dir))

    if not os.path.isdir(src_path):
        raise angel.exceptions.AngelVersionException("Unable to create copy (missing source path '%s')" % src_path)

    if src_path.startswith(dest_path_final) or dest_path_final.startswith(src_path):
        raise angel.exceptions.AngelVersionException("src and dest paths must not be nested.")

    if os.path.exists(dest_path_final):
        raise angel.exceptions.AngelVersionException("Unable to create copy (version path '%s' already exists)" % dest_path_final)

    if os.path.exists(dest_path_tmp):
        raise angel.exceptions.AngelVersionException("tmp dest path '%s' already exists." % dest_path_tmp)

    try:
        if not os.path.isdir(os.path.dirname(hardlink_checksum_dir)):
            os.makedirs(os.path.dirname(hardlink_checksum_dir))  # Make parent dirs with default umask
        if not os.path.isdir(hardlink_checksum_dir):
            os.makedirs(hardlink_checksum_dir, mode=0700)
            # Touch a file that we verify exists in remove_unused_links, as a safety check:
            open(os.path.join(hardlink_checksum_dir, ".dedup_safety_check"), "w").write(str(time.time()))
            # Create a hardlink to the safety file, so that the link count is >1, just to avoid potentially manually clearing it for nlink=1 checks:
            os.link(os.path.join(hardlink_checksum_dir, ".dedup_safety_check"), os.path.join(hardlink_checksum_dir, ".dedup_safety_check-2"))
    except Exception as e:
        raise angel.exceptions.AngelVersionException("can't make hardlink_checksum_dir %s: %s" %
                                                     (hardlink_checksum_dir, e))

    start_time = time.time()
    seconds_slept = 0
    files_missing_checksums = ()

    try:
        try:
            os.makedirs(dest_path_tmp)
        except Exception as e:
            raise angel.exceptions.AngelVersionException("unable to create destination dir '%s': %s" %
                                                         (dest_path_tmp, e))

        for (path, dirs, files) in os.walk(src_path):
            for dir in dirs:
                dir_srcpath = os.path.join(path, dir)
                dir_relpath = dir_srcpath[(1+len(src_path)):]
                dir_destpath = os.path.join(dest_path_tmp, dir_relpath)
                if os.path.islink(dir_srcpath):
                    # If it's a dir symlink to another point inside the tree, do a relative path link, otherwise abspath link.
                    # Debian package policy is that links are relative -- even if outside the install tree -- if they point at
                    # files under the same root dir. E.g.: /usr/foo -> /usr/bar will result in a relative link between foo and bar.
                    # While we "should" be okay with this, as we currently create versioned copies at the same dir level as the
                    # src copy, there's really no reason to tempt fate and no abspath these links at version creation time.
                    link_dest = os.readlink(dir_srcpath)
                    link_dest_abspath = os.path.normpath(os.path.join(os.path.dirname(dir_srcpath),link_dest))  # need dirname() to get the dir that the symlink is in
                    if not link_dest_abspath.startswith(src_path):
                        link_dest = link_dest_abspath
                    os.symlink(link_dest, dir_destpath)
                elif os.path.isdir(dir_srcpath):  # note that symlinks to dirs returns True for this as well!
                    os.mkdir(dir_destpath)
                    shutil.copystat(dir_srcpath, dir_destpath)
                else:
                    raise angel.exceptions.AngelVersionException("unknown dir type at path '%s'." % dir_srcpath)
            for file in files:
                file_srcpath = os.path.join(path, file)
                file_stat = os.lstat(file_srcpath)
                file_relpath = file_srcpath[(1+len(src_path)):]
                file_destpath = os.path.join(dest_path_tmp, file_relpath)
                if stat.S_ISLNK(file_stat.st_mode):
                    # See note above in dirs section about absolute vs relative link paths.
                    link_dest = os.readlink(file_srcpath)
                    link_dest_abspath = os.path.normpath(os.path.join(os.path.dirname(file_srcpath),link_dest))
                    if not link_dest_abspath.startswith(src_path):
                        link_dest = link_dest_abspath
                    os.symlink(link_dest, file_destpath)
                    continue
                if not stat.S_ISREG(file_stat.st_mode): 
                    print >>sys.stderr, "Unsupported file type at %s" % file_srcpath
                    return 7
                if file_relpath in file_checksums:
                    checksum_filename = file_checksums[file_relpath]
                else:
                    checksum_filename = dedup_get_checksum_based_name(get_md5_of_path_contents(file_srcpath), file_stat.st_size, file_stat.st_mode)
                    if len(file_checksums) and file_relpath != ".angel/file_checksums":
                        # Warn about files that exist that don't appear in the checksums file (except for the checksum file itself):
                        files_missing_checksums += (file_relpath,)
                if checksum_filename is None:
                    print >>sys.stderr, "Error: unable to find checksum_filename for %s; bailing." % file_srcpath
                    return 8
                hardlink_master_path = os.path.join(hardlink_checksum_dir, checksum_filename)
                if not os.path.exists(hardlink_master_path):
                    shutil.copy2(file_srcpath, hardlink_master_path)
                os.link(hardlink_master_path, file_destpath)

            # After each dir, potentially sleep -- we support this so large copies can be time-sliced out, to reduce i/o pressure in prod systems:
            seconds_slept += _dedup_microsleep(start_time, seconds_slept, sleep_ratio)

        os.rename(dest_path_tmp, dest_path_final)

    except Exception as e:
        raise angel.exceptions.AngelVersionException("failed to create copy: %s" % e)

    except KeyboardInterrupt:
        raise angel.exceptions.AngelVersionException("interrupt received")

    finally:
        if os.path.isdir(dest_path_tmp):
            shutil.rmtree(dest_path_tmp)

    if len(files_missing_checksums) > 5:
        print >>sys.stderr, "Warning: %s files missing checksums" % len(files_missing_checksums)


def dedup_files(path, sleep_ratio=0, verbose=True):
    ''' Hard link all identical files under a given path.
        Assumes that path does not contain more than one mountpoint!
        If sleep_ratio is >0, we'll insert sleeps in our loops, so as to throttle dedupping to a moderate amount for background processing where desired.
     '''

    path = os.path.abspath(os.path.expanduser(path))

    if not os.path.isdir(path):
        print >>sys.stderr, "Invalid path '%s' to file de-dup." % path
        return -1

    if sleep_ratio > 0.999:
        print >>sys.stderr, "Warning: sleep ratio '%s' too large; using 0.99" % sleep_ratio
        sleep_ratio = 0.99
    if sleep_ratio < 0:
        sleep_ratio = 0

    seen_inodes = {}
    checksum_to_path = {}

    start_time = time.time()
    seconds_slept = 0

    ret_val = 0
    
    dedup_files_preserved_stats = {}
    dedup_files_released_stats = {}
    dedup_files_count = 0
    
    for (path, dirs, files) in os.walk(path):
        for file in files:
            first_file_path = os.path.join(path, file)
            try:
                first_file_stat = os.lstat(first_file_path)
            except OSError as e:
                print >>sys.stderr, "Warning: can't stat %s (%s); skipping." % (first_file_path, e)
                continue
            if not stat.S_ISREG(first_file_stat.st_mode):
                continue
            if first_file_stat.st_ino in seen_inodes:
                continue
            
            first_file_checksum = None
            try:
                first_file_checksum = dedup_get_checksum_based_name(get_md5_of_path_contents(first_file_path), first_file_stat.st_size, first_file_stat.st_mode)
            except Exception as e:
                print >>sys.stderr, "Error: checksum of %s failed" % first_file_path
            if first_file_checksum is None:
                ret_val = -2
                break
                
            dedup_files_preserved_stats[first_file_stat.st_ino] = first_file_stat.st_size
            if first_file_checksum not in checksum_to_path:
                checksum_to_path[first_file_checksum] = first_file_path
                seen_inodes[first_file_stat.st_ino] = True
                continue
            
            # We now have two files with the same checksum but different inodes; we need to hardlink them together.
            second_file_path = checksum_to_path[first_file_checksum]  # This returns the path to a version seen in a previous iteration of our loop
            try:
                second_file_stat = os.lstat(second_file_path)
            except OSError:
                # It's possible that the second file was removed since we've seen it -- i.e. a version being deleted.
                # If this happens, reset the checksum marker to use first_file and continue:
                checksum_to_path[first_file_checksum] = first_file_path
                seen_inodes[first_file_stat.st_ino] = True
                continue

            # It's possible that file paths A, B, and C all have the same data but where A is one instance and B and C are already hardlinked.
            # To avoid "spinning" through files (Linking A to B; then B to C while leaving A at old value),
            # we take the inode that has the higher link count and use it.
            # If the link count is identitcal (usually 1 for two new files), we take the lower inode number, although this shouldn't matter. 
            # It's still possible to hit a suboptimal case (e.g. if one has A, B, CDE), but that should be rare in practice 
            # and won't cause any actual issue other than an extra copy on disk; and subsequent runs would dedup it.
            if (second_file_stat.st_nlink > first_file_stat.st_nlink) or (second_file_stat.st_nlink == first_file_stat.st_nlink and second_file_stat.st_ino < first_file_stat.st_ino):
                first_file_path_2 = second_file_path
                first_file_stat_2 = second_file_stat
                second_file_path = first_file_path
                second_file_stat = first_file_stat
                first_file_path = first_file_path_2
                first_file_stat = first_file_stat_2
                del first_file_path_2, first_file_stat_2
            
            # Create a hard link to first_file_path and then atomically move it on top of path to second_file_path to release the second instance:
            tmp_file_path = '%s-%s' % (second_file_path, time.time())
            try:
                os.link(first_file_path, tmp_file_path)
                os.rename(tmp_file_path, second_file_path)
                dedup_files_count += 1
                seen_inodes[first_file_stat.st_ino] = True  # It's possible the "new" file's inode is the winner; e.g. replacing a previously-seen copy
                if second_file_stat.st_ino in seen_inodes:
                    # Need to delete out the loosing inode instance, so that any upcoming instances that were hardlinked to it get cleaned up:
                    del seen_inodes[second_file_stat.st_ino]
                if verbose:
                    if 0 == dedup_files_count % 400:
                        sys.stdout.write('.')
                        sys.stdout.flush()
                dedup_files_released_stats[second_file_stat.st_ino] = second_file_stat.st_size
                if second_file_stat.st_ino in dedup_files_preserved_stats:
                    del dedup_files_preserved_stats[second_file_stat.st_ino]  # Can happen if >2 hardlinkes to a file
            except Exception as e:
                print >>sys.stderr, "Error: unable to relink files in dedup step (%s->%s: %s)" % (second_file_path, first_file_path, e)
                ret_val = -3
                break
        
        # At the end of every dir that we've traversed, check if we need to micro-sleep:
        try:
            seconds_slept += _dedup_microsleep(start_time, seconds_slept, sleep_ratio)
        except:
            print >>sys.stderr, "Interrupted during dedup sleep; bailing."
            ret_val = -4
            break

    files_seen = len(checksum_to_path)
    inodes_seen = len(dedup_files_preserved_stats) + len(dedup_files_released_stats)
    files_deleted = len(dedup_files_released_stats)
    disk_usage_before = sum(dedup_files_preserved_stats.values()) + sum(dedup_files_released_stats.values())
    disk_usage_after = sum(dedup_files_preserved_stats.values())
    disk_usage_released = sum(dedup_files_released_stats.values())
    
    if verbose:
        print >>sys.stderr, "Unique files seen (checksum method): %s" % (files_seen)
        print >>sys.stderr, "Total files seen (by inode count): %s" % (inodes_seen)
        print >>sys.stderr, "Disk usage, before: %s bytes" % (disk_usage_before)
        if disk_usage_before > 0:
            print >>sys.stderr, "Disk usage, after: %s bytes (%.2f%% change)" % (disk_usage_after, 100 - 100*(float(disk_usage_after)/float(disk_usage_before)))
        else:
            print >>sys.stderr, "Disk usage, after: 0"
        print >>sys.stderr, "Files dedupped: %s (%s bytes released)" % (files_deleted, disk_usage_released)
        
    return ret_val


def remove_unused_links(hardlink_checksum_dir):
    """Run through hardlinks dir and remove any file that has a link count of exactly one."""

    # This is rather dangerous if run with a bad input path, so we create a safety check file when we
    # first create the hardlink dir, and verify that that file exists when removing files.
    if not os.path.isfile(os.path.join(hardlink_checksum_dir, ".dedup_safety_check")):
        raise angel.exceptions.AngelVersionException("Invalid hardlink_checksum_dir (missing safety check)")
    for f in os.listdir(hardlink_checksum_dir):
        if f == ".dedup_safety_check":
            continue
        p = os.path.join(hardlink_checksum_dir, f)
        if os.stat(p).st_nlink == 1:
            os.remove(p)


def _dedup_microsleep(start_time, seconds_slept, sleep_ratio):
    ''' Sleep for a short amount of time, based on sleep_ratio and start time; return amount of time slept. '''
    run_time = time.time() - start_time
    if seconds_slept > run_time * sleep_ratio:
        return 0
    seconds_to_sleep = run_time * sleep_ratio - seconds_slept
    if seconds_to_sleep > 2:
        seconds_to_sleep = 2
    if seconds_to_sleep > 0.05:
        time.sleep(seconds_to_sleep)
        return seconds_to_sleep
    return 0

