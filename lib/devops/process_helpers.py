
# Launcher is complicated, and used in many ways. Don't just edit this trivially... there are some pretty subtle semantics going on here.

import grp
import pwd
import os
import signal
import subprocess
import sys
import threading
import time
import traceback

from devops.file_and_dir_helpers import *
from angel.util.pidfile import *


def run_function_in_background(config, name, pid_filename_for_daemon, func, log_basepath=None, run_as_user=None, run_as_group=None):
    ''' Run a python function in a new process in the background, optionally setting the user, redirecting stdout/stderr, and storing the pid in given pid filename.
        Returns 0 for successful start of background job, non-zero otherwise. '''

    def run_and_exit():
        # For convenience, wrap the function in a handler that calls os._exit.
        # (Don't use sys.exit; that generates a SystemExit exception that triggers stacktraces.)
        # This is because functions run via 'launch_via_function' are never supposed to return,
        # but users calling this function shouldn't have to deal with that.
        try:
            ret_val = func()
            if ret_val is None:
                ret_val = 0
            os._exit(ret_val)
        except Exception as e:
            print >>sys.stderr, "Unexpected exception in run_and_exit (%s)." % e
            os._exit(5)

    return launch_via_function(config, name, log_basepath, run_and_exit, run_as_daemon=True,
                               run_as_user=run_as_user, run_as_group=run_as_group, pid_filename_for_daemon=pid_filename_for_daemon,
                               restart_daemon_on_exit=False)



def launch(config, command, args=None, env=None, reset_env=False, log_basepath=None,
           run_as_daemon=False, restart_daemon_on_exit=True, run_as_user=None, run_as_group=None, pid_filename_for_daemon=None, 
           foreground_exec_mode=False, init_func=None, stop_func=None, nice_value=0, chdir_path=None, name=None, show_exec_info=True, process_oom_adjustment=0):
    ''' Runs the given command and args with various process options (redirection, chdir, nice, etc.)
        When run_as_daemon is true, returns 0 on successful daemonization and non-zero on error.
        When run_as_daemon is false, returns exit code of process.
        When env is given, it completely replaces the current env.
        Never returns when foreground_exec_mode is true. 
        init_func() is called one time before running the command; this allows us to pass startup logic to a child process.
        stop_func(server_pid) is called when we run in daemon mode and receive SIGTERM and given the process id of the server process.
    '''

    # Generate a descriptive name for showing in ps / top:
    if name is None:
        name = os.path.basename(command)
        if args is not None:
            name += ' ' + ' '.join(map(str,args))
    if name[:19] == '/usr/bin/python -O ': name = name[19:]
    if name.find('site.py ') > 0:
        name = name[name.find('site.py '):]
    if len(name) > 36:
        name = name[:12] + '...' + name[-20:]

    if command is None:
        print >>sys.stderr, "Error: binary missing; args: %s" % args
        return -1

    if not os.path.exists(command):
        print >>sys.stderr, "Error: binary missing; can't find '%s'." % command
        return -2

    if run_as_daemon and foreground_exec_mode:
        print >>sys.stderr, "Error: can't running %s as both a daemon and foregrounded." % command
        return -3

    def exec_func():
        if show_exec_info:
            print >>sys.stderr, "*Execing: %s" % command
            print >>sys.stderr, "    args: %s" % ' '.join(map(str,args))
            print >>sys.stderr, "     pid: %s" % os.getpid()
            print >>sys.stderr, " uid/gid: %s/%s" % (os.getuid(), os.getgid())
            print >>sys.stderr, "    path: %s" % os.environ['PATH']
            if chdir_path:
                print >>sys.stderr, "   chdir: %s" % chdir_path
            else:
                print >>sys.stderr, "     pwd: %s" % os.getcwd()
            if reset_env:
                print >>sys.stderr, "   env: RESET"
            if env:
                line_preamble="     env: "
                for var in sorted(env):
                    print >>sys.stderr, '%s%s="%s"' % (line_preamble, var, env[var])
                    line_preamble="          "
        ret_val = exec_process(command, args=args, nice_value=nice_value, env=env, reset_env=reset_env, chdir_path=chdir_path)
        print >>sys.stderr, "Error: exec_func() is about to return in pid %s, this shouldn't happen (reason %s)." % (os.getpid(), ret_val)
        
    if foreground_exec_mode:
        if launcher_open_logs_and_prep_env(config, log_basepath, run_as_user, run_as_group):
            print >>sys.stderr, "Unable to set up environment correctly, refusing to run process %s." % command
            sys.exit(1)
        if init_func is not None:
            try:
                ret_val = init_func()  # Then we run the init followed by the exec -- not a common case, but support it for completeness
                if ret_val is None or int(ret_val) != 0:
                    print >>sys.stderr, "Warning: init_func for %s returned %s in foreground_exec_mode; calling exec_func anyway" % (name, ret_val)
            except Exception as e:
                print >>sys.stderr, "Init function through exception %s." % str(e)
                print e
                sys.exit(1)

        if not restart_daemon_on_exit:
            exec_func()

        while True:
            sys.stdout.flush()
            sys.stderr.flush()
            child_pid = os.fork()
            if 0 == child_pid:
                exec_func()
                print >>sys.stderr, "Error: exec_func returned! Exiting 1 now."
                os._exit(1)  # Use os._exit to avoid any finally clauses from parent process being triggered
            else:
               try:
                   os.wait()
               except KeyboardInterrupt:
                   print >>sys.stderr, "Killed by ctrl-c; sending process %s SIGTERM and returning." % child_pid
                   os.kill(child_pid, signal.SIGTERM)
                   wait_pid, status = os.waitpid(child_pid, os.WNOHANG)
                   return
               print >>sys.stderr, "Error: child process %s exited; restarting in 5 seconds." % pid
               try:
                   time.sleep(5)
               except KeyboardInterrupt:
                   print >>sys.stderr, "Killed by ctrl-c (pid %s); returning." % pid
                   return

        print >>sys.stderr, "Impossible condition reached: foreground_exec_mode continued after exec_func; calling sys.exit(1)"
        sys.exit(1)

    return launch_via_function(config, name, log_basepath, exec_func,
                run_as_daemon=run_as_daemon, restart_daemon_on_exit=restart_daemon_on_exit,
                run_as_user=run_as_user, run_as_group=run_as_group, pid_filename_for_daemon=pid_filename_for_daemon,
                init_func=init_func, stop_func=stop_func, process_oom_adjustment=process_oom_adjustment)



