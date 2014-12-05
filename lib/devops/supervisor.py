import fcntl
import os
import random
import select
import signal
import sys
import time
import traceback

from devops.file_and_dir_helpers import *
from angel.util.pidfile import *
from devops.unix_helpers import set_proc_title
from angel.stats.disk_stats import disk_stats_get_usage_for_path
from devops.process_helpers import *
import angel.settings

# This function is similar to Python's subprocess module, with some tweaks and customizations.
# Like subprocess, it forks a child process, waits for it to exit, and re-starts it on exit. It never returns.
# Our supervisor handles shutdown conditions, calling a stop_func when the supervisor process receives SIGTERM.
# We also handle log rotation, rolling over stdout/stderr when the supervisor process receives SIGWINCH.
# Most other signals are propogated to the child process -- that is, sending the supervisor process SIGHUP will
# be passed through to the child process.

def supervisor_manage_process(config, name, pid_filename_for_daemon, run_as_user, run_as_group, log_basepath,
                              restart_daemon_on_exit, process_oom_adjustment, init_func, exec_func, stop_func):

    ''' Creates and manages a child process, running given functions.
         - If init_func is defined, it is called in the child process first. If it returns a non-zero status, then supervisor will exit.
         - exec_func is then called. If restart_daemon_on_exit is True, exec_func is restarted whenever it exits.
         - If stop_func is defined, it is called when this managing process receives a SIGTERM.
         - pid_filename_for_daemon is used by this manager process to update status info and track that the manager should be running.
         - process_oom_adjustment is a value, typically between -15 and 0, that indicates to the Linux kernel how "important" the process is.
        This function never returns.
      '''

    # Create supervisor logger:
    supervisor_logfile_path = launcher_get_logpath(config, log_basepath, 'supervisor')
    if 0 != create_dirs_if_needed(os.path.dirname(supervisor_logfile_path), owner_user=run_as_user, owner_group=run_as_group):
        print >>sys.stderr, "Supervisor error: unable to create log dirs."
        os._exit(0)  # Never return
    try:
        supervisor_logger = SupervisorLogger(open(supervisor_logfile_path, 'a', buffering=0))
    except Exception as e:
        print >>sys.stderr, "Supervisor error: unable to create supervisor log (%s: %s)." % (supervisor_logfile_path, e)
        os._exit(0)  # Never return

    # Send SIGTERM to the supervisor daemon to tell it to quit the child process and exit.
    # Send SIGWINCH to the supervisor daemon to tell it to rotate logs.
    # Any other trappable_signal is sent to the child process to do any service-defined logic as necessary.
    trappable_signals = (signal.SIGINT, signal.SIGWINCH, signal.SIGHUP, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2, signal.SIGQUIT)

    global supervisor_daemon_exit_requested
    supervisor_daemon_exit_requested = False

    global run_init_instead_of_exec
    run_init_instead_of_exec = False

    set_proc_title('supervisor[%s]: starting' % name)

    # Always run supervisor with kernel out-of-memory flags set to hold off on killing us.
    # This is reset back up to 0 in the child process (or whatever process_oom_adjustment is set to).
    set_process_oom_factor(-15)

    supervisor_pid = os.getpid()
    child_pid = None
    daemon_start_time = int(time.time())
    last_start_time = None
    start_count = 0
    continous_restarts = 0
    min_delay_between_continous_restarts = 5
    max_delay_between_continous_restarts = 30
    restart_delay_jitter = 60  # If we hit max_delay, we'll re-try at some interval between (max_delay - jitter) and (max_delay)

    # Define a function that waits for a child pid to exit OR for us to receive a signal:
    def _supervisor_daemon_waitpid(pid):
        if pid is None or pid < 2:
            supervisor_logger.warn("Supervisor[%s]: can't wait on invalid pid %s." % (name, pid))
            return -1
        try:
            # To-do: periodically wake up and check that pid_filename_for_daemon contains our pid, or exit
            (wait_pid, wait_exitcode) = os.waitpid(pid, 0)
            return (wait_exitcode >> 8) % 256
        except OSError:
            return -2  # waitpid will throw an OSError when our supervisor recieves a kill signal (i.e. SIGTERM to tell us to exit); our code below will loop and re-call this.
        return -3


    # Define a function that receives a signal and passes it through to our child process:
    def _supervisor_daemon_signal_passthru(signum, frame):
        if child_pid is None or child_pid < 2:
            # This can happen if the supervised child was *just* killed, or isn't running yet (during a re-spawn).
            supervisor_logger.warn("Supervisor: invalid pid %s found during kill -%s of process %s" % (child_pid, signum, name))
            return
        try:
            supervisor_logger.info("_supervisor_daemon_signal_passthru: kill -%s %s" % (signum, child_pid))
            os.kill(child_pid, signum)
        except Exception as e:
            supervisor_logger.error("Supervisor %s[%s/%s managing %s]: unable to send signal %s to pid %s: %s" % (name, supervisor_pid, os.getpid(), child_pid, signum, child_pid, e))


    # Define a function that receives a signal and rotates logs:
    def _supervisor_daemon_rotate_logs(signum, frame):
        supervisor_logger.info("Supervisor %s[%s/%s managing %s]: rotate logs not implemented yet; log_basepath=%s" % (name, supervisor_pid, os.getpid(), child_pid, log_basepath))


    # Define a function that receives a signal and cleanly shuts down the server:
    def _supervisor_daemon_quit(signum, frame):

        # Flag that quit has been requested:
        global supervisor_daemon_exit_requested
        supervisor_daemon_exit_requested = True

        if child_pid is None or child_pid < 2:
            # This can happen if the supervised child was *just* killed, or isn't running yet (during a re-spawn).
            supervisor_logger.warn("Supervisor: invalid pid %s found during kill -%s of process %s" % (child_pid, signum, name))
            return

        # Check if we're still in an init phase (can't call stop_func on something that hasn't actually started):
        global run_init_instead_of_exec
        if run_init_instead_of_exec:
            # if we're currently invoking a custom init function, then we need to send the supervisor process the kill signal directly so it exits
            return _supervisor_daemon_signal_passthru(signum, frame)

        # Run stop function if given, otherwise pass along given kill signal to child process:
        if stop_func is not None:
            try:
                import threading
                supervisor_logger.info("Supervisor %s[%s/%s managing %s]: quit request received (sig %s in thread %s); calling stop function" % (name, supervisor_pid, os.getpid(), child_pid, signum, threading.currentThread().name))
                ret_val = stop_func(child_pid)
                supervisor_logger.info("Supervisor %s[%s/%s managing %s]: quit request received (sig %s in thread %s); stop function done (%s)" % (name, supervisor_pid, os.getpid(), child_pid, signum, threading.currentThread().name, ret_val))
                return
            except Exception:
                supervisor_logger.error("Supervisor %s[%s/%s managing %s]: error in stop function: %s" % (name, supervisor_pid, os.getpid(), child_pid, traceback.format_exc(sys.exc_info()[2])))
        else:
            supervisor_logger.warn("Supervisor %s[%s/%s managing %s]: no stop function given" % (name, supervisor_pid, os.getpid(), child_pid))
        return _supervisor_daemon_signal_passthru(signum, frame)


    def _install_signal_functions():
        signal.signal(signal.SIGWINCH, _supervisor_daemon_rotate_logs)
        signal.signal(signal.SIGTERM, _supervisor_daemon_quit)
        for sig in trappable_signals:
            if sig not in (signal.SIGWINCH, signal.SIGTERM):
                signal.signal(sig, _supervisor_daemon_signal_passthru)

    def _remove_signal_functions():
        for sig in trappable_signals:
            signal.signal(sig, signal.SIG_DFL)

    def _sleep_without_signal_functions(duration):
        # Because there are cases where *we* need to be interrupted:
        _remove_signal_functions()
        time.sleep(duration)
        _install_signal_functions()

    # Install signal functions:
    _install_signal_functions()

    # chdir() to /, to avoid potentially holding a mountpoint open:
    os.chdir('/')

    # Reset umask:
    os.umask(022)

    # Redirect STDOUT/STDERR:
    # (Redirects run as separate threads in our supervisor process -- don't move these to the child process; os.exec will wipe them out.)
    os.setsid()
    stdout_redirector = SupervisorStreamRedirector(supervisor_logger, launcher_get_logpath(config, log_basepath, ''), run_as_user=run_as_user, run_as_group=run_as_group)
    stderr_redirector = SupervisorStreamRedirector(supervisor_logger, launcher_get_logpath(config, log_basepath, 'error'), run_as_user=run_as_user, run_as_group=run_as_group)
    supervisor_redirector = SupervisorStreamRedirector(supervisor_logger, launcher_get_logpath(config, log_basepath, 'supervisor'), run_as_user=run_as_user, run_as_group=run_as_group)
    stdout_redirector.startRedirectThread(sys.stdout)
    stderr_redirector.startRedirectThread(sys.stderr)
    supervisor_redirector.startRedirectThread(supervisor_logger.logger_fd)

    # Close STDIN:
    sys.stdin.close()
    os.close(0)
    new_stdin = open(os.devnull, 'r', 0)  # So FD 0 isn't available

    #new_stdin = open(os.devnull, 'r', 0)
    #try:
    #    os.dup2(new_stdin.fileno(), sys.stdin.fileno())
    #except ValueError:
    #    print >>sys.stderr, "Can't set up STDIN, was it closed on us?"


    # Loop until shutdown requested, handling signals and logs and making sure that our server remains running:
    while not supervisor_daemon_exit_requested:

        if not is_pid_in_pidfile_our_pid(pid_filename_for_daemon):
            supervisor_logger.warn("Supervisor[%s/%s]: Warning: invalid pid %s in lock file %s. Re-checking..." % (supervisor_pid, os.getpid(), get_pid_from_pidfile(pid_filename_for_daemon), pid_filename_for_daemon))
            try:
                time.sleep(0.5)
            except:
                pass
            if not is_pid_in_pidfile_our_pid(pid_filename_for_daemon):
                supervisor_logger.error("Supervisor[%s/%s]: FATAL: invalid pid %s in lock file %s. Exiting now." % (supervisor_pid, os.getpid(), get_pid_from_pidfile(pid_filename_for_daemon), pid_filename_for_daemon))
                sys.stdout.flush()
                sys.stderr.flush()
                time.sleep(0.5) # Need to sleep so that logger threads can write out above stderr message. Gross, but it works.
                os._exit(1)

        lockfile_pid = get_pid_from_pidfile(pid_filename_for_daemon)
        if lockfile_pid is None or supervisor_pid != lockfile_pid:
            supervisor_logger.error("Supervisor[%s/%s]: FATAL: lock file %s not owned by current process! (pid is %s) Exiting now." % (supervisor_pid, os.getpid(), pid_filename_for_daemon, lockfile_pid))
            os._exit(1)

        one_time_run = False
        run_init_instead_of_exec = False
        if start_count == 0 and init_func is not None:
            run_init_instead_of_exec = True
        if not restart_daemon_on_exit:
            # This is a clever trick: we might want to run a command in the background one-time (i.e. priming a service).
            # By passing restart_daemon_on_exit as false from way up above us in the callstack,
            # we can use our run logic inside the supervisor process and let it exit cleanly.
            # This works by reading one_time_run after we've started and flipping supervisor_daemon_exit_requested to True.
            one_time_run = True

        try:
            log_disk_stats = disk_stats_get_usage_for_path(config['LOG_DIR'])
            data_disk_stats = disk_stats_get_usage_for_path(config['DATA_DIR'])
            run_disk_stats = disk_stats_get_usage_for_path(config['RUN_DIR'])
            if log_disk_stats is not None and data_disk_stats is not None and run_disk_stats is not None:
                # Only do this check when we can get stats -- otherwise it's possible to rm -rf log_dir and then have the service die.
                if log_disk_stats['free_mb'] < 100 or data_disk_stats['free_mb'] < 100 or run_disk_stats['free_mb'] < 100:
                    supervisor_logger.error("Supervisor[%s/%s]: insufficent disk space to run %s." % (supervisor_pid, os.getpid(), name))
                    try:
                        _sleep_without_signal_functions(10)
                    except:
                        supervisor_daemon_exit_requested = True
                    continue
        except Exception as e:
            supervisor_logger.error("Supervisor[%s/%s]: disk check failed: %s" % (supervisor_pid, os.getpid(), e))

        if child_pid is None and not supervisor_daemon_exit_requested:
            if one_time_run:
                supervisor_daemon_exit_requested = True

            # Then we need to fork and start child process:
            try:
                sys.stdout.flush()  # If we have a ' print "Foo", ' statement (e.g. with trailing comma), the forked process ends up with a copy of it, too.
                sys.stderr.flush()
                child_pid = os.fork()
                if child_pid:

                    # Parent process:
                    supervisor_logger.info("Supervisor[%s/%s]: managing process %s running as pid %s" % (supervisor_pid, os.getpid(), name, child_pid))
                    set_proc_title('supervisor: managing %s[%s]' % (name, child_pid))

                    prior_child_start_time = last_start_time
                    last_start_time = time.time()
                    start_count += 1
                    if 0 != update_pidfile_data(pid_filename_for_daemon, { \
                                           angel.constants.LOCKFILE_DATA_DAEMON_START_TIME: daemon_start_time,           \
                                           angel.constants.LOCKFILE_DATA_PRIOR_CHILD_START_TIME: prior_child_start_time, \
                                           angel.constants.LOCKFILE_DATA_CHILD_START_TIME: int(time.time()),             \
                                           angel.constants.LOCKFILE_DATA_CHILD_PID: child_pid,                           \
                                           angel.constants.LOCKFILE_DATA_START_COUNT: start_count,                       \
                                       } ):
                        supervisor_logger.error("Supervisor[%s/%s]: error updating pidfile data in pidfile %s" % (supervisor_pid, os.getpid(), pid_filename_for_daemon))

                else:

                    # Child process:
                    supervisor_logger.info("Supervisor[%s/%s]: running %s" % (supervisor_pid, os.getpid(), name))
                    set_proc_title('supervisor: starting %s' % name)

                    # Set our process_oom_adjustment, as the parent process ALWAYS has it set to a very low value to avoid the supervisor from being killed:
                    set_process_oom_factor(process_oom_adjustment)

                    # Drop root privileges (has to be done after oom adjustment):
                    if 0 != process_drop_root_permissions(run_as_user, run_as_group):
                        supervisor_logger.error("Supervisor[%s/%s]: error setting user/group to %s/%s in child process." % (supervisor_pid, os.getpid(), run_as_user, run_as_group))
                        os._exit(1)

                    # We need to reset the signal handlers so as to NOT trap any signals because exec_func and init_func will have python code that runs within our current process.
                    # We have to unset this in the child process; if we set it in the "parent" branch of the if statement, then we'd be missing them on the next loop.
                    _remove_signal_functions()

                    # If there's an init function, run it instead:
                    if run_init_instead_of_exec:
                        set_proc_title('%s worker init' % name)
                        supervisor_logger.info("Supervisor[%s/%s]: starting init for %s" % (supervisor_pid, os.getpid(), name))
                        init_okay = True
                        ret_val = None
                        try:
                            ret_val = init_func()
                        except Exception as e:
                            supervisor_logger.error("Error in init function: %s; bailing." % e)
                            init_okay = False
                        if type(ret_val) is not int:
                            supervisor_logger.warn("Warning: init_func for %s returned non-int; please return 0 on success; non-zero otherwise; or throw an exception." % (name, ret_val))
                        else:
                            if ret_val != 0:
                                init_okay = False
                        if not init_okay:
                            supervisor_logger.error("Supervisor[%s/%s]: FATAL: init failed for %s" % (supervisor_pid, os.getpid(), name))
                            os.kill(supervisor_pid, signal.SIGTERM)
                        else:
                            supervisor_logger.info("Supervisor[%s/%s]: init finished for %s" % (supervisor_pid, os.getpid(), name))
                        os._exit(ret_val) # Exit child process; supervisor will pick
                        

                    # Run the exec function:
                    set_proc_title('%s worker' % name)
                    try:
                        exec_func() # This should be a function that calls os.exec and replaces our current process
                    except Exception as e:
                        supervisor_logger.error("Error in exec function: %s" % e)
                    supervisor_logger.error("MAJOR ERROR: Supervisor[%s/%s]: function for %s unexepectedly returned." % (supervisor_pid, os.getpid(), name))
                    os._exit(2)

            except Exception as e:
                supervisor_logger.error("Supervisor[%s/%s]: child process failed (%s)." % (supervisor_pid, os.getpid(), e))
                try:
                    _sleep_without_signal_functions(10)  # Sleep in child to prevent parent from rapidly re-spawning
                except:
                    pass
                continue
    
        if child_pid is None:
            supervisor_logger.error("Supervisor[%s/%s]: child process setup failed (supervisor_daemon_exit_requested: %s)." % (supervisor_pid, os.getpid(), supervisor_daemon_exit_requested))
            try:
                _sleep_without_signal_functions(10)  # Sleep in child to prevent parent from rapidly re-spawning
            except:
                supervisor_daemon_exit_requested = True
            continue

        # The parent process needs to wait for the child process to exit:
        wait_exitcode = _supervisor_daemon_waitpid(child_pid)
        set_proc_title('supervisor: managing %s[%s exited %s]' % (name, child_pid, wait_exitcode))

        if run_init_instead_of_exec:
            supervisor_logger.info("Supervisor[%s/%s]: init function finished." % (supervisor_pid, os.getpid()))
            child_pid = None
            continue

        if supervisor_daemon_exit_requested:
            set_proc_title('supervisor: managing %s[%s exited %s for exit]' % (name, child_pid, wait_exitcode))
            if one_time_run:
                supervisor_logger.info('Supervisor[%s/%s]: %s[%s] exited (exit code %s) for one-time run.' % (supervisor_pid, os.getpid(), name, child_pid, wait_exitcode))
            else:
                supervisor_logger.info('Supervisor[%s/%s]: %s[%s] exited (exit code %s) for shutdown.' % (supervisor_pid, os.getpid(), name, child_pid, wait_exitcode))
            break

        # The wait-for-child logic above may have returned early due to a signal that we received and passed off to child or otherwise handled.
        # Only reset stuff for a restart if the child process actually exited (i.e. waitpid() returned because the child exited, not because the parent received a signal):
        if not is_pid_running(child_pid):
            set_proc_title('supervisor: restarting %s' % (name))
            this_run_duration = time.time() - last_start_time

            # Re-try service starts no faster than some minimum interval, backing off to some maximum interval so lengthy outages don't trigger a sudden spike
            delay_until_next_restart = 0
            if continous_restarts > 0:
                delay_until_next_restart = min_delay_between_continous_restarts + (continous_restarts - 1) * 10 - this_run_duration + 2*random.random()
                if delay_until_next_restart < min_delay_between_continous_restarts:
                    delay_until_next_restart = min_delay_between_continous_restarts + 2*random.random()
                if delay_until_next_restart > max_delay_between_continous_restarts:
                    delay_until_next_restart = max_delay_between_continous_restarts - random.random() * restart_delay_jitter

            supervisor_logger.error('Supervisor[%s/%s]: %s[%s] unexpected exit (exit code %s) after %s seconds on run number %s, waiting %s seconds before restarting.' %
                                    (supervisor_pid, os.getpid(), name, child_pid, wait_exitcode, this_run_duration, start_count, delay_until_next_restart))
            supervisor_logger.error('Supervisor[%s/%s]: more info: run_init_instead_of_exec: %s; restart_daemon_on_exit: %s' %
                                    (supervisor_pid, os.getpid(), run_init_instead_of_exec, restart_daemon_on_exit))

            child_pid = None

            if this_run_duration < max_delay_between_continous_restarts:
                continous_restarts += 1
                try:
                    time_left = delay_until_next_restart
                    while time_left > 0:
                        # spit out a log every few seconds so we can see what's going on in the logs -- otherwise it looks wedged:
                        supervisor_logger.error('Supervisor[%s/%s]: %s[%s] waiting %s seconds.' % (supervisor_pid, os.getpid(), name, child_pid, int(time_left)))
                        sleep_time = 5
                        if sleep_time > time_left:
                            sleep_time = time_left
                        _sleep_without_signal_functions(sleep_time)
                        time_left -= sleep_time
                except Exception as e:
                    supervisor_logger.error('Supervisor[%s/%s]: %s had exception while waiting; bailing (%s).' % (supervisor_pid, os.getpid(), name, e))
                    supervisor_daemon_exit_requested = True
            else:
                continous_restarts = 0

    
    # We'll only exit above loop when supervisor_daemon_exit_requested is true.
    # We keep running until the child process exits, otherwise there's no way
    # for the outside world to send further signals to the process.
    while is_pid_running(child_pid):
        try:
            # While we can still send signals to the supervisor process, wait on it
            set_proc_title('supervisor: waiting for exit %s[%s]' % (name, child_pid))
            supervisor_logger.info("Supervisor[%s/%s]: waiting for exit %s[%s]" % (supervisor_pid, os.getpid(), name, child_pid))
            _supervisor_daemon_waitpid(child_pid)
        except OSError:
            pass

    set_proc_title('supervisor: finished monitoring %s[%s]; closing logfiles' % (name, child_pid))
    supervisor_logger.info("Supervisor[%s/%s] finished monitoring %s[%s]; exiting" % (supervisor_pid, os.getpid(), name, child_pid))

    if os.path.isfile(pid_filename_for_daemon):
        # The pid file really should exist, but if it doesn't, there's not a lot we can do anyway, and logging it wi
        os.remove(pid_filename_for_daemon)
    else:
        supervisor_logger.warn("Supervisor[%s/%s]: no lockfile at %s to remove, oh well." % (supervisor_pid, os.getpid(), pid_filename_for_daemon))

    # Stop logging threads:
    stdout_redirector.stopRedirectThread()
    stderr_redirector.stopRedirectThread()
    supervisor_redirector.stopRedirectThread()

    # Do not return from this function -- and use os._exit instead of sys.exit to nuke any stray threads:
    os._exit(0)


