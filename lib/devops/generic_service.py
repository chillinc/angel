import base64
import datetime
import fnmatch
import inspect
import os
import pwd
import re
import shutil
import signal
import socket
import stat
import string
import sys
import time
import urllib2

import angel
import angel.settings
import angel.util.checksum

from devops.stats import *
from devops.file_and_dir_helpers import create_dirs_if_needed, set_file_owner
from angel.util.pidfile import is_pid_running, is_pid_in_pidfile_running, get_pid_from_pidfile, release_pidfile, read_data_from_lockfile, write_pidfile
from devops.process_helpers import launch, launch_via_function, get_command_output, exec_process, run_function_in_background
from devops.unix_helpers import get_pid_relationships, kill_and_wait, hard_kill_all, get_all_children_of_process

class GenericService(object):

    # Number of seconds service is allowed to take to start:
    # (This controls how long status shows 'starting' as well as minimum length of time a process must be running before 'repair' will do anything.)
    ALLOWED_STARTUP_TIME_SECS = 300 

    # Number of seconds we wait when stop is called before returning an error:
    ALLOWED_STOP_TIME_SECS = 600 

    # Unix signals and timeouts uses for stopping the service:
    STOP_SOFT_KILL_SIGNAL = signal.SIGHUP
    STOP_SOFT_KILL_TIMEOUT = 30
    STOP_HARD_KILL_SIGNAL = signal.SIGTERM
    STOP_HARD_KILL_TIMEOUT = 30

    # How much disk space do we expect this service to need? If the disk partition that we use is less than this, then status will issue warnings.
    MIN_OK_DATA_DIR_DISK_SIZE_IN_MB = 0 # Override as needed in subclasses

    # How much free disk space do we require on the DATA_DIR partition, as a percentage? If we go over this, then status will issue warnings.
    MAX_OK_DATA_DIR_DISK_USAGE = 0.8

    # Thresholds for load monitoring:
    MONITORING_LOAD1_THRESHOLD = 4.0
    MONITORING_LOAD5_THRESHOLD = 1.0
    MONITORING_LOAD15_THRESHOLD = 0.7
    MONITORING_SHORTTERM_SPIKE_GRACE = 1.0  # Add this much to the threshold for SPIKE_TIME seconds
    MONITORING_SHORTTERM_SPIKE_TIME = 300  # In seconds

    SUPERVISOR_NOT_RUNNING_MESSAGE = "supervisor not running"

    # List of tools to exclude -- some services may have tools defined by files under their ./server/bin/ directory that must not be executed
    DISABLED_TOOLS = ()

    # List of tools to hide from tool command, but still allow for calling -- essentially for "unpublished" tools that shouldn't be visible
    HIDDEN_TOOLS = ()


    # Config dict that holds all key-value settings:
    _angel = None
    _config = None  # This is legacy and should go away, but for now, gives us backwards compatibility


    def __init__(self, angel_obj):
        self._config = angel_obj.get_settings()
        for f in self._config:
            if isinstance(self._config[f], basestring) and self._config[f].startswith('~'):
                self._config.set(f, os.path.expanduser(self._config[f]))
        self._angel = angel_obj
        self._supervisor_pidfile = self._angel.get_supervisor_lockpath(self.__class__.__name__)
        self._supervisor_statusfile = self._angel.get_supervisor_lockpath(self.__class__.__name__).replace('.lock','') + '.status'
        start_command_hint = " [Try: %s]" % self._angel.get_command_for_running(args=('tool', self.getServiceName(), 'start'))
        self.SUPERVISOR_NOT_RUNNING_MESSAGE = "supervisor not running%s" % start_command_hint


    def service_start(self):
        ''' Start the service. Make SURE to create lockfile at self._supervisor_pidfile -- that's what defines the service as currently running.'''
        raise Exception('service_start() must be implemented in service %s' % self.getServiceName())


    def service_status(self):
        ''' status is expected to return a status struct -- see self.getStatStruct(). '''
        return self.checkStatusViaPid()
    

    def service_stop(self, server_pid):
        # Stop service using the server pid that our supervisor daemon is waiting on.
        # server_pid is the process id of the server process itself (e.g. postgresql postmaster; apache2; varnish); NOT the supervisor process.
        # Note that this is called from within the same running instance of the code that started the service;
        # that is, upgrades to this logic won't be applied to already-running instances.
        return self.killUsingPid(server_pid)


    def service_reload(self, is_code_changed, is_conf_changed, flush_caches_requested):
        ''' This is normally called when the code has been upgraded or the conf has changed.
            In testing conditions, it may also be called if the data has been changed -- i.e. data in the DB has been reset and any caches should be cleared.
             - Reloading a service MUST not cause any downtime (i.e. this is called on prod with live traffice flowing).
             - Reload is called with is_conf_changed=True whenever the config has changed and a service may need to update itself (i.e. new IP addresses in varnish)
             - When code has changed, it's very likely that our base dir will be under a new path on this run (i.e. package-based installs during upgrades)
        '''
        return 0  # by default, don't do anything.


    def rotateLogs(self):
        if not self.isServiceRunning():
            print >>sys.stderr, "Log rotation: service %s not running; need to move files aside, otherwise nothing to do." % self.getServiceName()
            return 0
        supervisor_pid = get_pid_from_pidfile(self._supervisor_pidfile)
        if supervisor_pid is None:
            print >>sys.stderr, "Warning: skipping logfile rotation (can't find supervisor pid for service %s)." % self.getServiceName()
            return -2
        try:
            os.kill(supervisor_pid, signal.SIGWINCH)  # Send supervisor SIGWINCH to tell it to roll over STDOUT / STDERR files.
        except Exception as e:
            print >>sys.stderr, "Warning: skipping logfile rotation (failed to send SIGWINCH to supervisor pid %s: %s)." % (supervisor_pid, e)
            return -3
        # To-do: check that all files in dir have been rolled over?
        return 0


    def switchToRegularMode(self):
        ''' Called when system is coming out off offline / maintenance mode. Most services should ignore this. '''
        return 0


    def switchToMaintenanceMode(self):
        ''' Called when system is going into offline / maintenance mode. Most services should ignore this. '''
        return 0


    def service_repair(self):
        ''' Called when the service status shows errors and 'service repair' is triggered.
            This can be overriden in your subclass to do something smarter; restarting() is a reasonable default for now.
        '''
        return self.trigger_restart()


    def decommission_precheck(self):
        ''' Check if the service can be decommissioned. Return True if and only if this service can support a call to decommission. '''
        return False


    def decommission(self):
        ''' Tell the service that the node is being decommissioned, and that the service should transfer data away and transition traffic elsewhere.

            MUST NOT RETURN until any necessary data has been completed.
            For example, if the service needs to finish push events to another service,
            the queue must be satisfactorially drained before this returns.

            - The node will be marked as decommissioned regardless of the return of this function; this function should
              only be called once, ever.
            - The decommission_precheck() call can be used to prevent a call to decommission().

            - A return of 0 means that all data for the service has been processed and that
            there is no data for the service on this node that we care about any longer.
            - A non-zero return will fail the decommission call.
            
        '''
        # By default, we can't be decommissioned -- this way if a service fails to implement this, we don't hose ourselves:
        return 1


    def trigger_start(self):
        ''' Called during service start -- don't override this; override service_start() instead. '''
        if self._angel.is_decommissioned():
            print >>sys.stderr, "Error: can't start service on a decomissioned node."
            return -1
        if self._config['RUN_AS_USER']:
            if 0 != os.getuid() and os.getuid() != pwd.getpwnam(self._config['RUN_AS_USER']).pw_uid:
                print >>sys.stderr, "Error: can't start service as current user. Try sudo?"
                return -2
        self.setSupervisorStatusMessage(None)  # Make sure to clear out any stale message (i.e. why a last start failed)
        ret_val = self.service_start()
        if not os.path.isfile(self._supervisor_pidfile) and ret_val == 0:
            print >>sys.stderr, "Error: start failed to create lockfile '%s'." % self._supervisor_pidfile
            return -3
        return ret_val


    def trigger_restart(self):
        ''' Restart the service. '''
        self.trigger_stop()  # Trigger stop will invoke service_stop via unix signal, so we'll stop using the current running version; and then start with this version.
        return self.service_start()


    def trigger_status(self):
        ''' Manage calls to service_status().
            Updates the state from Error to Starting if the service is expected to still be coming up.
        '''
        daemon_info = self.getDaemonLockfileInfo()
        if daemon_info is None or 'pid' not in daemon_info:
            reason = self.getSupervisorStatusMessage()
            if reason is None:
                reason = self.SUPERVISOR_NOT_RUNNING_MESSAGE
            return self.getStatStruct(message=reason, state=angel.constants.STATE_STOPPED)

        struct = self.service_status()
        if self.isStatStructStateOk(struct):
            return struct

        # If state isn't OK, then we'll check to see if the supervisor has any reasons to append to the status check:
        if daemon_info is not None:
            reason = self.getSupervisorStatusMessage()
            if reason is not None:
                self.updateStatStruct(struct, replace_instead_of_append=True, message=reason)

            # If we're just starting, always force the state to be STARTING (unless we're already at an OK/WARN level):
            if 'uptime' in daemon_info:
                if daemon_info['uptime'] < self.ALLOWED_STARTUP_TIME_SECS:
                    # Then daemon manager has only been up for a short while -- assume starting up:
                    self.updateStatStruct(struct, replace_instead_of_append=True, state=angel.constants.STATE_STARTING)

        # We'll also check if iptables is blocking us, via the linux-netdown tool:
        if os.path.isfile(self._get_linux_netup_filename()):
            self.updateStatStruct(struct, message='[see linux-netdown tool!]')

        return struct


    def trigger_stop(self):
        ''' Called by service stop -- don't override this; override service_stop() instead. '''

        if self._angel.is_decommissioned():
            # Don't override this -- if a node is in-process of decomissioning, stop services underneath the decomissioning logic could be Very Bad.
            print >>sys.stderr, "Error: can't stop services on a decomissioned node."
            return -1

        # Stop our supervisor process, which will in turn run any stop logic as defined in service_stop():
        daemon_pid = get_pid_from_pidfile(self._supervisor_pidfile)
        if daemon_pid is None:
            print >>sys.stderr, "Warning: service %s isn't running; skipping stop request (no lockfile?)." % (self.getServiceName())
            return 0

        ret_val = 0
        if not is_pid_running(daemon_pid):
            print >>sys.stderr, "Error: went to stop supervisor daemon %s, but pid isn't running." % daemon_pid
            try:
                os.remove(self._supervisor_pidfile)
                print >>sys.stderr, "Error: went to stop supervisor daemon %s, but pid isn't running; stale lock file removed."  % daemon_pid
            except Exception as e:
                print >>sys.stderr, "Error: went to stop supervisor daemon %s, but pid isn't running; unable to remove lockfile (%s)"  % (daemon_pid, e)
            return -1
        try:
            os.kill(daemon_pid, signal.SIGTERM)
        except Exception as e:
            print >>sys.stderr, "Error: failed to send SIGTERM to supervisor daemon %s: %s" % (daemon_pid, e)
            return -2

        wait_time = self.ALLOWED_STOP_TIME_SECS
        if wait_time < (self.STOP_SOFT_KILL_TIMEOUT + self.STOP_HARD_KILL_TIMEOUT):
            wait_time = self.STOP_SOFT_KILL_TIMEOUT + self.STOP_HARD_KILL_TIMEOUT + 10
            print >>sys.stderr, "ALLOWED_STOP_TIME_SECS %s too short (soft timeout: %s, hard timeout: %s); setting to %s." % (self.ALLOWED_STOP_TIME_SECS, self.STOP_SOFT_KILL_TIMEOUT, self.STOP_HARD_KILL_TIMEOUT, wait_time)
        try:
            while is_pid_running(daemon_pid):
                time.sleep(0.5)
                wait_time -= 0.5
                if wait_time < 0:
                    print >>sys.stderr, "Error: %s failed to stop within %s seconds; process %s still running." % (self.getServiceName(), self.ALLOWED_STOP_TIME_SECS, daemon_pid)
                    ret_val = -3
                    break;
        except KeyboardInterrupt:
            print >>sys.stderr, "\nInterrupted while waiting for process %s to quit. (It should exit eventually but may leave a stale lockfile; should be ok.)" % daemon_pid
            return -4
        except Exception as e:
            print >>sys.stderr, "Error: aborted while waiting for %s to stop; process %s still running: %s" % (self.getServiceName(), daemon_pid, e)
            return -5

        return ret_val


    def trigger_repair(self):
        ''' Called by service repair -- don't override this; override service_repair() instead. '''

        # To-Do: check that we're not in shutdown mode?

        # If service isn't running, start it:
        if not self.isServiceRunning():
            print >>sys.stderr, "Repair: service %s not running; starting it." % self.getServiceName()
            return self.trigger_start()

        # If service is running, check if last startup time is less than ALLOWED_STARTUP_TIME_SEC seconds ago; if so, don't do anything:
        time_since_last_start = self._get_seconds_since_last_child_start()
        if time_since_last_start is not None and time_since_last_start < self.ALLOWED_STARTUP_TIME_SECS:
            print >>sys.stderr, "Repair: service %s last started %s seconds ago; skipping repair." % (self.getServiceName(), int(time_since_last_start))
            return 0

        if self.getServiceState() == angel.constants.STATE_ERROR:
            # Don't use self.isServiceStateOk -- that'll return false during startup and shutdown states.
            print >>sys.stderr, "Repair: service %s has errors; attempting to fix it." % self.getServiceName()
            return self.service_repair()

        return 0


    def trigger_reload(self, is_code_changed, is_conf_changed, flush_caches_requested):
        ''' Called by service management -- don't override this; override service_reload() instead. '''
        if not self._config['DEFAULT_SERVICE_RELOAD_ON_UPGRADE']:
            return 0
        reload_config_setting_name = self.getServiceName().upper() + '_SERVICE_RELOAD_ON_UPGRADE'
        if reload_config_setting_name in self._config:
            if not self._config[reload_config_setting_name] or self._config[reload_config_setting_name].lower() == 'false':
                # Check for 'false' string -- there may not be a default setting for the variable to cast the type, so we might be getting a string instead
                print >>sys.stderr, "Warning: skipping %s reload; %s is false." % (self.getServiceName(), reload_config_setting_name)
                return 0
        return self.service_reload(is_code_changed, is_conf_changed, flush_caches_requested)


    def get_process_uptime(self):
        ''' Returns how many seconds the process has been running. Note that the service itself may have been up longer;
        if the process crashes we'll restart it.
        '''
        return self._get_seconds_since_last_child_start()


    def get_supervisor_pid(self):
        ''' Returns the process ID of the running supervisor for this service, or None.
        '''
        daemon_info = self.getDaemonLockfileInfo()
        if daemon_info is not None and 'pid' in daemon_info:
            if is_pid_running(daemon_info['pid']):
                return daemon_info['pid']
        return None


    def get_server_process_pid(self):
        ''' Returns the process ID of the process running under supervisor for this service, or None.
        '''
        daemon_info = self.getDaemonLockfileInfo()
        if daemon_info is not None and angel.constants.LOCKFILE_DATA_CHILD_PID in daemon_info:
            if is_pid_running(daemon_info[angel.constants.LOCKFILE_DATA_CHILD_PID]):
                return daemon_info[angel.constants.LOCKFILE_DATA_CHILD_PID]
        return None


    def _get_seconds_since_last_child_start(self):
        ''' Returns the number of seconds since the supervised process was last started, or None.
            Note: not the same thing as when supervisor was started. '''
        time_since_last_start = None
        daemon_info = self.getDaemonLockfileInfo()
        if daemon_info is not None and 'pid' in daemon_info:
            if is_pid_running(daemon_info['pid']):
                if angel.constants.LOCKFILE_DATA_CHILD_START_TIME in daemon_info:
                    time_since_last_start = int(time.time() - int(daemon_info[angel.constants.LOCKFILE_DATA_CHILD_START_TIME]))
        return time_since_last_start


    def isServiceRunning(self):
        return is_pid_in_pidfile_running(self._supervisor_pidfile)


    def isNamedServiceRunningLocally(self, name_of_service):
        ''' Is a service with the given name running locally, as tracked by a supervisor pid file?
            This is gross -- really, we should be using a directory service to keep track of this,
            but for now this buys us a quick fix to shutdown dependency ordering on a local dev node. '''
        return is_pid_in_pidfile_running(self._angel.get_supervisor_lockpath(name_of_service))


    def isUserIdOkForPort(self, port):
        # If we're non-root, make sure we're not trying to bind to a port below 1024:
        if 0 != os.getuid() and int(port) < 1024:
            print >>sys.stderr, "Can't start service on port %s without being root." % port
            return False
        return True


    def getServiceCodeDir(self):
        return os.path.dirname(os.path.realpath(sys.modules[self.__module__].__file__))


    def get_service_data_dir(self):
        return os.path.join(os.path.expanduser(self._config['DATA_DIR']), self.getServiceName())


    def get_service_built_dir(self):
        return os.path.join(os.path.expanduser(self._angel.get_project_base_dir()),
                                               'built', 'services', os.path.split(self.getServiceCodeDir())[1])


    def getServiceName(self):
        ''' Return a short human-readable name of service. '''
        name = self.__class__.__name__.replace('Service','')
        return '-'.join(re.findall('[A-Z][^A-Z]*', name)).lower()  # ExampleName -> example-name


    def getServiceNameConfStyle(self):
        ''' Return a short human-readable name of service. '''
        name = self.__class__.__name__.replace('Service','')
        return '_'.join(re.findall('[A-Z][^A-Z]*', name)).upper()  # ExampleName -> EXAMPLE_NAME


    def setSupervisorStatusMessage(self, message):
        ''' Set a status message for our supervisor (or None to unset it). This status message gets displayed as part of status;
            it can be set by any process (typically a child worker process of the supervisor). '''        
        try:
            if message is None or len(message) == 0:
                if os.path.isfile(self._supervisor_statusfile):
                    os.remove(self._supervisor_statusfile)
            else:
                open(self._supervisor_statusfile, 'w').write(message)
        except:
            print >>sys.stderr, "Warning: couldn't update status message: %s = %s" % (self._supervisor_statusfile, message)


    def getSupervisorStatusMessage(self):
        if not os.path.isfile(self._supervisor_statusfile):
            return None
        try:
            return open(self._supervisor_statusfile, 'r').read()
        except:
            return None


    def getLockViaPidfile(self, pidfile, print_errors=True):
        return write_pidfile(pidfile, os.getpid(), print_errors=print_errors)


    def releasePidfile(self, pidfile):
        return release_pidfile(pidfile)


    def _getLockPidfile(self, lockname):
        lockname = re.sub(r'([^\w\-\_])+', '?', lockname)
        return os.path.join(os.path.expanduser(self._config['LOCK_DIR']), 'servicelock-%s-%s' % (self.getServiceName(), lockname))


    def getLock(self, lockname, print_errors=True):
        ''' Attempt to get a lock using the given name. (Mostly meant for tool commands that want to ensure non-concurrent runs.) '''
        return self.getLockViaPidfile(self._getLockPidfile(lockname), print_errors=print_errors)


    def releaseLock(self, lockname):
        return self.releasePidfile(self._getLockPidfile(lockname))


    def isLockAvailable(self, lockname):
        if not os.path.isfile(self._getLockPidfile(lockname)):
            return True
        # It's possible that we have a stale lock file -- so we'll try to actually get the lock, which will release it.
        # This only works if the callee has write permission to the lockdir, though.
        if 0 != self.getLock(lockname, print_errors=False):
            return False
        self.releaseLock(lockname)
        return True


    def getPidOfLockOwner(self, lockname):
        ''' return the PID of the process owning the named lock, or None if it's not locked. '''
        return get_pid_from_pidfile(self._getLockPidfile(lockname))


    def getDaemonLockfileInfo(self):
        return self.getLockfileInfo(self._supervisor_pidfile)


    def getLockfileInfo(self, pidfile):
        ''' Return all info in the given pidfile. Adds 'uptime' and 'pid' to the returned dict where applicable. 'pid' will only exist if the process is up and running. '''
        lockfile_data = read_data_from_lockfile(pidfile)
        if lockfile_data is None:
            return None

        pid = get_pid_from_pidfile(pidfile)
        if pid is None or not is_pid_running(pid):
            return lockfile_data
        lockfile_data['pid'] = pid

        if angel.constants.LOCKFILE_DATA_DAEMON_START_TIME in lockfile_data:
            try:
                lockfile_data['uptime'] = time.time() - int(lockfile_data[angel.constants.LOCKFILE_DATA_DAEMON_START_TIME])
            except:
                print >>sys.stderr, "Warning: invalid start time '%s' in lockfile %s; ignoring." % \
                    (lockfile_data[angel.constants.LOCKFILE_DATA_DAEMON_START_TIME], pidfile)

        if angel.constants.LOCKFILE_DATA_CHILD_PID in lockfile_data:
            try:
                child_pid = int(lockfile_data[angel.constants.LOCKFILE_DATA_CHILD_PID])
                lockfile_data[angel.constants.LOCKFILE_DATA_CHILD_PID] = child_pid
            except:
                print >>sys.stderr, "Warning: invalid child_pid '%s' in lockfile %s." % \
                    (lockfile_data[angel.constants.LOCKFILE_DATA_CHILD_PID], pidfile)

        return lockfile_data



    def getStatStruct(self, service_name=None, message=None, state=None, data=None):
        stat_struct = stats_create_struct(message=message, state=state, data=data)
        if service_name is None:
            service_name = self.getServiceName()
        stat_struct['service_name'] = service_name.replace('Service',"")
        return stat_struct


    def isStatStructStateOk(self, struct, accept_warnings_as_ok=True):
        if 'state' not in struct: return False
        if struct['state'] == angel.constants.STATE_RUNNING_OK: return True
        if struct['state'] == angel.constants.STATE_WARN and accept_warnings_as_ok: return True
        return False


    def parseNagiosOutputToStatStruct(self, stat_struct, nagios_string):
        ''' Nagios plugins generate a message on STDOUT that is <message>|<data>. Split the data portion into the approriate key/value pairs. '''
        if nagios_string is None or len(nagios_string) == 0:  # This can happen when a nagios plugin times out or fails.
            return stat_struct
        nagios_message = nagios_string.split("\n")[0].partition('|')[0].strip()
        nagios_data = None
        nagios_data = nagios_string.split("\n")[0].partition('|')[2]
        self.updateStatStruct(stat_struct, message=nagios_message)
        stats_import_nagios_data(stat_struct, nagios_data)
        return stat_struct  # return is for convience of chaining calls together


    def deleteStatFromStatStruct(self, stat_struct, data_name):
        stats_delete_data_record(stat_struct, data_name)
        return stat_struct  # return is for convience of chaining calls together


    def mergeStatStructs(self, stat_struct_to_merge_into, stat_struct_to_import):
        stats_merge_structs(stat_struct_to_merge_into, stat_struct_to_import)
        return stat_struct_to_merge_into  # return is for convience of chaining calls together


    def updateStatStruct(self, struct, message=None, state=None, replace_instead_of_append=False):
        stats_update_struct(struct, message=message, state=state, replace_instead_of_append=replace_instead_of_append)
        return struct  # return is for convience of chaining calls together


    def addDataPointToStatStruct(self, struct, name, value, unit=None, stat_group=None):
        stats_add_data_record(struct, name, value, unit=unit, stat_group=stat_group)
        return struct  # return is for convience of chaining calls together


    def getServiceState(self):
        s = self.service_status()
        return s['state']


    def isServiceStateOk(self, accept_warnings_as_ok=True):
        return self.isStatStructStateOk(self.service_status(), accept_warnings_as_ok=accept_warnings_as_ok)


    def checkStatusViaNagiosPlugin(self, name, args):
        ''' name is a string containing the name of the Nagios Plugin inside the Nagios plugin dir; args is an '[ ]' list of parameters to pass '''
        plugin_binary = self.getNagiosPluginPath(name)
        if plugin_binary is None:
            return self.getStatStruct(message="Missing nagios plugin %s" % name, state=angel.constants.STATE_UNKNOWN)
        output, err, exitcode = self.getCommandOutput(plugin_binary, args=args, log_nonzero_exits=False)
        return self.parseNagiosOutputToStatStruct(self.getStatStruct(state=exitcode), output)


    def checkStatusViaPid(self):
        daemon_info = self.getDaemonLockfileInfo()
        if daemon_info is None or 'pid' not in daemon_info:
            return self.getStatStruct(message=self.SUPERVISOR_NOT_RUNNING_MESSAGE, state=angel.constants.STATE_STOPPED)
        return self.getStatStruct(message="Running (pid %s)" % daemon_info['pid'], state=angel.constants.STATE_RUNNING_OK)


    def checkStatusViaTCP(self, host, port, warn_time=2, error_time=4, check_timeout=6):
        if host is None:
            host = '127.0.0.1'
        args = ['-H', host, '-p', str(port)]
        if warn_time is not None:
            args += ['-w', str(warn_time)]
        if error_time is not None:
            args += ['-c', str(error_time)]
        if check_timeout is not None:
            args += ['-t', str(check_timeout)]
        status = self.checkStatusViaNagiosPlugin('check_tcp', args)
        if 'message' in status and status['message'] is not None:
            status['message'] = status['message'].replace('Connection refused', 'Connection to %s:%s refused' % (host,port))
        return status


    def checkStatusViaPidAndTcp(self, ip, port):
        daemon_info = None
        if ip is None:
            # Then we're checking a local instance, also check the pidfile:
            daemon_info = self.getDaemonLockfileInfo()
            if daemon_info is None or 'pid' not in daemon_info:
                return self.getStatStruct(message=self.SUPERVISOR_NOT_RUNNING_MESSAGE, state=angel.constants.STATE_STOPPED)
        # By default, an ip of None will check 127.0.0.1, and this usually works, because most services bind to 0.0.0.0.
        stat_struct = self.checkStatusViaTCP(ip, port)
        if daemon_info is None: return stat_struct
        return stat_struct


    def getNagiosPluginPath(self, name):
        ''' Return the full path for the given nagios plugin. If the given name starts with a /, assume we have a full-path already and return that. '''
        path = name
        if not path.startswith('/'):
            possible_common_dirs = ('/usr/lib/nagios/plugins/', '/usr/lib64/nagios/plugins', '/usr/local/sbin', '/usr/sbin')
            path = self.which(name, additional_paths=possible_common_dirs)
            if path is None:
                print >>sys.stderr, "Error: can't find nagios plugin '%s' under default and nagios paths (%s)." % \
                                    (name, ':'.join(possible_common_dirs))
                return None
        if not os.path.exists(path):
            print >>sys.stderr, "Warning: nagios plugin %s missing (can't find %s)." % (name, path)
            return None
        return path


    def waitForServicesToStart(self, dependents, timeout_in_seconds=None):
        ''' Given a list of service names ('apache2','varnish'), wait for the services to return an OK status. '''
        for service_name in dependents:
            service_obj = self._angel.get_service_object_by_name(service_name)
            if service_obj is None: 
                print >>sys.stderr, "Error: unknown service '%s' in %s service dependencies." % (service_name, self.getServiceName())
                return 1
            if 0 != self.waitForOkayStatus(service_obj.service_status, timeout_in_seconds=timeout_in_seconds):
                return 1
        return 0


    def waitForOkayStatus(self, status_ok_func, timeout_in_seconds=None, args=()):
        ''' status_ok_func needs to be a function that returns a dict, which should contain key 'state' with one of the defined Nagios state values. '''
        ''' Returns 0 once the service comes up; non-zero otherwise (i.e. timeout). '''
        retry_interval_in_seconds = 1
        if timeout_in_seconds is None:
            timeout_in_seconds = 60*60  # After an hour, something is probably wedged -- exit out
        accept_warnings_as_ok = True
        update_status_messages = True
        wait_time = 0
        last_message_printed_time = 0
        cur_state = self.getStatStruct(state=angel.constants.STATE_UNKNOWN)
        ret_val = 1
        cancel_count_until_error = 3

        while not self.isStatStructStateOk(cur_state, accept_warnings_as_ok=accept_warnings_as_ok) and wait_time <= timeout_in_seconds:
            cur_state = status_ok_func(*args)
            if self.isStatStructStateOk(cur_state, accept_warnings_as_ok=accept_warnings_as_ok):
                ret_val = 0
                break
            wait_time += retry_interval_in_seconds
            if wait_time - last_message_printed_time > 5:
                last_message_printed_time = wait_time
                print >>sys.stderr, '%s[%s]: waiting for %s: %s' % (self.getServiceName(), os.getpid(), cur_state['service_name'], cur_state['message'])
            if wait_time < timeout_in_seconds:
                if update_status_messages:
                    self.setSupervisorStatusMessage('Waiting for %s (%s seconds elapsed)' % (cur_state['service_name'], int(wait_time)))
                try:
                    time.sleep(retry_interval_in_seconds)
                except:
                    cancel_count_until_error -= 1
                    if cancel_count_until_error <= 0:
                        return 1
                    print >>sys.stderr, "Warning: time.sleep threw exception while waiting for service to start"

        if update_status_messages:
            self.setSupervisorStatusMessage(None)

        return ret_val



    def killUsingPidfile(self, pidfile, soft_kill_signal=None, gracetime_for_soft_kill=None, hard_kill_signal=None, gracetime_for_hard_kill=None):
        ''' Kill the given process, starting with a nice signal (usually SIGHUP), then a warning (usually SIGTERM), then eventually SIGKILL.  
            Return 0 on success, non-zero otherwise. (A 0 return means the process is not running; non-zero means it is running or unknown state.) '''

        if not os.path.exists(pidfile):
            return 0

        pid = get_pid_from_pidfile(pidfile)
        if pid is None:
            release_pidfile(pidfile)
            return 0

        if not is_pid_running(pid):
            print >> sys.stderr, "Killer[%s]: went to kill %s[%s], but process not running." % (os.getpid(), self.getServiceName(), pid)
            release_pidfile(pidfile)
            return 0

        return self.killUsingPid(pid, soft_kill_signal=soft_kill_signal, gracetime_for_soft_kill=gracetime_for_soft_kill, hard_kill_signal=hard_kill_signal, gracetime_for_hard_kill=gracetime_for_hard_kill)



    def killUsingPid(self, pid, soft_kill_signal=None, gracetime_for_soft_kill=None, hard_kill_signal=None, gracetime_for_hard_kill=None):
        ''' See killUsingPidfile. '''

        if soft_kill_signal is None:
            soft_kill_signal = self.STOP_SOFT_KILL_SIGNAL

        if hard_kill_signal is None:
            hard_kill_signal = self.STOP_HARD_KILL_SIGNAL

        if gracetime_for_soft_kill is None: gracetime_for_soft_kill = self.STOP_SOFT_KILL_TIMEOUT
        if gracetime_for_hard_kill is None: gracetime_for_hard_kill = self.STOP_HARD_KILL_TIMEOUT

        min_time_between_kills = 2
        if gracetime_for_soft_kill < min_time_between_kills: gracetime_for_soft_kill = min_time_between_kills 
        if gracetime_for_hard_kill < min_time_between_kills: gracetime_for_hard_kill = min_time_between_kills

        max_time_between_kills = 60*60
        if gracetime_for_soft_kill > max_time_between_kills: gracetime_for_soft_kill = max_time_between_kills 
        if gracetime_for_hard_kill > max_time_between_kills: gracetime_for_hard_kill = max_time_between_kills

        # Take a snapshot of all process relationships before kill:
        pid_mapping = get_pid_relationships()

        # Step through each level of kill using above helper function:
        name = self.getServiceName()
        if 0 != kill_and_wait(pid, name, soft_kill_signal, gracetime_for_soft_kill):
            if 0 != kill_and_wait(pid, name, hard_kill_signal, gracetime_for_hard_kill):
                if 0 != kill_and_wait(pid, name, signal.SIGUSR1, 5):
                    print >>sys.stderr, "Killer[%s]: %s[%s] failed to exit; resorting to SIGKILL." % (os.getpid(), name, pid)
                    if 0 != kill_and_wait(pid, name, signal.SIGKILL, 10):
                       print >>sys.stderr, 'Killer[%s]: unable to kill %s[%s]. Do you need to run as root?' % (os.getpid(), name, pid)
                       return -1

        if pid_mapping is None:  # OS X
            return 0

        # Helper function to print warnings about any still-running processes that were owned by the process we killed:
        def _check_child_processes_not_running(pid_to_check):
            if is_pid_running(pid_to_check):
                print >>sys.stderr, "Killer[%s]: warning: child process %s still running!" % (os.getpid(), pid_to_check)
            if pid_to_check not in pid_mapping:
                #print >>sys.stderr, "Killer[%s]: warning: process %s missing from pid_mapping!" % (os.getpid(), pid_to_check)  # This happens if the process exited after we made our mapping
                return
            for child_pid in pid_mapping[pid_to_check]:
                _check_child_processes_not_running(child_pid)

        _check_child_processes_not_running(pid)
        return 0


    def start_supervisor_with_binary(self, binary, args={}, env=None, reset_env=False, name=None, log_basepath=None,
                                     init_func=None, stop_func=None,
                                     run_as_config_user=False, run_as_user=None, run_as_group=None,
                                     pidfile=None, chdir_path=None,
                                     process_oom_adjustment=0, nice_value=0, run_as_daemon=True,
                                     include_conf_in_env=False):
        # To-do: elminate run_as_user/group, use run_as_config_user, default it to True.
        if binary is None:
            print >>sys.stderr, "Missing binary in service %s (args: %s)" % (self.getServiceName(), args)
            return -1
        if run_as_config_user:
            run_as_user = self._config['RUN_AS_USER']
            run_as_group = self._config['RUN_AS_GROUP']
        if env is None:
            env = {}
        if include_conf_in_env:
            env = self._export_settings_into_env(env)
        if run_as_daemon:
            # Make sure the pidfile path is set up correctly:
            if pidfile is None:
                pidfile = self._supervisor_pidfile
            if pidfile.startswith('~'):
                pidfile = os.path.abspath(pidfile)
            if not pidfile.startswith('/'):
                print >>sys.stderr, "Warning: relative pidfile path %s; adding LOCK_DIR in." % pidfile
                pidfile = os.path.join(os.path.expanduser(self._config['LOCK_DIR']), pidfile)
            # If no stop function, tell launch to call stop() so that we can send the supervisor daemon a SIGHUP and have it send the correct signals for soft and hard kill:
            if stop_func is None:
                stop_func = lambda server_pid: self.service_stop(server_pid)
            # Check log output:
            if log_basepath is None:
                log_name = None
                if name is not None and len(name):
                    log_name = re.sub(r'\W+', ' ', name).split(' ')[0]
                if log_name is None:
                    log_name = self.getServiceName()
                log_basepath = '%s/%s' % (self.getServiceName(), log_name)
        return launch(self._config, binary, args, env=env, reset_env=reset_env, name=name, log_basepath=log_basepath, chdir_path=chdir_path,
                      run_as_daemon=run_as_daemon, pid_filename_for_daemon=pidfile, init_func=init_func, stop_func=stop_func, run_as_user=run_as_user, run_as_group=run_as_group,
                      nice_value=nice_value, process_oom_adjustment=process_oom_adjustment)


    def start_supervisor_with_function(self, function_name, run_func):
        log_name = function_name.replace('_','-')
        log_basepath = '%s/%s' % (self.getServiceName(), log_name)
        return launch_via_function(self._config, function_name, log_basepath, run_func,
                            run_as_daemon=True, pid_filename_for_daemon=self._supervisor_pidfile, stop_func = lambda server_pid: self.service_stop(server_pid),
                            run_as_user=self._config['RUN_AS_USER'], run_as_group=self._config['RUN_AS_GROUP'])


    def isPortAvilable(self, port):
        host=''  # defaults to all interfaces
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Allow bind to work on ports in the wait state (i.e. if something was listening)
        try:
            s.bind((host, int(port)))
            s.close()
        except Exception:
            return False
        return True


    def get_service_run_dir(self, create_if_missing=False):
        path = os.path.join(os.path.expanduser(self._config['RUN_DIR']), self.getServiceName())
        if not os.path.exists(path) and create_if_missing:
            self.createDirsIfNeeded(path, owner_user=self._config['RUN_AS_USER'], owner_group=self._config['RUN_AS_GROUP'])
        return path


    def get_service_log_dir(self, create_if_missing=False):
        path = os.path.join(os.path.expanduser(self._config['LOG_DIR']), self.getServiceName())
        if not os.path.exists(path) and create_if_missing:
            self.createDirsIfNeeded(path, owner_user=self._config['RUN_AS_USER'], owner_group=self._config['RUN_AS_GROUP'])
        return path


    def createConfDirFromTemplates(self, src_path=None, dest_path=None, owner_user=None, owner_group=None, mode=0755, vars=None, reset_dir=False):
        ''' When src_path is None, copy template files from ./services/<service>/conf/ to <RUN_DIR>/<service>/, replacing settings tokens with current values;
            and also delete any unknown files in dest_path.

            If src_path is given, unknown files in dest_path will be left unmodified; making it safe to expand into settings dirs for other services.
            src_path can be a full path or a relative path under ./services/<current_service>/conf/

            If reset_dir is True, any existing conf dir will be deleted first. This can lead to unexpected results if a service's tools are actively running
            with the conf dir -- i.e. don't reset the conf dir on ever conf_dir creation if multiple tools are firing off at once using it.

            Returns a tuple of the full path to newly-created conf file or directory and number of lines changed from existing files in dir, or None, -1 on error.
        '''

        delete_unknown_files = False
        if src_path is None:
            delete_unknown_files = True
            src_path = os.path.join(self.getServiceCodeDir(), 'conf')
            if not os.path.isdir(src_path):
                print >>sys.stderr, "Error: can't find conf template dir '%s' for service %s." % (src_path, self.getServiceName())
                return None, -1
        else:
            if not src_path.startswith('/'):
                src_path = os.path.join(self.getServiceCodeDir(), 'conf', src_path)

        if dest_path is None:
            dest_path = os.path.join(self.get_service_run_dir(), "conf")

        if reset_dir and os.path.isdir(dest_path):
            try:
                shutil.rmtree(dest_path)
            except:
                print >>sys.stderr, "Error: can't reset conf template dir '%s' for service %s." % (src_path, self.getServiceName())
                return None, -2

        ret_val = self.install_template_to_dir(src_path, dest_path,
                                               owner_user=owner_user, owner_group=owner_group, mode=mode,
                                               vars=vars, delete_unknown_files=delete_unknown_files)
        if ret_val < 0: return None, -3
        return dest_path, ret_val


    def createDirsIfNeeded(self, absolute_path, name="service", owner_user=None, owner_group=None, mode=0755):
        return create_dirs_if_needed(absolute_path, name=name, owner_user=owner_user, owner_group=owner_group, mode=mode)


    def which(self, names, additional_paths=(), must_be_executable=True):
        ''' Return the path to the named executable, or None if not found.
            names can be a string or list of strings to be searched for within those paths that are directories.
            paths can be a list of additional potential directories or file paths to search inside.
            If no matching executable is found within the paths given, we'll also search in the following directories in this order:
                <base_dir>/services/<service>/{bin, server/bin, server/sbin}
                <base_dir>/built/services/<service>/{bin, server/bin, server/sbin}
                <base_dir>/built/bin
                <base_dir>/bin
                /opt/local/bin
                /usr/local/bin
                /usr/bin
                /usr/sbin
                /bin
        '''

        # Any paths passed in to us take precedence over default paths, so start list with them:
        paths = additional_paths

        # Add service-specific paths:
        built_base_dir = self.get_service_built_dir()
        for base in (self.getServiceCodeDir(), built_base_dir):
            for dir in ('bin', os.path.join('server', 'bin'), os.path.join('server', 'sbin')):
                paths += (os.path.join(base, dir),)

        # Add project-level bin paths:
        for bin_path in self._config['BIN_PATHS'].split(':'):
            paths += (os.path.join(self._angel.get_project_base_dir(), bin_path),)

        # Add top-level and system dirs to path (system dirs come last so that project paths take precedence):
        paths += (os.path.join(os.path.expanduser(self._angel.get_project_base_dir()),'built','bin'),
                  os.path.join(os.path.expanduser(self._angel.get_project_base_dir()),'bin'),
                  '/opt/local/bin',
                  '/usr/local/bin',
                  '/usr/bin',
                  '/usr/sbin',
                  '/bin')

        # We could include os.environ['PATH'], but that's risky: if something is started from a shell that happens
        # to have that set to something that wouldn't exist otherwise, services might appear to work when they
        # wouldn't on a proper startup.

        for some_path in paths:
            if not some_path.startswith('/'):
                print >>sys.stderr, "Warning: non-absolute path '%s' given to which()" % some_path
                continue
            if os.path.isfile(some_path) and os.access(some_path, os.X_OK):
                return some_path
            if os.path.isdir(some_path):
                if names is None:
                    print >>sys.stderr, "Warning: 'which' function given dir path '%s' but no names to search for." % some_path
                    continue
                if isinstance(names, str):
                    names = list((names,))
                for name in names:
                    this_path = os.path.join(some_path,name)
                    # Do not use realpath -- some sbin dirs use symlinks with different bins
                    # pointing to different dirs, and unaliasing that can break things (e.g. nagios)
                    if os.path.isfile(this_path):
                        if must_be_executable:
                            if os.access(this_path, os.X_OK):
                                return this_path
                        else:
                            return this_path
        return None


    def whichDir(self, paths):
        '''Given a list of directories, return the first one that exists, or None.'''
        for path in paths:
            if not path.startswith('/'):
                print >>sys.stderr, "Warning: non-absolute path given to whichDir()"
            if os.path.isdir(path):
                return path
        return None


    def execCommand(self, command, args=None, env=None, reset_env=False, nice_value=None, chdir_path=None,
                    run_as_config_user=False, stdin_string=None, include_conf_in_env=False,
                    run_as_child_and_block=False, stdout_fileno=None):
        ''' Exec (replace current running process) with the given command and optional args and env.
            When run_as_child_and_block is true, fork, exec in child, block, and return the child exit code. '''
        run_as_user = None
        run_as_group = None
        if run_as_config_user:
            run_as_user = self._config['RUN_AS_USER']
            run_as_group = self._config['RUN_AS_GROUP']
        if include_conf_in_env:
            env = self._export_settings_into_env(env)
        return exec_process(command, args=args, env=env, reset_env=reset_env, nice_value=nice_value,
                            chdir_path=chdir_path, run_as_user=run_as_user, run_as_group=run_as_group,
                            stdin_string=stdin_string, run_as_child_and_block=run_as_child_and_block,
                            stdout_fileno=stdout_fileno)


    def _getBackgroundFunctionLockfilePath(self, name):
        return os.path.join(os.path.expanduser(self._config['LOCK_DIR']),
                            "%s-bg-function-%s" % (self.getServiceName(), name))
        

    def runFunctionInBackground(self, exec_func, name, run_as_user=None, run_as_group=None):
        lockfile_path = self._getBackgroundFunctionLockfilePath(name)
        log_basepath = os.path.join(os.path.expanduser(self._config['LOG_DIR']), self.getServiceName(), name)
        return run_function_in_background(self._config, name, lockfile_path, exec_func, log_basepath=log_basepath,
                                          run_as_user=run_as_user, run_as_group=run_as_group)


    def killFunctionInBackground(self, name):
        pidfile = self._getBackgroundFunctionLockfilePath(name)
        pid = get_pid_from_pidfile(pidfile)
        if pid is None:
            return 0
        return self.killUsingPid(pid)


    def isBackgroundFunctionRunning(self, name):
        pidfile = self._getBackgroundFunctionLockfilePath(name)
        pid = get_pid_from_pidfile(pidfile)
        if pid is None:
            return False
        return True


    def runCommand(self, command, args=None, env=None, reset_env=False, run_as_config_user=False, chdir_path=None,
                   log_nonzero_exits=True, timeout_in_seconds=None, include_conf_in_env=False, stdin_string=None):
        ''' Run the given command (swallowing stdout/stderr) with optional args and env, and return the exit code. '''
        out, err, exitcode = self.getCommandOutput(command,
                                                   args=args,
                                                   env=env,
                                                   run_as_config_user=run_as_config_user,
                                                   reset_env=reset_env,
                                                   chdir_path=chdir_path,
                                                   stdin_string=stdin_string,
                                                   log_nonzero_exits=log_nonzero_exits,
                                                   timeout_in_seconds=timeout_in_seconds,
                                                   include_conf_in_env=include_conf_in_env)
        return exitcode


    def get_angel_command_output(self, args, setting_overrides=None, timeout_in_seconds=None, stdin_string=None):
        command = self._angel.get_project_exec()
        (args, env) = self._angel.get_args_and_env_for_running_self(args=args,
                                                                    setting_overrides=setting_overrides)
        return self.getCommandOutput(command, args=args, env=env,
                                     timeout_in_seconds=timeout_in_seconds,
                                     stdin_string=stdin_string,
                                     log_nonzero_exits=False)


    def getCommandOutput(self, command, args=None, env=None, chdir_path=None,
                         reset_env=False, include_conf_in_env=False, tee_output=False,
                         run_as_config_user=False, log_nonzero_exits=True, timeout_in_seconds=None, stdin_string=None):
        ''' Run given command with optional args and env settings and return stdout, stderr, and exit code.
            If run_as_config_user is True, this will be run with the user and group as defined in config.run_as_user / config.run_as_group, otherwise the current effective user is used. '''
        run_as_user = run_as_group = None
        if run_as_config_user:
            run_as_user = self._config['RUN_AS_USER']
            run_as_group = self._config['RUN_AS_GROUP']
        if include_conf_in_env:
            env = self._export_settings_into_env(env)
            if env is None:
                return None, None, None
        return get_command_output(command, args=args, env=env, chdir=chdir_path, reset_env=reset_env, tee_output=tee_output,
                                  run_as_user=run_as_user, run_as_group=run_as_group,
                                  print_error_info=log_nonzero_exits, timeout_in_seconds=timeout_in_seconds,
                                  stdin_string=stdin_string)


    def _export_settings_into_env(self, env):
        if env is None:
            env = {}
        env_prefix = "%s_SETTING_" % self._angel.get_project_name().upper()
        for k in self._config:
            if k.startswith('.'): continue
            if type(self._config[k]) not in (float, int, bool, type(None), str):
                continue  # This might lead to some vars being dropped if we add supporting other types...
            env['%s%s' % (env_prefix, k)] = self._config[k]
        settings_env_name = "%s_SETTINGS" % self._angel.get_project_name().upper()
        if settings_env_name not in env:
            env[settings_env_name] = self.export_settings_to_tmp_file()
        if env[settings_env_name] is None:
            return None
        return env


    def fetchDataFromUrl(self, url, timeout=5, silent=False, headers=None,
                         http_auth_username=None, http_auth_password=None):
        try:
            opener = urllib2.build_opener(urllib2.HTTPHandler)
            request = urllib2.Request(url)
            if headers:
                for h in headers:
                    request.add_header(h, headers[h])
            if http_auth_username and http_auth_password:
                request.add_header('Authorization', "Basic %s" % base64.encodestring('%s:%s' % (http_auth_username, http_auth_password)))
            connection = opener.open(request, None, timeout)
            return connection.read()
        except urllib2.URLError as e:
            if not silent:
                print >>sys.stderr, "Warning: URLError while fetching '%s' (%s)." % (url, e)
            return None
        except Exception as e:
            print >>sys.stderr, "Warning: unexpected error while fetching '%s' (%s)." % (url, e)
            return None


    def fetchStatsFromUrl(self, url, timeout=5, silent=False):
        ''' Given a URL that outputs a text document of "Key: value\n" values, return a dict; or None on failure. '''
        data_str = self.fetchDataFromUrl(url, timeout=timeout, silent=silent)
        if data_str is None:
            return None
        try:
            return dict([ [y.strip() for y in x.split(': ')] for x in data_str.strip().split('\n')])  # "Key: value\nKey-2: value2\n" -> dict{key} -> value
        except Exception as e:
            if not silent:
                print >>sys.stderr, "Warning: failed to parse data from %s (%s)." % (url, e)
            return None



    def shell_tool_start(self, wait=False):
        ''' Start the service, regardless of conf HOST settings.
            * wait: wait for service to start '''
        ret_val = self.trigger_start()
        if not wait or ret_val != 0: return ret_val
        return self.waitForOkayStatus(self.service_status, timeout_in_seconds=120)


    def shell_tool_stop(self, hard=False, mean=False):
        '''
         Stop the service (note: 'service repair' may start it again)
           * hard: send kill SIGTERM signals to all service processes; not recommended in prod
           * mean: send kill SIGKILL signals to all service processes; not recommended ever!
        '''
        daemon_pid = get_pid_from_pidfile(self._supervisor_pidfile)
        if daemon_pid is None:
            print >>sys.stderr, "Service %s already stopped." % self.getServiceName()
            return 0
        if hard or mean:
            print >>sys.stderr, "Reminder: --hard and --mean should only be used when a service has become unresponsive to normal stop requests."
            hard_kill_all(self._supervisor_pidfile, send_sigterm=hard, send_sigkill=mean,
                          yes_i_understand_this_is_potentially_dangerous=True)
        return self.trigger_stop()


    def shell_tool_status(self, key=None, interval=None, with_timestamp=False, count=-1, wait=None, with_stats=True, with_summary=True):
        '''
        Show detailed status information with extended data
          * count: when running with an interval, stop after N samples (defaults to forever)
          * interval: show output every N seconds
          * key: output only the value for names key; non-zero exit if min/max ranges defined and exceeded
          * wait: wait for N seconds (defaults to 120) for an okay status; non-zero exit otherwise
          * with_stats: include stats on each interval
          * with_summary: include summary stats at end
          * with_timestamp: include timestamps (epoch,key format when key-based output)
        '''
        if interval is not None:
            try:
                interval = float(interval)
            except:
                print >>sys.stderr, "Invalid interval '%s'." % interval
                return 1
            if interval < 0:
                print >>sys.stderr, "Warning: invalid negative interval %s; using 0 instead." % interval
                interval = 0
            if interval > 3600: 
                print >>sys.stderr, "Using interval of 3,600 seconds instead of %s." % interval
                interval = 3600                

        wait_for_ok = False
        wait_for_ok_time_left = 120
        if wait is not None:
            if type(wait) is bool:
                wait_for_ok = wait
            else:
                try:
                    wait_for_ok_time_left = float(wait)
                    wait_for_ok = True
                except:
                    print >>sys.stderr, "Invalid wait time '%s'." % wait
                    return 1

        if wait_for_ok and interval is not None:
            print >>sys.stderr, "Error: --interval and --wait-for-ok are mutually-exclusive."
            return 1

        whole_second_interval = False
        if interval:
            # This is a bit of a cheat -- if we're running on an interer interval, sleep for a tiny bit so that
            # stats are triggered at the top of the second -- this will roughly syncronize stats on different systems.
            if int(interval) == float(interval):
                whole_second_interval = True

        def _sleep_until_top_of_the_second(max_sleep=0.999):
            ''' sleep until the time rolls over to the next second; skip sleep if that can't happen within max_sleep seconds. '''
            now = time.time()
            fractional_second = 1 - abs(now - int(now))
            if fractional_second > max_sleep:
                return
            try:
                time.sleep(fractional_second)
            except Exception as e:
                print >>sys.stderr, "Error in drift correction (%s)." % e
                return 1

        if whole_second_interval:
            _sleep_until_top_of_the_second()

        do_loop = True
        loops_left = None
        if count > 0:
            loops_left = count
        ret_val = 0
        loop_count = 0
        statistics_values = {}
        statistics_warn_count = {}
        statistics_error_count = {}
        statistics_sample_count = {}
        try:
            while do_loop:
                loop_count += 1
                if loops_left is not None:
                    loops_left -= 1
                    if loops_left <= 0:
                        do_loop = False
                start_time = time.time()
                stat_struct = self.trigger_status()

                if self.isServiceRunning():
                    last_child_start = self._get_seconds_since_last_child_start()
                    if last_child_start is None: last_child_start = -1
                    last_service_start = -1
                    daemon_info = self.getDaemonLockfileInfo()
                    if daemon_info is not None and 'uptime' in daemon_info and daemon_info['uptime'] is not None:
                        try:
                            last_service_start = int(daemon_info['uptime'])
                        except:
                            pass
                    self.addDataPointToStatStruct(stat_struct, 'service uptime', last_service_start, unit=angel.constants.STAT_TYPE_SECONDS)
                    self.addDataPointToStatStruct(stat_struct, 'process uptime', last_child_start, unit=angel.constants.STAT_TYPE_SECONDS)
                    server_pid = self.get_server_process_pid()
                    if server_pid:
                        self.addDataPointToStatStruct(stat_struct, 'server pid', server_pid)

                if wait_for_ok:
                    if not self.isStatStructStateOk(stat_struct):
                        try:
                            time.sleep(1)
                        except:
                            print >>sys.stderr, "Interrupted while waiting for service."
                            return 1
                        wait_for_ok_time_left -= (time.time() - start_time)
                        if wait_for_ok_time_left > 0:
                            continue 

                if key is not None:
                    # When key is given, output format is *just* the data value of the requested key, or empty string if it's missing.
                    # Do not chance this format, so that `tool <servicename> --key <somekey>` can be expanded safely by users.
                    # Return value is 0 if key exists and is within min / max values
                    if 'data' in stat_struct and key in stat_struct['data']:
                        d = stat_struct['data'][key]
                        value = d['value']
                        if with_timestamp:
                            print "%s,%s" % (int(start_time), value)
                        else:
                            print value
                        if 'min' in d and value < d['min']:
                            ret_val = 1
                        if 'max' in d and value < d['max']:
                            ret_val = 1
                    else:
                        print >>sys.stderr, "Error: can't find status info key '%s'." % key
                        ret_val = 1

                else:
                    # When no key is specified, print a human-readable summary of all status info for the service:
                    if with_timestamp:
                        print datetime.datetime.fromtimestamp(start_time).isoformat()
                    if 'service_name' not in stat_struct: stat_struct['service_name'] = '(unknown service!)'
                    if 'message' not in stat_struct: stat_struct['message'] = '(no status message!)'
                    if 'state' not in stat_struct:
                        stat_struct['state'] = None
                    state_string = '(unknown state! %s)' % stat_struct['state']
                    if stat_struct['state'] in angel.constants.STATE_CODE_TO_TEXT: state_string = angel.constants.STATE_CODE_TO_TEXT[stat_struct['state']]
                    loop_count_string = ''
                    if interval:
                        if interval >= 1:
                            loop_count_string = "[%s] " % int(time.time())
                        else:
                            loop_count_string = "[%0.2f] " % time.time()
                    print "%s%s %s: %s" % (loop_count_string, stat_struct['service_name'], state_string, stat_struct['message'])
                    if 'data' in stat_struct:
                        data = stat_struct['data']
                        for this_key in sorted(data):

                            value = data[this_key]['value']
                            line = "%20s: %s" % (this_key, value)

                            if 'unit' in data[this_key]:
                                line += ' %s' % data[this_key]['unit']
                                if data[this_key]['unit'] == angel.constants.STAT_TYPE_SECONDS and value > 120:
                                    minutes = int(value/60) % 60
                                    hours = int(value/3600) % 24
                                    days = int(value/86400)
                                    time_str = ' ('
                                    if days > 0:
                                        time_str += '%s day%s, ' % (days, ('s' if days != 1 else ''))
                                    if hours > 0:
                                        time_str += '%s hour%s, ' % (hours, ('s' if hours != 1 else ''))
                                    time_str += '%s minute%s)' % (minutes, ('s' if minutes != 1 else ''))
                                    line += time_str

                            is_in_error_range = False
                            error_is_larger_than_warn = True
                            if 'error' in data[this_key] and 'warn' in data[this_key]:
                                if data[this_key]['warn'] > data[this_key]['error']:
                                    error_is_larger_than_warn = False

                            if 'error' in data[this_key]:
                                if this_key not in statistics_error_count:
                                    statistics_error_count[this_key] = 0
                                if (error_is_larger_than_warn and value > data[this_key]['error']) or (not error_is_larger_than_warn and value < data[this_key]['error']):
                                    is_in_error_range = True
                                    statistics_error_count[this_key] += 1
                                    line += ' *** crossed error threshold %s *** ' % data[this_key]['error']

                            if 'warn' in data[this_key] and not is_in_error_range:
                                if this_key not in statistics_warn_count:
                                    statistics_warn_count[this_key] = 0
                                if (error_is_larger_than_warn and value > data[this_key]['warn']) or (not error_is_larger_than_warn and value < data[this_key]['warn']):
                                    statistics_warn_count[this_key] += 1
                                    line += ' *** crossed warn threshold %s *** ' % data[this_key]['warn']

                            if with_stats:
                                print line

                            if isinstance(value, int) or isinstance(value, float):
                                if this_key not in statistics_values:
                                    statistics_sample_count[this_key] = 1
                                    statistics_values[this_key] = (value,)
                                else:
                                    statistics_sample_count[this_key] += 1
                                    statistics_values[this_key] += (value,)

                        if interval is not None and len(stat_struct['data']) and with_stats:
                            print ""

                if not self.isStatStructStateOk(stat_struct):
                    ret_val = 1

                if interval is None:
                    break

                try:
                    # Sleep until the next interval.
                    # This is a little messy, but it works: if we're running on an integer interval,
                    # sleep a little less than needed, then sleep to roll forward to the next second.
                    # This keeps our interval on the "top" of the second, which is sorta nice.
                    run_time = time.time() - start_time
                    delta = interval - run_time
                    if whole_second_interval:
                        delta -= 0.05
                    if delta > 0:
                        time.sleep(delta)
                    if whole_second_interval:
                        _sleep_until_top_of_the_second(max_sleep=0.05)

                except Exception as e:
                    print >>sys.stderr, e
                    break

        except KeyboardInterrupt:
            pass

        # If we're running in a loop with full output, display averages:
        if interval is not None and key is None and len(statistics_values) and with_summary:
            print "\n--- %s statistics ---%s" % (self.getServiceName(), '-' * (80-len(self.getServiceName())))
            print "                    average      min val      max val      warnings     errors       sample count"
            for key in sorted(statistics_sample_count):
                avg_value = (sum(statistics_values[key])/statistics_sample_count[key])
                max_value = max(statistics_values[key])
                min_value = min(statistics_values[key])
                format_type = 'd'
                if isinstance(statistics_values[key][0], float):
                    format_type = 'f'
                else:
                    avg_value = int(round(avg_value))
                    max_value = int(round(max_value))
                    min_value = int(round(min_value))
                format_string = "{0:>17s}:  {1:<12%s} {2:<12%s} {3:<12%s} {4:<12s} {5:<12s} {6:s}" % (format_type, format_type, format_type)
                warn_info = '-'
                if key in statistics_warn_count:
                    warn_info = statistics_warn_count[key]
                err_info = '-'
                if key in statistics_error_count:
                    err_info = statistics_error_count[key]
                try:
                    print format_string.format(key, avg_value, min_value, max_value, str(warn_info), str(err_info), str(statistics_sample_count[key]))
                except Exception as e:
                    print >>sys.stderr, "(Error: can't print info for %s: %s)" % (key, e)
        return ret_val


    def shell_tool_debug_gdb(self, pid=None):
        """
        Attach gdb to the running server process (for linux).
         * pid: override pid to use
        """
        if pid is None:
            if not self.isServiceRunning():
                print >>sys.stderr, "Error: service not running."
                return 2
            pid = self.get_server_process_pid()
        if pid is None:
            print >>sys.stderr, "Error: no server pid."
            return 2
        cmd = self.which('lldb')  # OS X
        if cmd is None:
            cmd = self.which('gdb')
        if cmd is None:
            print >>sys.stderr, "Error: can't find gdb or lldb."
            return 2
        args = ("-p", pid)
        if 0 != os.getuid():
            args = (cmd,) + args
            cmd = self.which("sudo")
        print >>sys.stderr, "Running debugger:"
        print >>sys.stderr, "    %s %s" % (cmd, ' '.join(map(str, args)))
        print >>sys.stderr, "Tips:"
        print >>sys.stderr, "   backtrace"
        print >>sys.stderr, "   info threads"
        print >>sys.stderr, "   thread apply <threadnumber> backtrace"
        print >>sys.stderr, "   thread apply all backtrace"
        print >>sys.stderr, ""
        return self.execCommand(cmd, args=args)


    def shell_tool_telnet(self, host=None, port=None, use_ssl=False, code=None):
        ''' Connect via telnet to running service (will use local node where possible)
            * host: Connect to the given host, instead of auto-discovering it from settings
            * port: Connect to the given port, instead of auto-discovering it from settings
            * use_ssl: Connect with OpenSSL instead of telnet
            * code: connect and write given string; print response and then close connection
        '''
        port_var = "%s_PORT" % self.getServiceName().upper().replace('-','_')
        host_var = "%s_HOST" % self.getServiceName().upper().replace('-','_')
        hosts_var = "%s_HOSTS" % self.getServiceName().upper().replace('-','_')
        if port is not None:
            try:
                port = int(port)
                if port < 1 or port > 65535: raise
            except:
                print >>sys.stderr, "Error: invalid port %s." % port
                return 1
        if port is None:
            if port_var not in self._config:
                print >>sys.stderr, "Error: can't find setting %s for telnet connection." % port_var
                return 1
            port = self._config[port_var]
        if host is None:
            if host_var in self._config:
                host = self._config[host_var]
            elif hosts_var in self._config:
                service_hosts = self._config[hosts_var].split(',')
                if self._angel.get_private_ip_addr() in service_hosts:
                    host = self._angel.get_private_ip_addr()
                elif '127.0.0.1' in service_hosts:
                    host = '127.0.0.1'
                else:
                    host = sorted(service_hosts)[0]
                    print >>sys.stderr, "Warning: picking first host (%s) out of %s setting." % (host, hosts_var)
            else:
                print >>sys.stderr, "Warning: couldn't find host; assuming 127.0.0.1"
                host = '127.0.0.1'

        if use_ssl:
            if code:
                print >>sys.stderr, "Error: --code and --use-ssl not implemented."
                return 1
            print >>sys.stderr, "openssl s_client -crlf -connect  %s:%s" % (host, port)  # AWS ELB needs -crlf
            return self.execCommand(self.which('openssl'), args=('s_client', '-crlf', '-connect', '%s:%s' % (host, port)))
        else:
            if not code:
                print >>sys.stderr, "telnet %s %s" % (host, port)
                return self.execCommand(self.which('telnet'), args=(host, port))
            # Use a basic socket to connect and write the --code string:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect((host, port))
                s.sendall(code)
                while True:
                    data = s.recv(1024)
                    if 0 == len(data):
                        break
                    sys.stdout.write(data)
                    sys.stdout.flush()
                s.close()
                return 0
            except Exception as e:
                print >>sys.stderr, "Error connecting to %s:%s (%s)" % (e, host, port)
                return 1



    def shell_tool_rotate_logs(self):
        ''' Rotate log files '''
        return self.rotateLogs()


    def shell_tool_restart(self, wait=False, only_if_running=False):
        ''' Restart the service
            * wait: wait for service to start
            * only_if_running: only restart the service if it is already running
        '''
        if only_if_running:
            if not self.isServiceRunning():
                return 0
        ret_val = self.trigger_restart()
        if not wait or ret_val != 0:
            return ret_val
        return self.waitForOkayStatus(self.service_status, timeout_in_seconds=120)

    
    def shell_tool_reload(self, reload_code=False, flush_caches=False):
        ''' Reload the service for config changes without service interruption.
              * reload_code: Attempt to also reload code (for code changes)
              * flush_caches: Requests the service to flush any cached data when applicable
        '''
        is_conf_changed = True
        return self.service_reload(reload_code, is_conf_changed, flush_caches)


    def shell_tool_repair(self):
        ''' Attempt to correct service issues (e.g. failed supervisor or non-ok status state) '''
        return self.trigger_repair()


    def print_command_help(self, command):
        self.shell_tool_help(command=command, print_to_stderr=True)


    def shell_tool_help(self, command=None, print_to_stderr=False):
        ''' Show help text for tool commands.
             * command: tool to display help for
        '''
        # Display a notice to use the main help command, instead of tool help
        print >>sys.stderr, "*** deprecated; you should use:   %s help tool %s %s" % (self._angel.get_project_name(), self.getServiceName(), command or "")
        time.sleep(3)
        options = self.get_tool_names()
        out = sys.stdout
        if print_to_stderr:
            out = sys.stderr
        if command is not None:
            if command in options:
                print >>out, options[command].rstrip()
                return 0
            print >>sys.stderr, 'Unknown command "%s".\n' % command
            return 1
        print >>out, "tool %s ..." % self.getServiceName()
        for option in sorted(options):
            include_option = True
            for exclusion_pattern in self.HIDDEN_TOOLS:
                if fnmatch.fnmatch(option, exclusion_pattern):
                    include_option = False
                    break
            if include_option:
                if len(options[option]):
                    print >>out, options[option][:-1]
                else:
                    print >>out, "  %s   (unknown info)"   % option
        return 0


    def shell_tool_get_autocomplete_options(self):
        ''' Return a string containing all the valid tools commands, for use by bash autocomplete. This is sorta magic...'''
        options = self.get_tool_names()
        options_to_include = ()
        for option in sorted(options):
            include_option = True
            for exclusion_pattern in self.HIDDEN_TOOLS:
                if fnmatch.fnmatch(option, exclusion_pattern):
                    include_option = False
                    break
            if include_option:
                options_to_include += (option,)
        print ' '.join(options_to_include)
        return 0


    def get_usage_for_tools(self):
        ret_val = {"commands": {}}
        for tool in self.get_tool_names():
            ret_val["commands"][tool] = self.get_usage_for_tool(tool)
        return ret_val


    def get_usage_for_tool(self, tool_name):
        ret_val = {}

        for exclusion_pattern in self.HIDDEN_TOOLS:
            if fnmatch.fnmatch(tool_name, exclusion_pattern):
                ret_val["hidden"] = True

        try:
            tool_func = getattr(self, "shell_tool_%s" % tool_name.replace('-','_'))
        except AttributeError:
            ret_val["description"] = "(No description for tool %s; is it a bin-based one?" % tool_name
            return ret_val

        comment = tool_func.__doc__
        if comment is None:
            ret_val["description"] = "(No description for tool %s; check function docstring." % tool_name
            return ret_val

        # We expect the docstring to be formatted like so:
        #      '''
        #      Some description to show here
        #      Any additional info here does not get used
        #        * option: description of option
        #                  any additional info here also does not get used
        #        * option2: another descript
        #      '''

        first_line_of_comment = comment.lstrip().split('\n')[0].lstrip().rstrip()
        if first_line_of_comment.endswith('.'): first_line_of_comment = first_line_of_comment[:-1]  # Trim periods from sentences
        if first_line_of_comment.startswith('#'):
            first_line_of_comment = first_line_of_comment[1:].lstrip()
        ret_val["description"] = first_line_of_comment

        arg_lines = [ a.lstrip()[1:].lstrip().rstrip() for a in comment.split('\n') if a.lstrip().startswith('*') ]
        try:
            arg_info_from_doc = dict(arg.split(': ', 1) for arg in arg_lines)
        except ValueError:
            print >>sys.stderr, "Warning: unable to parse docstrings for tool %s." % tool_name

        argspec = inspect.getargspec(tool_func)
        if argspec.defaults is not None:
            ret_val['options'] = {}
            kwdefaults = zip( argspec.args[-len(argspec.defaults):], argspec.defaults )  # (required1,required2,optionalA,optionalB),(A,B) -> ((optionalA,A), (optionalB,B))
            for kwdefault_name, kwdefault_value in kwdefaults:
                description = "(Unknown description, check function docstring?)"
                try:
                    description = arg_info_from_doc[kwdefault_name]
                except:
                    pass
                ret_val['options']['--%s' % kwdefault_name.replace("_", "-")] = {
                    "description": description
                }