def launch_via_function(config, name, log_basepath, exec_func, restart_daemon_on_exit=True,
                        run_as_daemon=False, run_as_user=None, run_as_group=None, pid_filename_for_daemon=None,
                        init_func=None, stop_func=None, process_oom_adjustment=0):
    ''' Runs a function in a child python process, optionally setting various process settings.
        In non-exec mode, returns non-zero on daemon launch errors and exit code of process when run in foreground. 
        In exec-mode, never returns.  '''

    if pid_filename_for_daemon is not None:
        if is_pid_in_pidfile_running(pid_filename_for_daemon):
            print  >>sys.stderr, "Process '%s' already running; active pid in file '%s'." %\
                                 (name, pid_filename_for_daemon)
            return -1

    if run_as_daemon and pid_filename_for_daemon is None:
        # Don't allow daemons to run without tracking them -- otherwise no way to stop them!
        print >>sys.stderr, "Error: %s set to run as daemon, but no daemon pidfile given." % name
        return -2

    # We have to fork a child process to correctly set the user id of the process while retaining root privileges in angel control script.
    # We also need to adjust stdin/stdout/stderr in the child process, otherwise we'll screw with the hsn script's filestreams.
    # We'd need to fork for run_as_daemon, too -- it's less code to always fork and then if non-daemon, simply wait for the child process.

    sys.stdout.flush()
    sys.stderr.flush()
    child_pid = os.fork()
    if child_pid:
        # Parent process...
        if run_as_daemon:
            # Wait for the pidfile to be created, for consistency reasons:
            wait_time = 10
            while wait_time > 0:
                if os.path.exists(pid_filename_for_daemon):
                    return 0
                try:
                    wait_pid, status = os.waitpid(child_pid, os.WNOHANG)
                    os.kill(child_pid, 0)
                except:
                    # This can happen for very short-lived background processes -- the child creates the pidfile and then exits and deletes it before the parent even gets here.
                    return 0
                try:
                    time.sleep(0.25)
                    wait_time -= 0.25
                except:
                    print >>sys.stderr, "Killed while waiting for pidfile %s to appear." % pid_filename_for_daemon
                    return -3
            print >>sys.stderr, "Error: pid lockfile failed to appear after 10 seconds (pid: %s, path: %s)." % (child_pid, pid_filename_for_daemon)
            return -4

        # Parent process, non-daemon mode: wait for child process to return an exit code:
        exit_code = -5
        try:
            wait_pid, status = os.waitpid(child_pid, 0)
            exit_code = (status >> 8) % 256  # Exit codes are 8 bit values; status is 8 bits of exit code (higher byte) and 8 bits of signal (lower byte)
        except OSError:
            print >>sys.stderr, "Warning: os.waitpid(" + str(child_pid) + ") canceled."
            return -6
        if wait_pid != child_pid:
            print >>sys.stderr, "Warning: os.waitpid(" + str(child_pid) + ") failed."
            return -7
        if exit_code and log_basepath != os.devnull:
            print >>sys.stderr, "Warning: %s exited non-zero (%s)." % (name, exit_code)
        return exit_code

    else:
        # Child process..

        if not run_as_daemon:
            # Set user/group for running process and redirect stdout/stderr:
            if launcher_open_logs_and_prep_env(config, log_basepath, run_as_user, run_as_group):
                print >>sys.stderr, "Unable to set up environment correctly, refusing to run process %s." % name
                os._exit(1)

            # Run the given init and exec functions. This is expected to never return; it'd normally call exec; and our parent process will have returned in above fork:
            if process_oom_adjustment != 0:
                set_process_oom_factor(process_oom_adjustment)
            if init_func is not None:
                ret_val = init_func()
                if ret_val is None or int(ret_val) != 0:
                    print >>sys.stderr, "Warning: init_func for %s returned %s in child-non-daemon case; calling exec_func anyway" % (name, ret_val)
            exec_func()
            print >>sys.stderr, "launcher: function unexpectedly returned in pid %s during non-daemon run; calling sys.exit(1)" % (os.getpid())
            os._exit(1)

        # If we get here, then we are in the child process and ready to run our functions in a daemon process:

        # Write our process ID out into a lockfile:
        if 0 != write_pidfile(pid_filename_for_daemon, os.getpid(), owner_user=run_as_user, owner_group=run_as_group):
            print >>sys.stderr, "Warning: could not write pid %s to pidfile %s for %s; won't be able to correctly stop the process later." % (os.getpid(), pid_filename_for_daemon, name)

        # Daemon mode requires having log files; we can't use STDOUT/STDERR:
        if log_basepath is None:
            print >>sys.stderr, "Warning: no log path given for %s output in process %s; sending it to /dev/null" % (name, os.getpid())
            log_basepath = os.devnull

        from devops.supervisor import supervisor_manage_process
        supervisor_manage_process(config, name, pid_filename_for_daemon, run_as_user, run_as_group, log_basepath,
                                  restart_daemon_on_exit, process_oom_adjustment, init_func, exec_func, stop_func)
        print >>sys.stderr, "launcher: supervisor unexpectedly returned in pid %s during daemon mode; calling sys.exit(1)" % (os.getpid())
        os._exit(1)