# For use by supervisor only -- please consider this 'private' to supervisor.
class SupervisorLogger():
    # Yes, re-inventing the wheel here. Trying to keep the external dependencies down to a minimum.
    logger_fd = None
    def __init__(self, logger_fd):
        self.logger_fd = logger_fd
    def info(self, message):
        self.log('info', message)
    def warn(self, message):
        self.log('warn', message)
    def error(self, message):
        self.log('error', message)
    def log(self, level, message):
        self.logger_fd.write("%s, %s, %s\n" % (time.time(), level, message))
        self.logger_fd.flush()


# For use by supervisor only -- please consider this 'private' to supervisor.
from threading import Thread
class SupervisorStreamRedirector(Thread):

    supervisor_logger = None
    log_data_source = None
    stop_event = None

    run_as_user = None
    run_as_group = None
    logfile_inode = None
    logfile_dir = None
    logfile_path = None
    logfile_fd = None


    def __init__(self, supervisor_logger, logfile_path, run_as_user=None, run_as_group=None):
        Thread.__init__(self)
        self.supervisor_logger = supervisor_logger
        self.logfile_path = logfile_path
        self.logfile_dir = os.path.dirname(self.logfile_path)
        self.run_as_user = run_as_user
        self.run_as_group = run_as_group
        self._create_logdir()


    def startRedirectThread(self, data_stream):
        if self.stop_event:
            if self.supervisor_logger is not None:
                self.supervisor_logger.warn("SupervisorStreamRedirector: redirect already started?")
            return -4
        self.stop_event = threading.Event()

        try:
            reader, writer = os.pipe()
            self.log_data_source = os.fdopen(reader, 'rb', 0)
            original_output_dest = os.fdopen(writer, 'wb', 0)

            # Flip on non-blocking, otherwise calls to select.select() will block:
            flags = fcntl.fcntl(original_output_dest, fcntl.F_GETFL)
            fcntl.fcntl(original_output_dest, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            flags = fcntl.fcntl(self.log_data_source, fcntl.F_GETFL)
            fcntl.fcntl(self.log_data_source, fcntl.F_SETFL, flags | os.O_NONBLOCK)

            data_stream.flush()
            os.dup2(original_output_dest.fileno(), data_stream.fileno())

        except Exception as e:
            if self.supervisor_logger is not None:
                self.supervisor_logger.warn("SupervisorStreamRedirector: error setting up file streams for redirect: %s" % e)
            return -5

        try:
            self.start()
        except Exception as e:
            if self.supervisor_logger is not None:
                self.supervisor_logger.warn("SupervisorStreamRedirector: error starting redirect thread: %s" % e)
            return -6

        return 0


    def stopRedirectThread(self):
        if self.stop_event:
            self.stop_event.set()
        else:
            if self.supervisor_logger is not None:
                self.supervisor_logger.warn("SupervisorStreamRedirector: stop_logger not running? (%s)" % self.stop_event)


    def _filter_lines(self, lines):
        ''' Given an array of lines, return a filtered / altered string as desired. '''
        # The intent here is to someday pass an object in that implements the filter, so that
        # sensative strings can be filtered out of the log files before getting written to disk
        # and then sent across the wire via logstash or what have you.
        # For now, we do a no-op.
        if len(lines) == 0:
            return ''
        return '\n'.join(lines) + '\n'

        # Here's an example that would timestamp every line:
        #if len(lines) == 0:
        #    return ''
        #line_beginning = '%11.1f  ' % (time.time())
        #line_ending = '\n'
        #return line_beginning + (line_ending + line_beginning).join(lines) + line_ending


    def _create_logdir(self):
        if 0 != create_dirs_if_needed(self.logfile_dir, owner_user=self.run_as_user, owner_group=self.run_as_group):
            self.supervisor_logger.error("SupervisorStreamRedirector[%s]: unable to create logdir %s" % (os.getpid(), self.logfile_path))
            return -7
        return 0


    def _reset_logfile(self):
        if 0 != self._create_logdir():
            return -8
        try:
            if os.path.exists(self.logfile_path):
                if os.path.islink(self.logfile_path) or not os.path.isfile(self.logfile_path):
                    self.supervisor_logger.error("SupervisorStreamRedirector: invalid file at logfile path %s" % self.logfile_path)
                    return -9
            new_fh = open(self.logfile_path, 'a')
            if self.logfile_fd is not None:
                self.logfile_fd.close()
            self.logfile_fd = new_fh
            self.logfile_inode = os.stat(self.logfile_path).st_ino
            self.supervisor_logger.info("SupervisorStreamRedirector[%s]: writing to logfile %s" % (os.getpid(), self.logfile_path))
        except Exception as e:
            self.supervisor_logger.error("SupervisorStreamRedirector[%s]: unable to open logfile %s: %s" % (os.getpid(), self.logfile_path, e))
            return -10
        return 0


    def run(self):
        okay_to_run = True
        last_read_size = 0
        last_remainder = ''
        while okay_to_run or last_read_size > 0:

            if self.stop_event.is_set():
                self.supervisor_logger.info("SupervisorStreamRedirector[%s]: stopping logger at %s" % (os.getpid(), self.logfile_path))
                okay_to_run = False
                self.stop_event.clear()

            try:
                # Don't use readline() -- it blocks, and there's no way for the main thread 
                # to tell the logger thread to exit while the i/o call is blocked. Sigh.
                [rlist, wlist, xlist] = select.select([self.log_data_source], [], [], 0.25)

                if not os.path.exists(self.logfile_dir):
                    # Re-create the logdir if it goes missing -- do this check every pass through,
                    # so that if logdir gets completely reset we instantly recreate the path for other
                    # processes which might also depend on it.
                    self._create_logdir()

                if not rlist:
                    last_read_size = 0
                else:
                    data = self.log_data_source.read(1024)
                    last_read_size = len(data)

                    # We split the data into lines so that we can filter sensative stings out, and potentially do some line-based formatting.
                    # Because we're not using readline (due to blocking reasons), we have to split the data into lines, and carry over the remainder
                    # of the last line (if it's mid-line) to the next pass through the loop.
                    lines = data.split('\n')
                    if data.endswith('\n'):
                        lines = lines[:-1]
                    if len(last_remainder):
                        lines[0] = last_remainder + lines[0]
                        last_remainder = ''
                    if not data.endswith('\n'):
                        last_remainder = lines[-1]
                        lines = lines[:-1]

                    try:
                        current_inode = os.stat(self.logfile_path).st_ino
                        if self.logfile_inode != current_inode:
                            self._reset_logfile()
                    except:
                        self._reset_logfile()

                    if self.logfile_fd is not None:
                        self.logfile_fd.write(self._filter_lines(lines))
                        if not okay_to_run and len(last_remainder):
                            # Then it's our last loop through -- purge out the remainder:
                            self.logfile_fd.write(self._filter_lines(last_remainder,))
                            last_remainder = ''
                        self.logfile_fd.flush()


            except Exception as e:
                self.supervisor_logger.error("SupervisorStreamRedirector: error in log thread: %s" % e)

        self.supervisor_logger.info("SupervisorStreamRedirector stopping; closing %s." % self.logfile_path)
        self.logfile_fd.flush()
        self.logfile_fd.close()
        self.stop_event = None
