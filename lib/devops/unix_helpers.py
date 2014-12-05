import copy
import fcntl
import glob
import os
import re
import signal
import socket
import struct
import sys
import time
from angel.util.pidfile import get_pid_from_pidfile, is_pid_running, is_any_pid_running


def hard_kill_all(lockpath, yes_i_understand_this_is_potentially_dangerous=False, pid_map=None, send_sigterm=True, send_sigkill=True):
    ''' Given a path to a lock dir or a lock file, send SIGTERM to all listed processes and all children processes. '''

    if not yes_i_understand_this_is_potentially_dangerous:
        # This is a really aggressive function -- it builds a list of all subprocesses owned by the pid given in a pidfile -- 
        # or if path is a dir, every pid file in a dir AND KILLS THEM AND ALL DESCENDANT PROCESSES. This is the equivalent
        # of "kill -9 -1", but scoped to the descendants of a given process.
        return

    pids_in_lockfiles = {}
    if pid_map is None:
        pid_map = get_pid_relationships()
    if pid_map is None:
        print >>sys.stderr, "Skipping hard kill; no pid_map!"
        return 0

    if os.path.isfile(lockpath):
        pid = get_pid_from_pidfile(lockpath)
        if pid is None or pid not in pid_map:
            return
        pids_in_lockfiles[pid] = pid_map[pid]

    elif os.path.isdir(lockpath):
        for root, subdirs, files in os.walk(lockpath):
            for file in files:
                lockfile = os.path.join(root,file)
                this_pid = get_pid_from_pidfile(lockfile)
                if this_pid is None: continue
                if this_pid not in pid_map:
                    # This happens on stale lockfiles
                    pid_map[this_pid] = {}
                pids_in_lockfiles[this_pid] = pid_map[this_pid]

    else:
        if pid_map is None:
            print >>sys.stderr, "Skipping hard kill; lock path %s isn't a directory or file." % lockpath
            return 0

    # We now have a table of all the lockfile-based pids. Some of them are actually children of others; need to eliminate those.
    for pid in dict(pids_in_lockfiles):
        if pid not in pid_map:
            del pids_in_lockfiles[pid]
            continue
        for child_pid in pid_map[pid]:
            if child_pid in pids_in_lockfiles:
                del pids_in_lockfiles[child_pid]


    def _kill(pid_list, signal):
        kill_count = 0
        for pid in pid_list:
            try:
                if is_pid_running(pid):
                    print >>sys.stderr, " %s" % (pid),
                    os.kill(pid, signal)
                    kill_count += 1
            except:
                print >>sys.stderr, "-->Error!",
        return kill_count

    if 0 == len(pids_in_lockfiles):
        print >>sys.stderr, "No processes found to hard kill."
        return

    pids_to_kill = []
    for pid in pids_in_lockfiles:
        pids_to_kill += get_all_children_of_process(pid)

    if send_sigterm:
        print >>sys.stderr, "Hard-stopping services:  kill -TERM",
        sys.stderr.flush()
        kill_count = _kill(pids_to_kill, signal.SIGTERM)
        if 0 == kill_count:
            print >>sys.stderr, " ...no processes found to kill! This is normal if processes are already stopped."
            return

    if send_sigterm and send_sigterm:
        wait_time = 4
        try:
            while wait_time > 0:
                time.sleep(0.5)
                wait_time -= 0.5
                if not is_any_pid_running(pids_to_kill):
                    print >>sys.stderr, " all processes exited."
                    return
        except KeyboardInterrupt:
            print >>sys.stderr, "Aborted by ctrl-c."
            return -1

    if send_sigkill:
        print >>sys.stderr, "Hard-stopping services:  kill -KILL",
        sys.stderr.flush()
        kill_count = _kill(pids_to_kill, signal.SIGKILL)
        if 0 == kill_count:
            print >>sys.stderr, " ...no remaining processes were killed."
            return
        print >>sys.stderr, " done."