def launcher_prep_logpath(config, log_basepath, run_as_user, run_as_group):
    dummy_path = launcher_get_logpath(config, log_basepath, 'fake')
    if log_basepath == os.devnull:
        return 0
    log_dir = os.path.dirname(os.path.abspath(dummy_path))
    if 0 != create_dirs_if_needed(log_dir, owner_user=run_as_user, owner_group=run_as_group):
        return -1
    return 0


def launcher_get_logpath(config, log_basepath, log_type, run_as_user=None, run_as_group=None):
    ''' Return the full path to a logfile (/dev/null when log_basepath is None), making sure that the necessary log dirs
        exist and are set up correctly.
        Returns None if there are permission problems with the log path.
          log_basepath:  relative or absolute base path
          log_type: should be "out" or "err", used in returned path
    '''
    if log_basepath is None or log_basepath == os.devnull:
        return os.devnull
    if len(log_type):
        path = '%s-%s.log' % (log_basepath, log_type)
    else:
        path = '%s.log' % log_basepath
    if '/' != path[0]:
        path = os.path.join(os.path.expanduser(config['LOG_DIR']), path)
    return os.path.normpath(path)


def set_process_oom_factor(oom_adj):
    ''' Set the current process's oom adjustment,
        which controls how likely the process is to be killed during out-of-memory conditions. '''
    if oom_adj < -15:
        print >>sys.stderr, "Warning: oom factor %s too low; setting to -15." % oom_adj
        oom_adj = -15
    oom_score_adj = oom_adj * 50 # Older kernels used values down to -1000; we're being fuzzy here but close enough
    try:
        if os.path.isfile('/proc/self/oom_score_adj'):  # Older linux kernels
            open('/proc/self/oom_score_adj', 'w').write(str(oom_score_adj))
        if os.path.isfile('/proc/self/oom_adj'):  # Newer linux kernels
            open('/proc/self/oom_adj', 'w').write(str(oom_adj))
    except Exception as e:
        print >>sys.stderr, "Warning: unable to adjust OOM value in process %s (%s)." % (os.getpid(), e)
    

