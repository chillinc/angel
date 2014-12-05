
from contextlib import closing

import datetime
import mmap
import os
import sys
import syslog
import time


def log_to_syslog(message, version_info=None):
    logname = 'angel'  # To-do: use project name here
    if version_info is not None:
        logname += '-v%s' % version_info
    logname += '[%s]' % os.getpid()
    try:
        syslog.openlog(logname)
        syslog.syslog(message)
    except:
        print >>sys.stderr, "Can't write to syslog?"


def log_to_wall(calling_username, command_called):
    ''' Potentially broadcase (like wall) to all logged-in users that the given command was run.
        We do this so that multiple users logged in will be aware of what commands (like stopping services)
        may have been run by a different user.
        This only works for sudo-based calls, but for now, that's probably just fine for us.
    '''
    if not os.path.isdir('/proc') or 0 != os.getuid():
        return
    try:
        for proc in os.listdir('/proc'):
            env_file = os.path.join('/proc', proc, 'environ')
            env = {}
            try:
                env_lines = open(env_file).read().split('\0')
                for line in env_lines:
                    if '=' not in line:
                        continue
                    (key, value) = line.split('=',1)
                    env[key] = value
            except Exception as e:
                continue  # Might not be a process dir, process might have exited, might not have read access...
            if 'LC_DEPLOY_USER' not in env or 'SSH_TTY' not in env:
                continue
            if not env['SSH_TTY'].startswith('/dev/pts') or not os.path.exists(env['SSH_TTY']):
                print >>sys.stderr, "Process %s: weird SSH_TTY path %s" % (proc, env['SSH_TTY'])  # This can happen with screen; also: need to trach which ttys already written, so sudo -s doesn't get double-write
                continue
            if calling_username == env['LC_DEPLOY_USER']:
                continue
            alert_message = '\n' + \
                ' *** admin alert ***\n' + \
                '    command:  %s\n' % (command_called) + \
                '       user:  %s\n' % (calling_username) + \
                '       time:  %s\n' % (datetime.datetime.utcnow()) + \
                '\n'
            open(env['SSH_TTY'], 'w').write(alert_message)
    except:
        pass


def log_get_logfile_paths(base_logdir, filters=(), include_system_logs=True):
    ''' Return absolute path to logfiles in our log dir, along with paths to some common system log files.
        Returns None if logdir is missing
        If filters is given, only those log files whose names match the filters are included.
        Filters that start with a '-' cause logs that match that name to be excluded from the list.
        Filters that start with a '+' require the log name to include that string (as opposed to or-ing the names)

        Not all system logfiles are included; the intent here is to capture those that are likely
        to contain info as an extra catch for those services that use syslog (e.g. postfix).

        Note: compressed logfiles or .N numbered logfiles (from rotations) are excluded.

        '''
    if not os.path.isdir(base_logdir):
        print >>sys.stderr, "Error: log dir '%s' doesn't exist." % base_logdir
        return None

    # Start with a list of system-based logs:
    logfiles = []
    if include_system_logs:
        logfiles = ['/var/log/syslog', '/var/log/messages', '/var/log/system.log', '/var/log/cron', 
                    '/var/log/mail.info', '/var/log/mail.err', '/var/log/mail.warn', '/var/log/mail.log', 
                    '/var/log/maillog']  # To-do: include /var/log/<project>*
    for log in logfiles[:]:
        if not os.path.isfile(log):
            logfiles.remove(log)

    try:
        for root, dirs, files in os.walk(base_logdir):
            for file in files:
                file_path = os.path.abspath(os.path.join(root, file))
                if file_path.endswith('.gz') or file_path.endswith('.bz2') or file_path[-1:].isdigit():
                    continue
                logfiles += (file_path,)
    except Exception as e:
        print >>sys.stderr, "Error: exception generating log list (%s)." % e
        return None

    # Filter the list of logs down, removing logs that match -filter, only including logs that match at least one filter names, and if +filters given, require them always:
    if len(filters):
        filterd_logfiles = []

        # Before filtering out options, check if we have ONLY +/- filters -- in which case we have to start with all logs, instead of or-ing based on normal filters:
        normal_filter_seen = False
        for filter in filters:
            if filter[0] == '-' or filter[0] == '+':
                continue
            normal_filter_seen = True
        if not normal_filter_seen:
            filterd_logfiles = logfiles

        # For each standard "or" filter, add into our final list logs if any filter matches:
        for filter in filters:
            if filter[0] == '-' or filter[0] == '+':
                continue
            for log in logfiles:
                if log.lower().find(filter[0:].lower()) >= 0:
                    filterd_logfiles += [log]

        # For each negative filter, remove logfiles:
        for filter in filters:
            if filter[0] == '-':
                for log in filterd_logfiles[:]:
                    if log.lower().find(filter[1:].lower()) >= 0:
                        filterd_logfiles.remove(log)

        # For each positive filter, remove logfiles:
        for filter in filters:
            if filter[0] != '+':
                continue
            for log in filterd_logfiles[:]:
                if not log.lower().find(filter[1:].lower()) >= 0:
                    filterd_logfiles.remove(log)

        logfiles = filterd_logfiles

    # Always remove compressed logfiles:
    logfiles = [l for l in logfiles if not l.endswith('.tgz') and not l.endswith('.gz')]

    return sorted(set(logfiles))