def get_proc_title():
    global _set_proc_title_value
    return _set_proc_title_value
    # Don't use setproctitle.getproctitle(); it has additional formatting on it


_set_proc_title_value = None
_set_proc_title_version_info = None
def set_proc_title(title, version_info=None):
    # We need the unformatted title for use with our get_proc_title wrapper, so quick solution is to cache it globally:
    global _set_proc_title_value
    _set_proc_title_value = title

    # We don't always have a way to get the version info (the config dict may not be available in a sub-function), 
    # but as a hack, we'll shove it in a global and re-use it on future calls when missing. Ugly, but fast and works ok.
    global _set_proc_title_version_info
    if version_info is not None:
        _set_proc_title_version_info = version_info
    if version_info is None and _set_proc_title_version_info is not None:
        version_info = _set_proc_title_version_info

    # Generate a formatted process name that IDs use project name and a version, along with whatever title / status we're being given:
    slug = '[angel] '  # To-do: refactor to use project name
    if version_info is not None:
        slug = "[angel.%s] " % version_info
    if title is None or len(title) == 0:
        title = 'untitled process'
    slug += title
    slug = slug.replace('(','[').replace(')',']') # Do not allow ()'s in proc title, will screw up parseing in get_pid_relationships

    try:
        import setproctitle
        setproctitle.setproctitle(slug)
    except:
        pass


def get_pid_relationships():
    ''' We sometimes need to check if a process is running correctly by looking at its own children.
        This returns a mapping of process ids to children process ids, or None if we can't determine it. '''
    # {'19474': ['19554'], '20792': ['22361'], '12943': ['12972'], '5985': ['6282'], ...
    pid_map = None
    def _add_mapping(pid, parent_pid):
        pid = int(pid)
        parent_pid = int(parent_pid)
        if pid not in pid_map:
            pid_map[pid] = []
        if parent_pid is not None:
            if parent_pid not in pid_map:
                pid_map[parent_pid] = []
            pid_map[parent_pid].append(pid)

    if sys.platform == 'darwin':
        # Then we're on OS X:
        pid_map = {}
        from devops.process_helpers import get_command_output
        data, output_stderr, exitcode = get_command_output('ps', ('-axo', 'pid,ppid'))
        if exitcode != 0 or data is None:
            print >>sys.stderr, "Warning: unable to build process relationship map (no /proc and no ps output; exit %s, error %s)" % (exitcode, output_stderr)
            return None
        for row in data.split('\n'):
            m = re.search('([0-9]+)\s+([0-9]+)', row)
            if m is not None:
                pid = m.group(1)
                parent_pid = m.group(2)
                _add_mapping(pid, parent_pid)

    if os.path.isdir('/proc'):
        # Then we're on a linux-based system:
        pid_map = {}
        for proc in glob.glob('/proc/[0-9]*'):
            if not os.access('%s/status' % proc, os.R_OK):
                continue
            try:
                pid = proc[6:]
                stat = open('%s/status' % proc)
                parent_pid = None
                while True:
                    data = stat.readline()
                    if 0 == len(data): 
                        print >>sys.stderr, "Warning: couldn't find parent id for process %s" % pid
                        break
                    k, v = data.split('\t')
                    if k == 'PPid:':
                        parent_pid = v
                        break
                stat.close()
                _add_mapping(pid, parent_pid)
            except:
                pass # This can happen if the process no longer exists, in which case, continue on to the next process       

    return pid_map


def get_all_children_of_process(pid, pid_map=None):
    ''' Return a list of all descendant processes of a given pid (including pid), ordered with child processes ALWAYS after parent processes in list.
        Returns empty list on errors. 
    '''
    # Make sure to keep parents before children, otherwise children might just come back via respawn
    if pid_map is None:
        pid_map = get_pid_relationships()
        if pid_map is None:
            return []
    if pid not in pid_map:
        return []
    children_pids = [pid,]
    for p in pid_map[pid]:
        children_pids += get_all_children_of_process(p, pid_map=pid_map)
    return children_pids