def process_drop_root_permissions(run_as_user, run_as_group):
    # If root, set the effective user/group for the running process if user/group is not None (silently ignored when None -- be careful).
    # If non-root, check that the current user is equal to the target user.
    # Returns 0 on success; non-zero otherwise
    if run_as_user is None:
        return 0

    if 0 == os.getuid():
        if run_as_group is not None:
            try:
                os.setgid(grp.getgrnam(run_as_group).gr_gid)
            except KeyError:
                print >>sys.stderr, "Error: unable to find group '%s'; does that group exist?" % run_as_group
                return -1

        if run_as_user is not None:
            try:
                os.setuid(pwd.getpwnam(run_as_user).pw_uid)
            except KeyError:
                print >>sys.stderr, "Error: unable to set user to '%s'; does that username exist?" % run_as_user
                return -2
        return 0

    if os.getuid() != pwd.getpwnam(run_as_user).pw_uid:
        print >>sys.stderr, "Error: can't switch to user %s, group %s; try running with sudo or check your config." % (run_as_user, run_as_group)
        return -3

    try:
        os.listdir('.')
    except:
        print >>sys.stderr, "Warning: current working directory '%s' isn't accessible to user %s; switching cwd to /." % (os.getcwd(), run_as_user)
        os.chdir('/')

    return 0


def launcher_open_logs_and_prep_env(config, log_basepath, run_as_user, run_as_group):

    if log_basepath:
        # Create log dirs before dropping privileges:
        if 0 != launcher_prep_logpath(config, log_basepath, run_as_user, run_as_group):
            return -1

    if 0 != process_drop_root_permissions(run_as_user, run_as_group):
        return -2

    if log_basepath:
        # Reset STDOUT and STDERR to use log files under log_basepath:
        new_stdout = open(launcher_get_logpath(config, log_basepath, 'out', run_as_user=run_as_user, run_as_group=run_as_group), 'at', 0) # Unbuffered output to avoid annoying "where's my log?" confusion
        new_stderr = open(launcher_get_logpath(config, log_basepath, 'err', run_as_user=run_as_user, run_as_group=run_as_group), 'at', 0)
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(new_stdout.fileno(), sys.stdout.fileno())
        os.dup2(new_stderr.fileno(), sys.stderr.fileno())

    return 0


def run_command(command, args=None, chdir=None, timeout_in_seconds=None, tee_output=False, ignore_ctrl_c=False,
                run_as_user=None, run_as_group=None, env=None, print_error_info=False):
    ''' Run the given command and return the exit code. '''
    return get_command_output(command, args=args, chdir=chdir, timeout_in_seconds=timeout_in_seconds,
                              tee_output=tee_output, ignore_ctrl_c=ignore_ctrl_c,
                              run_as_user=run_as_user, run_as_group=run_as_group,
                              env=env, print_error_info=print_error_info)[2]