def log_tail_logs(log_dir, filters=None, show_timestamps=True, show_names=False, line_preamble='', colored_output=False, last_n_lines=None):
    ''' Tail logs in the given log_dir.
        filters, if given (as a list), filters log filenames (multiple values ORed together; +name for requiring a match; -name for exlcuding).
    '''

    color_start = ''
    color_end = ''
    if colored_output:
        color_start = '\033[1;31m'
        color_end = '\033[0m'

    error_log_color_start = ''
    error_log_color_end = ''
    if colored_output:
        error_log_color_start = '\033[0;31m'
        error_log_color_end = '\033[0m'

    def _print_to_stderr(message):
        print >>sys.stderr, "%s%s%s" % (color_start, message, color_end)

    def _print_log_line(log, line):
        if 0 == len(line):
            return
        line_preamble = ''
        this_color_start = ''
        this_color_end = ''
        if show_timestamps:
            line_preamble = "%0.2f " % time.time()
        if show_names:
            line_preamble += log + ' '
        if '-error.log' in log:
            this_color_start = error_log_color_start
            this_color_end = error_log_color_end
        if line[-1] == '\n':
            line = line[:-1]
        print "%s%s%s%s" % (this_color_start, line_preamble, line, this_color_end)

    if last_n_lines:
        if show_timestamps:
            show_timestamps = False

    logs = log_get_logfile_paths(log_dir, filters=filters)
    if logs is None:
        _print_to_stderr("Warning: log dir '%s' is currently missing." % log_dir)
        if last_n_lines:
            return 1
    elif len(logs) == 0:
        if last_n_lines:
            print >>sys.stderr, "Error: no logs match filter '%s'." % (' '.join(filters))
            return 1
        else:
           # If in "tail -f" mode, warn if no logs match filter string -- this could change if logs are about to be created:
            _print_to_stderr("Warning: no logs currently match filter '%s'." % (' '.join(filters)))

    if last_n_lines:
        # Instead of tailing the logs, show the last N lines of each logfile:
        try:
            for log in sorted(logs):
                log_file = os.path.join(log_dir, log)
                try:
                    if 0 == os.stat(log_file).st_size:
                        continue
                except:
                    continue
                lines = log_last_lines_from_file(log_file, last_n_lines)
                if lines:
                    for line in lines.split('\n'):
                        _print_log_line(log,line)
                else:
                    print >>sys.stderr, "Warning: error reading '%s'" % log
            return 0
        except (IOError, KeyboardInterrupt):
            return 1

    open_files = {}
    open_files_innode_check = {}  # Store the inode number of the given file, so if it changes we can detect and re-open the file

    first_pass = True
    time_of_last_log_sweep = 0
    time_of_last_io_error_on_file = {}
    try:
        while True:

            # Every two seconds or so, scan the log dir for files that match our filter expression.
            # We do it this way so that files created after 'show logs <filter>' get auto-added into our output.
            if time.time() - time_of_last_log_sweep > 2:
                time_of_last_log_sweep = time.time()
                logs = log_get_logfile_paths(log_dir, filters=filters)
                for log in (logs or {}):  # logs can be None if the dir gets deleted
                    try:
                        log_inode = os.stat(log).st_ino
                    except Exception as e:
                        if log not in time_of_last_io_error_on_file or time.time() - time_of_last_io_error_on_file[log] > 60:  # quick way to avoid printing out error in a loop
                            print >>sys.stderr, "Warning: unable to stat logfile '%s'; skipping it (%s)." % (log, e)
                            time_of_last_io_error_on_file[log] = time.time()
                        continue
                    if log in open_files_innode_check:
                        # Then file is already opened -- check if it's rolled over:
                        if open_files_innode_check[log] != log_inode:
                            open_files[log].close()
                            try:
                                open_files[log] = open(log, 'r')
                                open_files_innode_check[log] = log_inode
                                _print_to_stderr("(Log %s rolled over)" % log)
                            except Exception as e:
                                _print_to_stderr("(Log %s reset; error reading new file: %s)" % (log, e))
                                if log in open_files:
                                    del open_files[log]
                                if log in open_files_innode_check:
                                    del open_files_innode_check[log]
                    else:
                        # Otherwise this is a new file to tail:
                        try:
                            l = open(log, 'r')
                            open_files_innode_check[log] = log_inode
                            open_files[log] = l
                            if first_pass:
                                open_files[log].seek(0, 2)  # skip to EOF only on first pass -- otherise it's a new file created after startup
                            else:
                                _print_to_stderr("(Log %s added)" % log)
                        except Exception as e:
                            if "Permission denied" in str(e):
                                print >>sys.stderr, "Warning: unable to open logfile '%s' (Permission denied; try sudo?)." % (log)
                            else:
                                print >>sys.stderr, "Warning: unable to open logfile '%s' (%s)." % (log, e)
                first_pass = False
                logs_to_del = ()
                for log in open_files:
                    if logs is None or log not in logs:  # logs will be None if the entire log_dir is deleted / reset
                        logs_to_del += (log,)
                for log in logs_to_del:
                    _print_to_stderr("%s(Log %s dissappeared)%s" % (color_start, log, color_end))
                    open_files[log].close()
                    del open_files[log]
                    del open_files_innode_check[log]

            lines_seen = False
            for log in open_files:
                line = open_files[log].readline()
                while len(line):
                    lines_seen = True
                    _print_log_line(log, line)
                    line = open_files[log].readline()
            if not lines_seen:
                time.sleep(0.125)

    except KeyboardInterrupt:
        return 130  # tail -f returns this on ^C, so we do too...

    except Exception as e:
        print >>sys.stderr, "Error showing logs: %s" % e

    return 1


def log_last_lines_from_file(file_name, count):
    try:
        if 0 == os.stat(file_name).st_size:
            return ''
        with open(file_name) as f:
            with closing(mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)) as mm_data:
                return log_last_lines_from_string(mm_data, count)
    except Exception as e:
        print >>sys.stderr, "Error: unable to tail %s (%s)" % (file_name, e)
        return None


def log_last_lines_from_string(data, count):
    last_offset = len(data)
    try:
        for i in xrange(count):
            offset = data.rfind('\n', 0, last_offset-1)
            last_offset = offset
    except ValueError:
        pass
    return data[last_offset+1:]

