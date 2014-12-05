
import angel.exceptions
import grp
import os
import pwd
import stat
import sys


def is_path_in_use(path_to_check, do_false_negative_check=True):
    ''' Given a path, return True if files underneath that path are in active use or if it can't be determined.'''
    if path_to_check[-1] == '/':
        path_to_check = path_to_check[:-1]  # Trim trailing slash if given
    if not os.path.exists(path_to_check):
        print >>sys.stderr, "Error: is_path_in_use(): path doesn't exist at %s" % path_to_check
        return True
    if 0 != os.getuid():
        print >>sys.stderr, "Warning: is_path_in_use must be run as root; assuming path is in use."
        return True
    if not os.path.isdir('/proc'):
        print >>sys.stderr, "Warning: is_path_in_use requires /proc; assuming path is in use."
        return True
    for pid in os.listdir('/proc'):
        fd_dir = '/proc/%s/fd' % pid
        try:
            if not os.path.isdir(fd_dir):
                # /proc contains non-pid files; instead of globbing to exclude those we skip over ones that lack fd dir
                continue
            for f in os.listdir(fd_dir):
                f_path = os.path.realpath(os.path.join(fd_dir, f))
                if f_path == path_to_check or os.path.realpath(f_path).startswith('%s/' % path_to_check):
                    # The extra '/' at the end above is to avoid /foo/10 false-positive matching path /foo/100
                    return True
            map_file = '/proc/%s/maps' % pid
            if not os.path.isfile(map_file):
                if os.path.isdir('/proc/%s' % pid):  # Our proc could've exited between listdir time and now?
                    print >>sys.stderr, "Warning: is_path_in_use can't find map file %s?" % map_file
                continue
            for map in open(map_file, 'r').read().split('\n'):
                if path_to_check in map:
                    return True
        except (OSError, IOError):
            # A process can exut after we called os.listdir(fd_dir) and before we called os.path.realpath() or open
            pass

    # If we get here, then no files are open under path.
    # As a sanity check, open a file under path and make sure that we see path flip to being open:
    if do_false_negative_check:
        # It's possible that the version was *just* deleted (this has been observed):
        if not os.path.exists(path_to_check):
            return False
        try:
            fh = os.open(path_to_check, os.O_RDONLY)
            if not is_path_in_use(path_to_check, do_false_negative_check=False):
                print >>sys.stderr, "Error: false negative check for path %s failed! This should never happen." %\
                                    path_to_check
                return True
            os.close(fh)
        except Exception as e:
            print >>sys.stderr, "Error: false negative check for path %s failed: %s" % (path_to_check, e)
            return True
    return False


def create_dirs_if_needed(absolute_path,
                          owner_user=None,
                          owner_group=None,
                          mode=0755,
                          recursive_fix_owner=False,
                          ignore_ownership_errors=False):
    '''Given a directory path, make sure that it exists and that the dir has the correct ownership.
    Parent directories, when missing, are created and set to be owned by owner_user/group as well.
    If recursive_fix_owner is True, check that all its contents are owned by the given user
    with the top level dir having the given read/write permissions.'''

    if absolute_path.startswith('~'):
        absolute_path = os.path.expanduser(absolute_path)

    def _mkdir(path, owner_user, owner_group, mode):
        if not os.path.exists(path):
            try:
                parent_dir = os.path.dirname(path)
                if not os.path.exists(parent_dir):
                    _mkdir(parent_dir, owner_user, owner_group, mode)
                os.mkdir(path, mode)
                set_file_owner(path, owner_user=owner_user, owner_group=owner_group)
            except Exception as e:
                raise angel.exceptions.AngelUnexpectedException("Can't create directory '%s' (%s)" % (path, e))

    _mkdir(absolute_path, owner_user, owner_group, mode)
    set_file_owner(absolute_path, owner_user, owner_group,
                   recursive=recursive_fix_owner,
                   ignore_ownership_errors=ignore_ownership_errors)

    try:
        # The dir might have existed before but with old permissions, so reset it:
        if stat.S_IMODE(os.stat(absolute_path).st_mode) != mode:
            os.chmod(absolute_path, mode)
    except Exception as e:
        raise angel.exceptions.AngelUnexpectedException("can't update permission mode on %s to %s (%s)." %
                                                        (absolute_path, oct(mode), e))


def set_file_owner(absolute_path, owner_user=None, owner_group=None, recursive=False, ignore_ownership_errors=False):
    '''Set the file/dir at the given path to the given user/group.'''
    # ignore_ownership_errors: We might need to ignore errors in one case, which is because
    # lock files for some server processes get created as root and
    # if status is called (non-root run), then the permission set will fail.
    if owner_user is None and owner_group is None:
        return
    current_euid = os.getuid()
    if 0 != current_euid and owner_group is None:
        return  # If non-root, we could still have a required chgrp call on OS X, so only return if owner_group is None
    try:
        absolute_path_stat = os.stat(absolute_path)
        if owner_user is not None:
            target_uid = pwd.getpwnam(owner_user).pw_uid
            if absolute_path_stat.st_uid != target_uid:
                if current_euid != 0 and target_uid != current_euid:  # Non-root, and not the target owner; can't fix.
                    if ignore_ownership_errors:
                        return
                    raise angel.exceptions.AngelUnexpectedException("Can't set owner for %s (not running as root)" %
                                                                    (absolute_path))
                if 0 == current_euid:
                    try:
                        os.chown(absolute_path, target_uid, -1)
                    except Exception as e:
                        raise angel.exceptions.AngelUnexpectedException("Can't set owner for %s (%s)." %
                                                                        (absolute_path, e))
                else:
                    if not ignore_ownership_errors:
                        raise angel.exceptions.AngelUnexpectedException("Can't set owner for %s (user should be %s). Re-run your command as root?" % (absolute_path, owner_user))

        if owner_group is not None:
            target_gid = grp.getgrnam(owner_group).gr_gid
            if absolute_path_stat.st_gid != target_gid:
                try:  # Skip uid==0 check; a non-root user might have permission to chgrp something to a different group
                    os.chown(absolute_path, -1, target_gid)
                except:
                    pass
                absolute_path_stat = os.stat(absolute_path)
                if absolute_path_stat.st_gid != target_gid:
                    if not ignore_ownership_errors:
                        raise angel.exceptions.AngelUnexpectedException("Can't set owner for %s (group should be %s). Re-run your command as root?" % (absolute_path, owner_group))

    except:
        if ignore_ownership_errors:
            return
        raise angel.exceptions.AngelUnexpectedException("Can't find user/group %s/%s as needed for setting permissions on %s." % (owner_user, owner_group, absolute_path))

    if recursive and os.path.isdir(absolute_path):
        try:
            for file in os.listdir(absolute_path):
                set_file_owner('%s/%s' % (absolute_path,file),
                               owner_user=owner_user,
                               owner_group=owner_group,
                               recursive=recursive,
                               ignore_ownership_errors=ignore_ownership_errors)
        except Exception as e:
             if not ignore_ownership_errors:
                 raise angel.exceptions.AngelUnexpectedException("Can't update %s (%s). Either run your command as root, change the config path, or change RUN_AS_USER/RUN_AS_GROUP to your current user." % (absolute_path, e))
