import errno
import grp
import os
import pwd
import signal
import stat
import string
import subprocess
import sys
import time

def write_pidfile(filename, pid, extra_data={}, owner_user=None, owner_group=None, print_errors=True):
    ''' Create or update the given file for the given pid, storing extra_data.
        If the given pidfile already exists, then only the process id stared in the file is able to update it.'''

    if pid is not None and (pid < 2 or len(str(pid)) == 0):
        if print_errors:
            print >>sys.stderr, "Error: invalid pid %s for pidfile '%s'." % (pid, filename)
        return -1
    if '/' != filename[0]:
        if print_errors:
            print >>sys.stderr, "Error: invalid pidfile path '%s'." % filename
        return -1

    # Make sure the lock directory exists -- if not, create it:
    if not os.path.exists(os.path.dirname(filename)):
        try:
            os.makedirs(os.path.dirname(filename))
        except:
            if print_errors:
                print >>sys.stderr, "Error: unable to create lock dir for lockfile '%s'." % filename
            return -1

    # Make sure that if the pidfile already exists, that only the pid in the pidfile is updating the contents:
    if os.path.exists(filename):
        pid_in_file = get_pid_from_pidfile(filename)
        if pid_in_file is not None:
            if is_pid_running(pid_in_file):
                if os.getpid() != pid_in_file:
                    if print_errors:
                        print >> sys.stderr, "Error: pidfile '%s' already exists (owned by pid %s; won't allow process %s to edit)." % (filename, pid_in_file, os.getpid())
                    return -1
                if os.getpid() != pid and pid is not None:
                    if print_errors:
                        print >> sys.stderr, "Error: process %s tried to assign pidfile to process %s" % (os.getpid(), pid)
                    return -1
            else:
                # Stale pidfile -- allow new request to over-write it. This is okay to happen when a lockfile is left behind with status message / data.
                pid_in_file = None

    try:
        if pid is None and len(extra_data) == 0:
            # Then we shouldn't create or update a pidfile...
            if os.path.exists(filename):
                try:
                    os.remove(filename)
                except:
                    print >> sys.stderr, "Error: process %s tried to set pidfile to empty / no data, but can't delete it." % os.getpid()
                    return -1
            return 0

        tmp_filename = "%s.%s" % (filename, os.getpid())
        f = open(tmp_filename, 'wt')
        if pid is None:
            f.write("\n") # We may want to keep a pid file around for its extra_data even after the process has exited
        else:
            f.write("%s\n" % pid)
        for key in sorted(extra_data):
            f.write("%s=%s\n" % (key, str(extra_data[key]).replace('\n', ' | ')))
        f.close()
        os.rename(tmp_filename, filename)

    except IOError as e:
        if print_errors:
            print >>sys.stderr, "Error: IOError while updating pidfile '%s' (%s)" % (filename, e)
        return -1 # This can happen if the user doesn't have permission or the path doesn't exist

    from devops.file_and_dir_helpers import set_file_owner
    if 0 != set_file_owner(filename, owner_user, owner_group):
        if print_errors:
            print >>sys.stderr, "Error: unable to update file owner on pidfile '%s'" % filename
        return -1

    if pid is not None:
        check_pid = get_pid_from_pidfile(filename)
        if check_pid is None or check_pid != pid:
            if print_errors:
                print >> sys.stderr, "Error: pidfile '%s' already exists (race condition? check_pid: %s, pid: %s)." % (filename, check_pid, pid)
            return -1
 
    return 0


def release_pidfile(filename):
    ''' Mark the pidfile at given filename path as no longer being an active lock. '''
    data = {}
    if os.path.exists(filename):
        data = read_data_from_lockfile(filename, print_warnings=False)
        if data is None:
            print >>sys.stderr, "Error: release_pidfile: unable to read data from %s" % filename
            return -1
    return write_pidfile(filename, None, extra_data=data)


def update_pidfile_data(filename, new_data, owner_user=None, owner_group=None):
    ''' Updates or sets keys in new_data, preserving data previously. To delete a value, create an empty or None value for the given key in new_data. '''
    data = {}
    if os.path.exists(filename):
        data = read_data_from_lockfile(filename)
        if data is None:
            return -1
    for key in new_data:
        value = new_data[key]
        if value is None or len(str(value)) == 0:
            if key in data:
                del data[key]
        else:
            data[key] = value
    return write_pidfile(filename, os.getpid(), extra_data=data, owner_user=owner_user, owner_group=owner_group)


def is_pid_running(pid):
    if pid is None or pid < 2:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError, exc:
        if errno.EPERM == exc.errno:
            return True
        if errno.ESRCH == exc.errno:
            # No such process... definitely false
            return False
        print >>sys.stderr, "is_pid_running(%s) received errno %s" % (pid, exc.errno)
        return False


def get_only_running_pids(pid_list):
    ''' Given a list of process IDs, return a list of only those pids that are running. '''
    ret_val = []
    for pid in pid_list:
        if is_pid_running(pid):
            ret_val += [pid,]
    return pid_list


def is_any_pid_running(pid_list):
    ''' Given a list of process IDs, return True if at least one is running; false otherwise. '''
    for pid in pid_list:
        if is_pid_running(pid):
            return True
    return False


def get_pid_from_pidfile(filename):
    try:
        first_line = open(filename, 'r').readline()
        if 0 == len(first_line):
            return None
        return int(first_line)
    except:
        return None


def read_data_from_lockfile(filename, print_warnings=True):
    data = {}
    if not os.path.isfile(filename):
        return None
    try:
        stale_pid = False
        f = open(filename, 'r')
        try:
            pid = int(f.readline())
            if not is_pid_running(pid):
                stale_pid = True
        except:
            pass # pid can be set to "" -- this happens when a dead worker leaves status messages around for a daemon to show

        line_counter = 1
        while True:
            line = f.readline()
            line_counter += 1
            if len(line) == 0:
                break;
            try:
                key, value = line.split('=')
                data[key] = value[:-1] # Strip \n aff the end
            except:
                print >>sys.stderr, "Parse error on line %s, lockfile %s; ignoring. (Data: %s)" % (line_counter, filename, line)
        if stale_pid:
            if len(data) == 0:
                print >>sys.stderr, "Warning: removing stale pidfile %s, no data and invalid pid." % filename
                os.remove(filename)
            else:
                if print_warnings:
                    print >>sys.stderr, "Warning: stale pid in pidfile %s" % filename
                
    except:
        print >>sys.stderr, "Error reading data from lockfile %s" % filename
        return None
    return data


def is_pid_in_pidfile_running(filename, remove_stale_pidfile=False):
    ''' Check if the pid in given file is running. Only returns true if file exists and lists an active process id. '''
    if '/' != filename[0]:
        print >>sys.stderr, "Warning: invalid pidfile path %s" % filename
    if not os.path.exists(filename):
        return False
    pid_value = get_pid_from_pidfile(filename)
    is_running = is_pid_running(pid_value)
    if not is_running:
        if remove_stale_pidfile:
            try:
                if pid_value is not None:  # Only print warning for stale pidfiles, not ones left behind for status data
                    print >>sys.stderr, 'Removing stale pidfile %s in process id %s because we found no process id %s' % (filename, os.getpid(), pid_value)
                os.remove(filename)
            except OSError:
                print >> sys.stderr, 'Unable to remove stale pid file %s; no write access?' % filename
        return False
    return True


def is_pid_in_pidfile_our_pid(filename):
    pid = get_pid_from_pidfile(filename)
    if pid is None:
        return False
    if os.getpid() == pid:
        return True
    return False