# Helper function to send a signal to a service and wait for it to exit, for up to N seconds - 0 return on exited proc, non-zero otherwise:
def kill_and_wait(pid, name, kill_signal, timeout):
    if timeout < 2:
        timeout = 2
    try:
        #print >>sys.stderr, "Killer[%s]: running kill -%s %s on %s with %s second timeout" % (os.getpid(), kill_signal, pid, name, timeout)
        os.kill(pid, kill_signal)
    except:
       print >>sys.stderr, "Killer[%s]: kill -%s %s on %s failed; timeout is %s" % (os.getpid(), kill_signal, pid, name, timeout) # Could be that it already exited

    check_interval = 0.5
    while timeout >= 0:
        try:
            os.waitpid(pid, os.P_NOWAIT) # If we're killing a child process, then we as the parent process have to reap it
        except Exception as e:
            pass # Lots of "No child processes" most of the time
            #print >>sys.stderr, "kill_and_wait(%s,%s,%s,%s): %s" % (pid, name, kill_signal, timeout, e)
        if not is_pid_running(pid):
            #print >>sys.stderr, "Killer[%s]: running kill -%s %s on %s: SUCCESS" % (os.getpid(), kill_signal, pid, name)
            return 0
        try:
            time.sleep(check_interval)
        except Exception:
            print >>sys.stderr, "Killer[%s]: sleep failed during kill of %s; ignoring" % (os.getpid(), pid)
        timeout -= check_interval
    if kill_signal != signal.SIGHUP:
        print >>sys.stderr, "Killer[%s]: running kill -%s %s on %s: FAILED -- timed out" % (os.getpid(), kill_signal, pid, name)
    return 1


def is_ip_addr_local(ip):
    if ip in get_local_ip_addrs():
        return True
    return False
    

_devops_get_local_ip_addrs_cache = None
def get_local_ip_addrs():

    global _devops_get_local_ip_addrs_cache
    if _devops_get_local_ip_addrs_cache is not None: return copy.copy(_devops_get_local_ip_addrs_cache)

    addrs = []
    try:
        addrs = socket.getaddrinfo(socket.gethostname(), None) 
        # The above call will return a list of all addresses associated with our node, but will fail if the hostname doesn't resolve.
        # Some services patch socket to not read from /etc/hosts, in which case it'll throw an exception.
    except:
        pass

    ips = ['127.0.0.1',]  # Include localhost in case above addrs call fails, which it can on OS X
    lan_ip = _get_lan_ip()
    if lan_ip is not None:
        ips += [lan_ip]

    for item in addrs: 
        ips.append(item[4][0]) 

    _devops_get_local_ip_addrs_cache = sorted(list(set(ips)))
    return get_local_ip_addrs()


# Given an interface name (e.g. "eth0"), return the primary IP adress associated with the interface:
def get_interface_ip(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return socket.inet_ntoa(fcntl.ioctl(
                    s.fileno(),
                    0x8915,  # SIOCGIFADDR
                    struct.pack('256s', ifname[:15])
            )[20:24])


# Attempt to find a non "127.0.0.1" IP address for our node -- if /etc/hosts defines the hostname as localhost, then this will step over that.
def _get_lan_ip():
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except socket.gaierror as e:
        # This happens if the hostname doesn't properly resolve back to an IP:
        print >>sys.stderr, "Error: Unable to get ip address for %s: %s -- possibly entry missing in /etc/hosts?" % (socket.gethostname(), e)
        print >>sys.stderr, "This can happen if DHCP sets your hostname to a non-resolvable hostname. Try running:"
        print >>sys.stderr, "    sudo bash -c  \"echo '127.0.0.1  %s' >> /etc/hosts\""  % (socket.gethostname())
        return None
    if ip.startswith("127."):
        interfaces = ["eth0","eth1","eth2","wlan0","wlan1","wifi0","ath0","ath1","ppp0"]
        for ifname in interfaces:
            try:
                ip = get_interface_ip(ifname)
                break
            except IOError:
                pass
    return ip