def get_command_output(command, args=None, chdir=None, timeout_in_seconds=None, tee_output=False, ignore_ctrl_c=False,
                       run_as_user=None, run_as_group=None, env=None, reset_env=False,
                       print_error_info=False, stdin_string=None):
    ''' Given a command and a list of args, return a tuple of STDOUT, STDERR, and the exit code.
        Command can alternatively be a string with the entire command, in which case it will be run in a sub-shell.
        Runs command in a shell if no args given and the command has spaces in it.
        Any values given in env are added to the current env, unless reset_env is set to True.
        tee_output, when True, will cause the stdout / stderr of the command to be written to our stdout/stderr as well as returned.
        Note that due to pipe buffering, the output won't be line-flushed like you'd expect in a TTY.
        If stdin_string is not None, the value of it will be passed into the command as STDIN.
    '''
    if command is None:
        print >>sys.stderr, "Error: missing command (args=%s, env=%s)." % (args, env)
        return None, None, -1

    try:
        # Convert all args/env to strings:
        if args is None:
            args = []
        args = map(str, args)

        if reset_env:
            exec_env = {}
        else:
            exec_env = dict(os.environ)
        if env:
            for k in env:
                exec_env[str(k)] = str(env[k])
    except Exception as e:
        print >>sys.stderr, "Error: unable to cast args or env correctly for command %s: %s (args=%s, env=%s)" % (command, e, args, env)
        return None, None, -1

    output = None
    output_stderr = None
    exit_code = -1
    old_cwd = None
    old_sigchld = None
    old_sigint = None

    try:
        # Chdir if requested:
        if chdir:
            try:
                old_cwd = os.getcwd()
            except Exception as e:
                # Yes, I've seen this fail. Don't ask.
                print >>sys.stderr, "Error: unable to get current working directory (are you inside a directory that got deleted?)."
                return None, None, -2

            try:
                os.chdir(os.path.expanduser(chdir))
            except Exception as e:
                print >>sys.stderr, "Error: unable to chdir to %s: %s" % (chdir, e) # in case dir doesn't exist
                return None, None, -3

        # If no args and spaces in command, flip on shell mode:
        use_shell = False
        if 0 == len(args) and command.find(' ') > 0:
            use_shell = True

        # Temporarily remove any SIGCHLD handler (subprocess uses it internally and it conflicts when dpkg installs use it too; FYI might be fixed in python 2.7):
        old_sigchld = signal.signal(signal.SIGCHLD, signal.SIG_DFL)

        # If ignore_ctrl_c, then prevent Ctrl-C from killing this:
        def ignore_handler(signum, frame):
            print >>sys.stderr, "\nCan't cancel command '%s'." % command
        if ignore_ctrl_c:
            old_sigint = signal.signal(signal.SIGINT, ignore_handler)

        # Set up user/group info:
        run_as_uid = os.getuid()
        run_as_gid = os.getgid()
        try:
            if run_as_user is not None:
                run_as_uid = pwd.getpwnam(run_as_user).pw_uid
            if run_as_group is not None:
                run_as_gid = grp.getgrnam(run_as_group).gr_gid
        except:
            print >>sys.stderr, "Error: can't run as user/group %s/%s (missing user/group)" %  (run_as_user, run_as_group)
            return None, None, -4
        if os.getuid() != 0 and run_as_uid != os.getuid():
            print >>sys.stderr, "Error: can't run as user/group %s/%s (insufficient privileges; try sudo?)" %  (run_as_user, run_as_group)
            return None, None, -5

        # Run command and capture its stdout/stderr using a thread, so that we can support timeouts:
        bufsize = 0
        if tee_output:
            bufsize = 1
        p = subprocess.Popen([command] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             shell=use_shell, bufsize=bufsize, env=exec_env,
                             preexec_fn=lambda: [os.setgid(run_as_gid), os.setuid(run_as_uid)])

        def manage_subprocess():
            input = None
            if stdin_string is not None:
                input = str(stdin_string)
            output = ''
            output_stderr = ''
            while p.returncode is None:
                this_output, this_output_stderr, = p.communicate(input=input)
                input=None
                # Use p.communicate, not p.wait -- see the note about deadlocks in the python docs
                output += this_output
                output_stderr += this_output_stderr
                # FYI, line-buffering won't work unless sub-process calls flush() as it writes,
                # so tee_output isn't very useful for long-running processes.
                if tee_output:
                    print >>sys.stdout, this_output,
                    print >>sys.stderr, this_output_stderr,
                sys.stderr.flush()
                sys.stdout.flush()
            thread.subprocess_output = output
            thread.subprocess_output_stderr = output_stderr
            thread.subprocess_exit_code = p.returncode

        thread = threading.Thread(target=manage_subprocess)
        thread.start()
        try:
            thread.join(timeout_in_seconds)
        except KeyboardInterrupt:
            print >>sys.stderr, "Ctrl-c; aborting process."
            
        if thread.is_alive():
            print >>sys.stderr, "Error: timeout after %s seconds, or ctrl-c received, killing process %s." % (timeout_in_seconds, command)
            p.terminate()
            if p.poll() is None:
                time.sleep(0.5)
                if p.poll() is None:
                    p.kill()
            thread.join()
        else:
            output = thread.subprocess_output
            output_stderr = thread.subprocess_output_stderr
            exit_code = thread.subprocess_exit_code            

        if print_error_info and 0 != exit_code:
            print >>sys.stderr, "Error %s running command:" % exit_code
            print >>sys.stderr, "   command=%s" % command
            if args is not None:
                print >>sys.stderr, "   args=%s" % ' '.join(map(str,args))
            if chdir is not None:
                print >>sys.stderr, "   chdir=%s" % chdir
            if run_as_user is not None or run_as_group is not None:
                print >>sys.stderr, "   run as=%s/%s" % (run_as_user, run_as_group)
            if ignore_ctrl_c is True:
                print >>sys.stderr, "   ignore_ctrl_c=True"
            if env is not None:
                sorted_env = ""
                for k in sorted(exec_env):
                    v = exec_env[k]
                    if k == 'LS_COLORS':
                        v = v[0:12] + "..."
                    sorted_env += '%s=%s  ' % (k, v)
                print >>sys.stderr, "   env=%s" % sorted_env
            if output_stderr:
                print >>sys.stderr, "   err=%s" % output_stderr.rstrip()
            if output:
                print >>sys.stderr, "   out=%s" % output.rstrip()

        return output, output_stderr, exit_code

    except Exception as e:
        #traceback.print_exc()
        return None, "Exception %s ('%s') when running command %s %s (command stdout=%s; command stderr=%s)." %\
                     (type(e), e, command, ' '.join(map(str,args)), output, output_stderr), -6

    finally:
        if old_cwd:
            try:
                os.chdir(old_cwd)
            except Exception as e:
                print >>sys.stderr, "Error: unable to chdir to original directory %s (%s); using '/' instead." % (old_cwd, e)
                os.chdir('/')

        if old_sigchld:
            signal.signal(signal.SIGCHLD, old_sigchld)
            if old_sigint is not None:
                signal.signal(signal.SIGINT, old_sigint)


