import os
import grp
import pwd
import stat
import sys

from angel.locks import get_lock_filename
from angel.util.pidfile import *





def is_lock_available(config, lockname):
    if None == who_has_lock(config, lockname):
        return True
    return False


def who_has_lock(config, lockname):
    ''' Return the pid owner for a given lock, or None if no pid owns it. '''
    lock_filename = get_lock_filename(config, lockname)
    if lock_filename is None:
        return None
    if not os.path.exists(lock_filename):
        return None
    pid_in_lockfile = get_pid_from_pidfile(lock_filename)
    if pid_in_lockfile is None:
        print >>sys.stderr, 'No pid in lockfile; removing %s.' % lock_filename
        os.remove(lock_filename)
        return None
    if not is_pid_running(pid_in_lockfile):
        print >>sys.stderr, 'Removing stale lockfile %s' % lock_filename
        os.remove(lock_filename)
        return None
    return pid_in_lockfile
    

def get_lock(config, lockname, lock_timeout=15, print_errors=True, waiting_message=None):
    lock_filename = get_lock_filename(config, lockname)
    if lock_filename is None:
        return -1
    other_pid = None
    waiting_message_has_been_shown = False
    if not is_lock_available(config, lockname):
        other_pid = get_pid_from_pidfile(lock_filename)
        if print_errors and lock_timeout > 0: print >>sys.stderr, 'Waiting on process %s for lock %s (%s second timeout)' % (other_pid, lock_filename, lock_timeout)
        try:
            while (not is_lock_available(config, lockname)) and lock_timeout >= 0:
                time.sleep(0.5)
                lock_timeout -= 0.5
                if not waiting_message_has_been_shown and waiting_message is not None:
                    waiting_message_has_been_shown = True
                    print >>sys.stderr, waiting_message
        except:
             if print_errors: print >>sys.stderr, "Error: exception when getting lock %s from process %s" % (lock_filename, other_pid)
             return -1

    if not is_lock_available(config, lockname):
        if print_errors: print >>sys.stderr, 'Error: unable to get lock %s from process %s' % (lock_filename, other_pid)
        return -1

    return write_pidfile(lock_filename, os.getpid())


def release_lock(config, lockname):
    ''' Return 0 if the next call to get_lock will work; non-zero otherwise. '''
    lock_filename = get_lock_filename(config, lockname)
    if lock_filename is None:
        return -1
    if os.path.exists(lock_filename):
        locker_pid = get_pid_from_pidfile(lock_filename)
        if locker_pid == os.getpid():
            #print >>sys.stderr, "Releasing lock %s (pid %s/%s)" % (lock_filename, locker_pid, os.getpid())
            os.remove(lock_filename)
            return 0
        else:
            if is_pid_running(locker_pid):
                print >> sys.stderr, "Warning: went to release lock %s, but lockfile %s is owned by different process (%s). Did a forked child fail to exit somewhere?" % (lockname, lock_filename, locker_pid)
                return -1
            else:
                print >> sys.stderr, "Warning: went to release lock %s, but lockfile %s is owned by different process (%s). That process is dead, so removing lock anyway." % (lockname, lock_filename, locker_pid)
                os.remove(lock_filename)
                return 0

    print >> sys.stderr, 'Warning: went to release lock %s, but lockfile %s already missing. THIS SHOULD NEVER HAPPEN; WAS THE LOCKDIR DELETED DURING OUR RUN?' % (lockname, lock_filename)
    return 0



def set_file_owner(absolute_path, owner_user=None, owner_group=None, recursive=False, ignore_ownership_errors=False):
    ''' Set the file at the given path to the give user/group. Return 0 on success; non-zero otherwise (unless ignore_ownership_errors is true). '''
    # ignore_ownership_errors: We might need to ignore errors in one case: lock files for some server processes get created as root and 
    # if status is called (non-root run), then the permission set will fail.
    if owner_user is None and owner_group is None:
        return 0
    current_euid = os.getuid()
    if 0 != current_euid and owner_group is None:
        return 0  # If non-root, we could still have a required chgrp call on OS X...
    try:
        absolute_path_stat = os.stat(absolute_path)
        if owner_user is not None:
            target_uid = pwd.getpwnam(owner_user).pw_uid
            if absolute_path_stat.st_uid != target_uid:
                if current_euid != 0 and target_uid != current_euid:  # Non-root, and not the target owner; can't fix.
                    if ignore_ownership_errors: return 0
                    print >>sys.stderr, "Error: can't set owner for %s (not running as root). Re-run your command as root?" % (absolute_path)
                    return 1
                if 0 == current_euid:
                    try:
                        os.chown(absolute_path, target_uid, -1)
                    except Exception as e:
                        print >>sys.stderr, "Error: can't set owner for %s (%s)." % (absolute_path, e)
                        return 1
                else:
                    if not ignore_ownership_errors:
                        print >>sys.stderr, "Error: can't set owner for %s (user should be %s). Re-run your command as root?" % (absolute_path, owner_user)
                        return 1

        if owner_group is not None:
            target_gid = grp.getgrnam(owner_group).gr_gid
            if absolute_path_stat.st_gid != target_gid:
                try:  # Skip uid==0 check -- a non-root user might have permission to chgrp something to a different group that they are a member of.
                    os.chown(absolute_path, -1, target_gid)
                except:
                    pass
                absolute_path_stat = os.stat(absolute_path)
                if absolute_path_stat.st_gid != target_gid:
                    if not ignore_ownership_errors:
                        print >>sys.stderr, "Error: can't set owner for %s (group should be %s). Re-run your command as root?" % (absolute_path, owner_group)
                        return 1

    except:
        if ignore_ownership_errors: return 0
        print >>sys.stderr, "Error: can't find user/group %s/%s as needed for setting permissions on %s." % (owner_user, owner_group, absolute_path)
        return 1

    if recursive and os.path.isdir(absolute_path):
        try:
            for file in os.listdir(absolute_path):
                if 0 != set_file_owner('%s/%s' % (absolute_path,file), owner_user=owner_user, owner_group=owner_group, recursive=recursive, ignore_ownership_errors=ignore_ownership_errors):
                    return 1
        except:
             if not ignore_ownership_errors:
                 print >>sys.stderr, "Error: can't read %s. Either run your command as root, change the config path, or change RUN_AS_USER/RUN_AS_GROUP to your current user." % absolute_path
                 return 1

    return 0


def create_dirs_if_needed(absolute_path, name="service", owner_user=None, owner_group=None, mode=0755, recursive_fix_owner=False, ignore_ownership_errors=False):
    ''' Given a directory path, make sure that it exists and that the dir has the correct ownership.
        If recursive_fix_owner is True, check that all its contents are owned by the given user with the top level dir having the given read/write permissions.
        Returns 0 on success, non-zero otherwise. '''

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
            except:
                print >>sys.stderr, "Error: can't create %s directory '%s'; do you have write permission?" % (name, path)
                return 1

    _mkdir(absolute_path, owner_user, owner_group, mode)

    if 0 != set_file_owner(absolute_path, owner_user, owner_group, recursive=recursive_fix_owner, ignore_ownership_errors=ignore_ownership_errors):
        return 1

    try:
        # The dir might have existed before but with old permissions, so reset it:
        if stat.S_IMODE(os.stat(absolute_path).st_mode) != mode:
            os.chmod(absolute_path, mode)
    except Exception as e:
        print >>sys.stderr, "Error: can't update permission mode on %s to %s (%s)." % (absolute_path, oct(mode), e)
        return 1  # Even if ignore_ownership_errors is True, fail if top-level dir is wrong
    
    return 0