#                            if tool_kwarg.startswith('with_') and 'without_%s' % tool_kwarg[5:] in dict(kwdefaults):
#                                normalized_tool_kwargs['without_%s' % tool_kwarg[5:]] = not tool_kwargs[tool_kwarg]
#                            elif tool_kwarg.startswith('without_') and 'with_%s' % tool_kwarg[8:] in dict(kwdefaults):
#                                normalized_tool_kwargs['with_%s' % tool_kwarg[8:]] = not tool_kwargs[tool_kwarg]
#                            else:
        return ret_val


    def get_tool_names(self):
        ''' Return a list of all tools available for this service.
            This includes python functions defined in the class, as well as executable scripts in the appropriate bin directories.
            See ./services/README.txt for more info on naming requirements.
        '''
        right_column_indent = 32
        options = {}
        my_service_name = self.getServiceName()

        # Include <service-name>-<command-name> executables/scripts from our bin directory:
        our_bin_dir = os.path.join(self.getServiceCodeDir(), 'bin')
        if os.path.isdir(our_bin_dir):
            required_beginning = "%s-" % my_service_name
            for entry in os.listdir(our_bin_dir):
                if os.access(os.path.join(our_bin_dir, entry), os.X_OK) and entry[:len(required_beginning)] == required_beginning:
                    option_name = entry[len(required_beginning):]
                    options[option_name] = '  %s%s(usage unknown)\n' % (option_name, ' ' * (right_column_indent - 2 - len(option_name)))

        # Include executables from the server/bin directory:
        built_server_bin_dir = os.path.join(self.get_service_built_dir(), 'server', 'bin')
        server_bin_dir = os.path.join(self.getServiceCodeDir(), 'server', 'bin')
        for base in (server_bin_dir, built_server_bin_dir):
            if os.path.isdir(base):
                for entry in os.listdir(base):
                   if os.access(os.path.join(base, entry), os.X_OK):
                        options[entry] = '  %s%s(usage unknown)\n' % (entry, ' ' * (right_column_indent - 2 - len(entry)))

        # Find all functions that define shell_tool_<command>s:
        required_beginning = 'shell_tool_'
        for entry in dir(self):
            if entry[:len(required_beginning)] != required_beginning: continue
            if entry is 'shell_tool_get_autocomplete_options': continue
            if entry is 'shell_tool_help': continue
            option_name = entry[len(required_beginning):].replace('_','-')

            arg_info_from_doc = {}
            tool_func = getattr(self, entry)
            tool_options_info = '  %s%s(usage unknown)\n' % (option_name, ' ' * (right_column_indent - 2 - len(option_name)))

            comment = tool_func.__doc__
            if comment is not None:
                # We expect the comment to be formatted like so:
                #      '''
                #      Some description to show here
                #      Any additional info here does not get used
                #        * option: description of option
                #                  any additional info here also does not get used
                #        * option2: another descript
                #      '''
                first_line_of_comment = comment.lstrip().split('\n')[0].lstrip().rstrip()
                if first_line_of_comment.endswith('.'): first_line_of_comment = first_line_of_comment[:-1]  # Trim periods from sentences
                arg_lines = [ a.lstrip()[1:].lstrip().rstrip() for a in comment.split('\n') if a.lstrip().startswith('*') ]
                try:
                    arg_info_from_doc = dict(arg.split(': ', 1) for arg in arg_lines)
                except ValueError:
                    print >>sys.stderr, "Warning: unable to parse docstrings in %s." % option_name
                if first_line_of_comment.startswith('#'):
                    first_line_of_comment = first_line_of_comment[1:].lstrip()
                tool_options_info = '  %s%s%s\n' % (option_name, ' ' * (right_column_indent - 2 - len(option_name)), first_line_of_comment)

            argspec = inspect.getargspec(tool_func)
            optional_args = {}
            if argspec.defaults is not None:
                kwargs = sorted(zip( argspec.args[-len(argspec.defaults):], argspec.defaults ))  # (required1,required2,optionalA,optionalB),(A,B) -> ((optionalA,A), (optionalB,B))
                for kwdefault_name, kwdefault_value in kwargs:
                    optional_args[kwdefault_name] = kwdefault_value

            if argspec.args is not None:
                for arg_name in argspec.args:
                    if arg_name is 'self': continue
                    if arg_name in optional_args: continue
                    info = '(no doc string)'
                    if arg_name in arg_info_from_doc:
                        info = arg_info_from_doc[arg_name]
                    tool_options_info += '     <%s>%s  -- %s\n' % (arg_name.replace('_', '-'), ' ' * (right_column_indent - 7 - len(arg_name)), info)

            for kwdefault_name in sorted(optional_args):
                info = '(no doc string)'
                if kwdefault_name in arg_info_from_doc:
                    info = arg_info_from_doc[kwdefault_name]
                if optional_args[kwdefault_name] is False:
                    tool_options_info += '     [--%s]%s  -- %s\n' % (kwdefault_name.replace('_', '-'), ' ' * (right_column_indent - 9 - len(kwdefault_name)), info)
                elif optional_args[kwdefault_name] is None:
                    tool_options_info += '     [--%s <value>]%s  -- %s\n' % (kwdefault_name.replace('_', '-'), ' ' * (right_column_indent - 17 - len(kwdefault_name)), info)
                else:
                    indent = ' ' * (right_column_indent - 12 - len(kwdefault_name) - len(str(optional_args[kwdefault_name])))
                    tool_options_info += '     [--%s <%s>]%s  -- %s\n' % (kwdefault_name.replace('_', '-'), optional_args[kwdefault_name], indent, info)

            options[option_name] = tool_options_info

        options_to_delete = ()
        for disable_tool_pattern in self.DISABLED_TOOLS:
            for option in options:
                if fnmatch.fnmatch(option, disable_tool_pattern):
                    options_to_delete += (option,)
        for i in options_to_delete:
            del options[i]

        return options


    def reset_service_data_dir(self, confirm_ok=False, data_dir=None, post_reset_func=None):
        ''' If confirmed, and settings allow for it, stop (if running) the service, move data dir aside, and restart (if had been running) the service.
            If post_reset_func is defined, it will be called with a path to the old dir after the reset and before a potential service start (if service had been running). '''
        if not confirm_ok:
            print >>sys.stderr, "Error: missing --confirm-ok flag"
            return -1
        if not self._config['SYSTEM_RESET_DATA_ALLOWED']:
            print >>sys.stderr, "Error: refusing to run; SYSTEM_RESET_DATA_ALLOWED is set to false."
            return -2
        is_running = self.isServiceRunning()
        if data_dir is None:
            data_dir = self.get_service_data_dir()
        if not os.path.isdir(data_dir):
            print >>sys.stderr, "Warning: no data to reset; ignoring."
            return 0
        if is_running:
            print "Stopping %s..." % self.getServiceName(),
            ret_val = self.trigger_stop()
            if ret_val != 0:
                print >>sys.stderr, "Error: non-zero return %s while stopping %s." % (ret_val, self.getServiceName())
                return -4
            print "ok."
        old_data_dir = "%s-old-%s" % (data_dir, int(time.time()))
        try:
            os.rename(data_dir, old_data_dir)
        except Exception as e:
            print >>sys.stderr, "Error: unable to move %s -> %s: %s" % (data_dir, old_data_dir, e)
            return -5
        if post_reset_func:
            post_reset_func(old_data_dir)
        if is_running:
            print "Starting %s..." % self.getServiceName()
            ret_val = self.trigger_start()
            if ret_val != 0:
                print >>sys.stderr, "Error: non-zero while %s starting %s." % (ret_val, self.getServiceName())
                return -6
        return 0


    def execBeanshell(self, classpath=None):
        ''' Exec an interactive java shell (useful for debugging); or return non-zero error code. '''
        jar_path = os.path.join(os.path.expanduser(self._angel.get_project_base_dir()), 'share', 'tools', 'beanshell', 'bsh-2.0b4.jar')
        if not os.path.isfile(jar_path):
            return 1
        if classpath is None:
            classpath = jar_path
        else:
            classpath = jar_path + ':' + classpath
        return self.execCommand(self.which('java'), args=('bsh.Interpreter',), env={'CLASSPATH': classpath})
        


    def shell_tool_debug_show_disk_io(self, pid=None, interval=1):
        ''' Show disk I/O, per-process, for the running service.
            * pid: run against given pid instead of service
            * interval: seconds between stats
        '''
        if not sys.platform.startswith("linux"):
            print >>sys.stderr, "Error: not implemented for this platform."
            return 1
        if pid is None:
            pid = self.get_server_process_pid()
            if pid is None:
                print >>sys.stderr, "Error: can't find process id."
                return 1
            all_pids = get_all_children_of_process(pid)
            if all_pids is not None and len(all_pids) > 1:
                pid = ','.join(map(str,all_pids))
        args = ('-p', pid)
        args += ('-u', '-d')
        if interval > 0:
            args += (interval,)
        return self.execCommand(self.which('pidstat'), args=args)


    def shell_tool_debug_linux_netdown(self, duration=None):
        ''' Simulate net outage by dropping packets to settings.SERVICE_xxx_PORT[S] (linux only, root only)
            * duration: if given, limit the "network outage" for this many seconds
        '''

        if os.path.isfile(self._get_linux_netup_filename()):
            print >>sys.stderr, "Error: netdown already in effect for this service?"  # Could be a stale file from a reboot?
            return 1

        port_vars = [x for x in self._config if x.endswith('_PORT') and x.startswith('%s_' % self.getServiceNameConfStyle())]
        ports_vars = [x for x in self._config if x.endswith('_PORTS') and x.startswith('%s_' % self.getServiceNameConfStyle())]
        ports = [self._config[x] for x in port_vars]
        [ports.extend(self._config[x].split(',')) for x in ports_vars]

        try:
            map(int, ports)
        except:
            print >>sys.stderr, "Error: non-numeric port in ports list (%s)." % ports
            return 2

        if os.getuid() != 0:
            print >>sys.stderr, "Error: root access required."  # Maybe we should fall back on sudo iptables?
            return 3

        iptables = self.which('iptables', additional_paths=('/sbin',))
        if iptables is None:
            print >>sys.stderr, "Error: can't find iptables."
            return 4

        if duration is not None:
            try:
                duration = int(duration)
            except:
                print >>sys.stderr, "Error: invalid duration '%s'." % duration
                return 5
            if duration < 1 or duration > 60*60*24:
                print >>sys.stderr, "Error: invalid duration '%s' (too long or too short)." % duration
                return 6                
            import datetime
            duration = ' -m time --datestop %s' % (datetime.datetime.now() + datetime.timedelta(0, duration)).strftime("%Y-%m-%dT%H:%M:%S")
        else:
            duration = ''

        if 0 == len(ports):
            print >>sys.stderr, "Warning: no ports detected for the service."
        else:
            print "Blocking inbound traffic to: %s" % ', '.join(map(str,ports))

        add_rules =  ['%s -A INPUT -p tcp --destination-port %s -j DROP%s' % (iptables, port, duration) for port in ports]
        add_rules += ['%s -A INPUT -p udp --destination-port %s -j DROP%s' % (iptables, port, duration) for port in ports]

        remove_rules =  ['%s -D INPUT -p tcp --destination-port %s -j DROP%s' % (iptables, port, duration) for port in ports]
        remove_rules += ['%s -D INPUT -p udp --destination-port %s -j DROP%s' % (iptables, port, duration) for port in ports]

        try:
            open(self._get_linux_netup_filename(), 'a').write('\n'.join(remove_rules))
        except Exception as e:
            print >>sys.stderr, "Error: can't write to %s: %s" % (self._get_linux_netup_filename(), e)
            return 7
        ret_val = 0

        for rule in add_rules:
            if 0 != self.runCommand(rule):
                print >>sys.stderr, "Error: failed to run iptable command to add: %s" % rule
            ret_val = 1

        return ret_val


    def shell_tool_debug_linux_netup(self):
        ''' Remove previously-added iptable rules that are dropping packets (linux only, root only). '''
        if os.getuid() != 0:
            print >>sys.stderr, "Error: root access required."
            return 1
        if not os.path.isfile(self._get_linux_netup_filename()):
            print >>sys.stderr, "Warning: no rules for this service were found (missing %s)." % self._get_linux_netup_filename()
            return 0

        # We're not going to bother with locking on the netup file; it should be so rarely used.
        # Concurrent netup/netdowns on the same service will cause issues... don't do that.

        try:
            remove_rules = open(self._get_linux_netup_filename()).read().split('\n')
        except Exception as e:
            print  >>sys.stderr, "Error: can't read %s: %s" % (self._get_linux_netup_filename(), e)
            return 2
        ret_val = 0
        for rule in remove_rules:
            if not len(rule): continue
            if 0 != self.runCommand(rule):
                print >>sys.stderr, "Error: failed to run iptable command from %s: %s" % (self._get_linux_netup_filename(), rule)
                ret_val = 1
        if ret_val == 0:
            os.remove(self._get_linux_netup_filename())
        return ret_val


    def shell_tool_debug_get_server_pid(self):
        """Return the pid of the process being managed (e.g. the actual process, not the supervisor)."""
        pid = self.get_server_process_pid()
        if pid:
            print pid
            return 0
        print >>sys.stderr, "Error: no server process running."
        return 1

    def _get_linux_netup_filename(self):
        return '/.angel-netup-iptables-%s' % self.getServiceName().lower()


    def export_settings_to_tmp_file(self):
        '''Export settings to a tmp file and return the filename,
        using a checksum-based name so that we re-use files for exports of identical setting values.
        '''
        settings_as_string = self._config.export_settings_to_string()
        settings_checksum = angel.util.checksum.get_checksum(settings_as_string)[0:8]
        settings_filepath = os.path.join(os.path.expanduser(self._config['TMP_DIR']),
                                         '%s-settings-%s.conf' % (self._angel.get_project_name(), settings_checksum))
        if not os.path.exists(self._config['TMP_DIR']):
            try:
                os.makedirs(self._config['TMP_DIR'])
            except:
                print >>sys.stderr, "Error: tmp dir '%s' doesn't exist?" % self._config['TMP_DIR']
                return None
        if not os.path.isfile(settings_filepath):
            self._config.export_settings_to_file(settings_filepath)
        return settings_filepath


    def install_template_to_dir(self, src_path, dest_path,
                                owner_user=None, owner_group=None, mode=0755,
                                vars=None, delete_unknown_files=False):
        """Given a template path, copy files into dest path and replace tokens in the template files with values
        from our settings and vars.

        Tokens are expected to be of the form __PROJECT_SETTING_xxx__ and __PROJECT_VAR_xxx__, where "PROJECT" is the
        name of the project (from get_project_name()). PROJECT_VAR are extra vars, in a separate namespace from
        project settings.strings using config and vars.

        Returns the number of files that are changed (0 if no changes from previously filled-in files), or a negative number on error.

        When delete_unknown_files is set, removes any files under dest_path that are not present in src_path,
        which is useful in cases such as a code upgrade changes a conf filename.

        """

        token_prefix = self._angel.get_project_name().upper()

        if not os.path.isfile(src_path) and not os.path.isdir(src_path): # dirs are files, so this will work work with a src_dir, too.
            print >>sys.stderr, "Missing configuration template '%s'." % (src_path)
            return -1

        # To-do: figure out a more elegant solution for TOP_DIR, but for now, this gets us going.
        # Ideally, we'd use a normal template engine and pass an object to templates that can be queried / looped over.
        if vars is None:
            vars = {}
        if 'TOP_DIR' not in vars:
            vars['TOP_DIR'] = self._angel.get_project_base_dir()

        if os.path.isdir(src_path) and src_path[-1] == '/':
            src_path = src_path[:-1] # Get rid of trailing slashes, they result in double-slashes which causes problems

        if os.path.isdir(dest_path) and dest_path[-1] == '/':
            dest_path = dest_path[:-1] # Get rid of trailing slashes, they result in double-slashes which causes problems

        if os.path.islink(src_path):
            print >>sys.stderr, "Skipping symlink file %s in setting up template dir %s." % (src_path, dest_path)
            return 0 # We won't consider this a hard error -- it means there's a symlink in our src_dir, and we just don't support those.

        if os.path.isdir(src_path):
            # Then we have a directory to install, recurse through it.
            files_changed_count = 0
            # Check if there are unknown files under the dest path:
            if delete_unknown_files and os.path.exists(dest_path):
                for f in os.listdir(dest_path):
                    this_src_path = os.path.join(src_path, f)
                    this_dest_path = os.path.join(dest_path, f)
                    if not os.path.exists(this_src_path):
                        #pass
                        # We could warn or delete the file, but not doing this yet because there are some services that write files into the conf dir currently:
                        print >>sys.stderr, "Warning: template file %s exists in run-time conf dir at %s but does not exist in src template dir (no file at %s)." % (f, this_dest_path, this_dest_path)
                        # os.remove(this_dest_path)
                        # files_changed_count++
            # For each file in the src conf dir, recurse through and add it to dest path:
            for f in os.listdir(src_path):
                this_src_path = os.path.join(src_path, f)
                this_dest_path = os.path.join(dest_path, f)
                ret_val = self.install_template_to_dir(this_src_path, this_dest_path,
                                                       owner_user=owner_user, owner_group=owner_group, mode=mode,
                                                       vars=vars, delete_unknown_files=delete_unknown_files)
                if ret_val < 0:
                    return ret_val
                files_changed_count += ret_val
            return files_changed_count

        if mode is None:
            mode = stat.S_IMODE(os.stat(src_path).st_mode)  # Then fall back on the mode of the original file

        if 0 != create_dirs_if_needed(os.path.dirname(dest_path), name='config', owner_user=owner_user, owner_group=owner_group, mode=mode):
            print >>sys.stderr, "Can't create config dir for file '%s'." % dest_path
            return -1

        if src_path.endswith(".pyc") or src_path.endswith(".pyo"):
            print >>sys.stderr, "Warning: .pyc/.pyo file in template src dir %s; skipping file." % os.path.dirname(src_path)
            return 0

        try:
            with open(src_path, 'r') as fd:
                new_data = fd.read()

            # Replace __<PROJECT>_SETTING_<NAME>__ with value from settings:
            for key in self._config:
                new_data = string.replace(new_data, '__%s_SETTING_%s__' % (token_prefix, key), str(self._config[key]))

            # Replace __<PROJECT>_VAR_<NAME>__ with value from vars:
            if vars is not None:
                for key in vars:
                    new_data = string.replace(new_data, '__%s_VAR_%s__' % (token_prefix, key), str(vars[key]))

            if os.path.isdir(dest_path):
                dest_path = os.path.join(dest_path, os.path.basename(src_path))

            unexpanded_token_offset = new_data.find('__%s_' % token_prefix)
            line_count = len(new_data[0:unexpanded_token_offset].split("\n"))
            if unexpanded_token_offset > 0:
                print >>sys.stderr, "Error: undefined token (%s) found on line %s of template %s:" % \
                    (', '.join(re.findall('__%s_(.*?)__' % token_prefix, new_data)), line_count, src_path)
                print >>sys.stderr, new_data.split('\n')[line_count-1] #[unexpanded_token_offset:(unexpanded_token_offset+64)]
                if os.path.isfile(dest_path):
                    # There can be an old, stale file -- remove it, otherwise it's really confussing in debugging the error.
                    try:
                        os.remove(dest_path)
                    except:
                        pass
                return -1

            if os.path.islink(dest_path):
                print >>sys.stderr, "Warning: removing symlink %s during template installation" % (dest_path)
                os.unlink(dest_path)

            old_data = ''
            if os.path.isfile(dest_path):
                with open(dest_path, 'r') as fd:
                    old_data = fd.read()

            if old_data == new_data and os.path.isfile(dest_path):  # Need to check if dest_path exists here; otherwise 0-length files won't actually get created
                return 0

            fd = open(dest_path, 'w')
            fd.write(new_data)
            fd.close

            if owner_user is not None or owner_group is not None:
                if 0 != set_file_owner(dest_path, owner_user=owner_user, owner_group=owner_group):
                    print >>sys.stderr, "Error: unable to set file owner (%s to %s/%s)" % (dest_path, owner_user, owner_group)
                    return -1

            if os.access(src_path, os.X_OK):
                os.chmod(dest_path, (os.stat(dest_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

            return 1

        except Exception as e:
            print >>sys.stderr, "Error while installing template: unable to read from %s or write to %s (%s)." % (src_path, dest_path, e)
            raise e
            return -1

        print >>sys.stderr, "Error: impossible condition in file_and_dir_helpers.py"
        return -1