def exec_process(command, args=None, env=None, reset_env=False, nice_value=None, chdir_path=None,
                 run_as_user=None, run_as_group=None, stdin_string=None, run_as_child_and_block=False,
                 stdout_fileno=None):
    ''' Exec a process with optional args and env.
        If command contains any spaces, the command will be wrapped in a bash shell.
        If reset_env is true, then the ENV settings will be entirely replaced (potentially with none, if env is not given.)
        nice_value = unix nice value to run the process with; generally between 0 and 19.
        chdir_path = path to cd to before running process.
        If stdin_string is not None, the stdin of the exec'ed process will be mapped to a stream containing stdin_string.
        If run_as_child_and_block is true, fork and in the parent process, block and return the exit code.
        If stdout_fileno is given, redirect the stdout of the given process to the given file descriptor (must be an already-open fd)
    '''

    if run_as_child_and_block:
        child_pid = os.fork()
        if child_pid:
            def _supervisor_daemon_signal_passthru(signum, frame):
                try:
                    os.kill(child_pid, signum)
                except Exception as e:
                    print >>sys.stderr, "Error: sending signal %s to child pid %s failed: %s" % (signum, child_pid, e)
            trappable_signals = (signal.SIGINT, signal.SIGWINCH, signal.SIGHUP, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2, signal.SIGQUIT)
            old_handlers = {}
            for sig in trappable_signals:
                old_handlers[sig] = signal.signal(sig, _supervisor_daemon_signal_passthru)
            def _is_child_alive():
                try:
                    os.kill(child_pid, 0)
                    return True
                except:
                    return False
            try:
                while _is_child_alive():
                    try:
                        wait_pid, status = os.waitpid(child_pid, 0)
                        exit_code = (status >> 8) % 256  # Exit codes are 8 bit values; status is 8 bits of exit code (higher byte) and 8 bits of signal (lower byte)
                        return exit_code
                    except KeyboardInterrupt:
                        print >>sys.stderr, "Ctrl-C; sending SIGTERM to %s and returning" % child_pid
                        os.kill(child_pid, signal.SIGTERM)
                        wait_pid, status = os.waitpid(child_pid, os.WNOHANG)  # Reap child process
                        return -1
                    except Exception as e:
                        print >>sys.stderr, "Exception while exec_process waited for %s to exit (%s); ignoring." % (child_pid, e)
            finally:
                for sig in trappable_signals:
                    signal.signal(sig, old_handlers[sig])
                
        else:
            exit_code = exec_process(command, args=args, env=env, reset_env=reset_env, nice_value=nice_value,
                                     chdir_path=chdir_path, run_as_user=run_as_user, run_as_group=run_as_group,
                                     stdin_string=stdin_string, run_as_child_and_block=False,
                                     stdout_fileno=stdout_fileno)
            os._exit(exit_code)

    if command is None:
        print >>sys.stderr, "Error: missing binary name (args: %s)" % ' '.join(map(str,args))
        return -1

    if args is None:
        args = ()
        if command.find(' ') > 0:
            args = ('-c', command)
            command = '/bin/bash'
    else:
        if isinstance(args, str):
            print >>sys.stderr, "Warning: args given as string ('%s'), not list." % args

    if not os.path.exists(command):
        print >>sys.stderr, "Error: binary missing at '%s'." % command
        return -2

    # Make sure all ints / floats are cast to strings:
    args = map(str, args)
    if reset_env and env is None:
        env = {}
    if env is not None:
        str_env = {}
        for key in env:
            str_env[str(key)] = str(env[key])
        env = str_env
        if not reset_env:
            for key in os.environ:
                if key not in env:
                    env[key] = os.environ[key]

    # Set nice value if possible (ignores errors):
    if nice_value is not None:
        try:
            os.nice(nice_value)
        except:
            print >>sys.stderr, "Warning: Pid %s unable to set nice value to %s; ignoring." % (os.getpid(), nice_value)

    # chdir to requested path, or hard fail:
    if chdir_path is not None:
        try:
            os.chdir(os.path.expanduser((chdir_path)))
        except:
            print >>sys.stderr, "Error: Pid %s unable to chdir to %s!" % (os.getpid(), chdir_path)
            return -4

    # Drop root permissions, if requested:
    if run_as_user is not None or run_as_group is not None:
        if 0 != process_drop_root_permissions(run_as_user, run_as_group):
            return -3

    # Make sure the current working directory exists:
    try:
        os.getcwd()
    except Exception as e:
        print >>sys.stderr, "Error: current working directory doesn't resolve correctly (%s)." % e
        return -5

    # Flush STDOUT and STDERR -- python doesn't always flush these before os.execvp is called;
    # so if we printed anything before calling exec, it can get lost.
    sys.stdout.flush()
    sys.stderr.flush()

    # Set up STDIN, if given:
    if stdin_string is not None:
        import tempfile
        t = tempfile.TemporaryFile()
        t.write(stdin_string)
        t.seek(0)
        sys.stdin = t
        os.dup2(sys.stdin.fileno(), 0)

    # Set up STDOUT redirect, if given:
    if stdout_fileno is not None:
        os.dup2(stdout_fileno, sys.stdout.fileno())

    # Call exec:
    if env is None:
        try:
            os.execv(command, [ command ] + args)
        except Exception as e:
            print >>sys.stderr, "Error: in pid %s, os.execv(%s) failed: %s" % (os.getpid(), command, e)
    else:
        try:
            os.execve(command, [ command ] + args, env)
        except Exception as e:
            print >>sys.stderr, "Error: in pid %s, os.execve(%s) failed: %s" % (os.getpid(), command, e)
    return -6
