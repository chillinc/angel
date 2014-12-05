
import imp
import os
import pwd
import re
import signal
import shutil
import string
import sys
import traceback
import time
from stat import ST_CTIME
from multiprocessing import Pool

import angel.settings
from devops.unix_helpers import set_proc_title, hard_kill_all
import devops.process_helpers
import devops.file_and_dir_helpers
from devops.monitoring import run_status_check
from devops.logging import log_to_syslog



def set_maintenance_mode(config, run_in_maintenance_mode):
    ''' When in maintenance mode, no public access to the site should be permitted. '''
    lockfile_path = '%s/.maintenance_mode_lock' % config['DATA_DIR']  # Use data dir so that restarts / resets don't lose state
    if run_in_maintenance_mode:
        if os.path.exists(lockfile_path): return 0
        try:
            open(lockfile_path, 'w').close()
        except:
            print >>sys.stderr, "Error: unable to create maintenance mode lockfile."
            return 1
    else:
        if not os.path.exists(lockfile_path): return 0
        try:
            os.remove(lockfile_path)
        except:
            print >>sys.stderr, "Error: unable to remove maintenance mode lockfile."
            return 1

    ret_val = 0
    if are_services_running(config):
        run_in_parallel = True
        if run_in_maintenance_mode:
            (ret_val, dummy) = _run_verb_on_services(get_enabled_or_running_service_objects(config), 'switchToMaintenanceMode', run_in_parallel)
        else:
            (ret_val, dummy) = _run_verb_on_services(get_enabled_or_running_service_objects(config), 'switchToRegularMode', run_in_parallel)
    else:
        print >>sys.stderr, "Warning: services not running."
    return ret_val




def set_service_state(config, state_constant):
    state_file = _get_state_file_path(config)
    tmp_state_file = '%s-%s.tmp' % (state_file, int(time.time()))
    if state_constant is None:
        print >>sys.stderr, " ********** ERROR? set_service_state given a None state_constant? "
        try:
            if os.path.isfile(state_file):
                os.remove(state_file)
        except:
            print >>sys.stderr, "Error: unable to remove state file '%s'." % state_file
            return -1
        return 0
    try:
        open(tmp_state_file, 'wt').write( "%s\n%s" % (state_constant, time.time()) )
        os.rename(tmp_state_file, state_file)
    except Exception as e:
        print >>sys.stderr, "Error writing state file '%s': %s" % (state_file, e)
        return -1
    return 0


def _get_state_file_path(config):
    return config['LOCK_DIR'] + '/service_state.lock'


def are_services_status_ok(config, accept_warn_as_ok=True, wait_for_ok=False, timeout=600):
    if timeout > 60*60:
        print >>sys.stderr, 'Warning: wait timeout given invalid value "%s", using 60 minutes.' % timeout
        timeout_in_seconds=60*60
    current_state = run_status_check(config, do_all_checks=True, format="silent")
    if current_state == angel.constants.STATE_RUNNING_OK:
        return True
    if accept_warn_as_ok and current_state == angel.constants.STATE_WARN:
        return True
    if wait_for_ok and timeout > 0:
        try:
            time.sleep(3)
            return are_services_status_ok(config, accept_warn_as_ok=accept_warn_as_ok, wait_for_ok=wait_for_ok, timeout=(timeout-3))
        except KeyboardInterrupt:
            print >>sys.stderr, "Returning early (services not yet ok; ctrl-c abort)"
    return False
    

def get_service_names(config):
    ''' Given a config, auto-discover all the services by looking for SERVICE_xxx values. '''
    names = []
    for key in config:
        if key[-8:] != '_SERVICE': continue
        service_name = key[:-8].lower()
        names.append(service_name)
    return names


def are_services_running(config):
    ''' Return True if services are supposed to be running (i.e. 'service start' has been called); False otherwise. '''
    current_state, last_change_time = get_service_state(config)
    if current_state is None or current_state == angel.constants.STATE_STOPPED:
        return False

    # If services aren't stopped, then make sure that the last change time is newer than our uptime.
    # We do this so that we start up correctly if a node gets hard-rebooted.

    uptime = None
    if sys.platform == 'darwin':
        out, err, exitcode = devops.process_helpers.get_command_output("sysctl -n kern.boottime | awk '{print $4}' | sed -e 's:\,::'")
        if exitcode == 0:
            boottime = int(out)
            uptime = time.time() - boottime
            if uptime < 0: uptime = None
            if uptime > 365*24*60*60: uptime = None
        else:
            print >>sys.stderr, "Failed to get uptime in OS X logic"
    else:
        if os.path.exists("/proc/uptime"):
            uptime, dummy = [float(field) for field in open("/proc/uptime").read().split()]
        else:
            print >>sys.stderr, "Failed to get uptime"

    if uptime is None or last_change_time < 1000000000:
        print >>sys.stderr, "Warning: can't figure out uptime or last_change_time in call to are_services_running"
        return True  # Can't get uptime, assume that we didn't crash...

    state_age = time.time() - last_change_time
    if state_age > uptime:
        state_file = _get_state_file_path(config)
        if not os.path.exists(state_file):
            print >>sys.stderr, "Error: service uptime check missing state file?!"
        else:
            print >>sys.stderr, "Warning: uptime is less than service statefile's last change time; assuming machine hard crashed and rebooted; clearing old statefile."
            os.rename(state_file, "%s-stale-uptime-%s" % (state_file, int(time.time())))
        return False

    return True


def get_supervisor_lockdir(config):
    return '%s/supervisor' % config['LOCK_DIR']


def get_supervisor_lockpath(config, service_name):
    ''' Supervisor lock files need to be named such that we can back out the class name of the service based on the filename. '''
    super_lock_dir = get_supervisor_lockdir(config)
    filename = '_'.join(re.findall('[A-Z][^A-Z]*', service_name.replace('Service',''))).lower() # RedisReadonlyService -> redis_readonly
    if not os.path.isdir(super_lock_dir): 
        if 0 != devops.file_and_dir_helpers.create_dirs_if_needed(super_lock_dir, name='service lockdir', recursive_fix_owner=True, owner_user=config['RUN_AS_USER'], owner_group=config['RUN_AS_GROUP']):
            print >>sys.stderr, "Error: can't make lock subdir for services."
    return os.path.join(super_lock_dir, '%s.lock' % filename)


def get_running_service_names(config):
    ''' Return a list of all currently-running services. '''
    services = []
    super_lock_dir = get_supervisor_lockdir(config)
    if not os.path.isdir(super_lock_dir):
        return services
    lockfiles = os.listdir(super_lock_dir)
    for lockfile in lockfiles:
        if lockfile[-5:] == '.lock':
            services += [ lockfile[:-5], ]
    return services


class _HelperRunsVerbOnAService:

    _verb = None
    _timeout = None
    _args = ()
    _kwargs = {}

    def __init__(self, verb, args, kwargs, timeout):
        self._verb = verb
        if args is not None:
            self._args = args
        if kwargs is not None:
            self._kwargs = kwargs
        if isinstance(timeout, int) and timeout > 0:
            self._timeout = timeout

    def __call__(self, service):
        if service is None:
            print >>sys.stderr, 'Error: null service object'
            return -1

        class TimeoutAlarm(Exception):
            pass

        def timeout_alarm_handler(signum, frame):
            #print >>sys.stderr, "TIMEOUT in %s" % service
            raise TimeoutAlarm

        ret_val = None
        if self._timeout:
            old_sigalarm = signal.signal(signal.SIGALRM, timeout_alarm_handler)
            signal.alarm(self._timeout)

        name = str(service.getServiceName())
        try:
            set_proc_title('service %s: %s' % (name, self._verb))
        except:
            pass

        result = ': unknown'
        try:
            the_method = getattr(service, self._verb)
            ret_val = the_method(*self._args, **self._kwargs)
            result = ''
        except TimeoutAlarm:
            print >>sys.stderr, "Error: %s.%s() failed to return within %s seconds" % (service.__class__.__name__, self._verb, self._timeout)
            result = ': timeout'
        except SystemExit as e:
            print >>sys.stderr, "System exit from %s.%s()" % (service.__class__, service.__class__.__name__) # Some processes call sys.exit() -- things like redis-primer fork, run, then exit, which causes an exception here
            sys.exit(0)  # Exit here as well -- we're inside a pool, so the return above us will just generate a confussing stack trace and then exit anyway...
        except Exception as e:
            print >>sys.stderr, 'Error: unexpected exception during call in %s:\n%s' % (service.__class__.__name__, traceback.format_exc(e))
            result = ': exception'
        except:
            print >>sys.stderr, 'Error: unexpected %s error during call in %s:\n%s' % (sys.exc_info()[0], service.__class__.__name__, traceback.format_exc(sys.exc_info()[2]))
            result = ': error'

        try:
            set_proc_title('service %s: %s [Z%s]' % (self._verb, name, result))  # Z is for zombie... because with process pooling, this process is just waiting to be reaped... most likely.
        except:
            pass

        if self._timeout:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_sigalarm)

        return ret_val


def _get_enabled_or_running_service_names(config):
    ''' Return a list of names of the services that are currently running or should be active (as defined in the conf) for this node. '''
    return list(set( get_running_service_names(config) + config['.NODE_ENABLED_SERVICES'] ))


def get_enabled_or_running_service_objects(config):
    ''' Return a list of classes for services that are currently running or should be active (as defined in the conf) for this node. '''
    return _get_service_objects_by_name(_get_service_objects(config), _get_enabled_or_running_service_names(config))


def _get_service_objects(config):
    ''' Return a map of ServiceName->class objects by auto-discovering services in config (SERVICE_xxx) and loading the associated python class. '''
    service_objects = {}
    for service_name in get_service_names(config):
        service_objects[service_name] = get_service_object_by_name(service_name, config)
    return service_objects


def get_service_object_by_name(service_name, config):
    ''' Given the name of a service, find the .py file that defines it and return an instantiated instance of the class.
        The service should be defined in a file under ./services/<name>/<name>_service.py.
        E.g.:
            redis is loaded from class RedisService in ./services/redis/redis_service.py
        We support subclassing services with an '_' in the name, like this:
            redis_readonly is loaded from class RedisReadonlyService
        Subclassed services are loaded from three possible file paths, most-specific first, like this:
                     ./services/redis_readonly/redis_readonly_service.py
                     ./services/redis/redis_readonly_service.py
                     ./services/redis/redis_service.py

        Returns None on error.
    '''

    try:
        service_name = service_name.replace('-','_') # command-line utils use hypens; swap to underscores here so that python paths for subclassed services work
        parent_service_name = None
        service_classname = service_name.capitalize() + 'Service'

        if service_name.find('_') > 0:
            parent_service_name = service_name[0:service_name.find('_')]
            service_classname = parent_service_name.capitalize() + service_name[service_name.find('_')+1:].capitalize() + 'Service'

        service_dir = os.path.join(config['TOP_DIR'], 'services', service_name)
        if not os.path.isdir(service_dir) and parent_service_name is not None:
            service_dir = os.path.join(config['TOP_DIR'], 'services', parent_service_name)

        service_py_filename = '%s_service.py' % service_name
        src_path = os.path.join(service_dir, service_py_filename)
        if not os.path.isfile(src_path) and parent_service_name is not None:
            service_py_filename = service_py_filename = '%s_service.py' % parent_service_name
            src_path = os.path.join(service_dir, service_py_filename)

        if not os.path.exists(src_path):
            print >>sys.stderr, "Error: Can't find service class for service '%s'." % (service_name)
            return None

        if service_dir not in sys.path:
            # Add the service_dir to the path, so that services can do relative imports -- needed for parent imports as well as services that define any sort of common stuff:
            sys.path.insert(0,service_dir)

        service_module = None
        if os.path.exists(src_path + 'c'):
            try:
                service_module = imp.load_compiled(service_classname, src_path + 'c')
            except Exception as e:
                print >>sys.stderr, "Error: failed to load %sc: %s" % (src_path, e) # Hint: if the .pyc was compiled with a different version of python, you'll get a "Bad Magic Number" error.
        if service_module is None:
            service_module = imp.load_source(service_classname, src_path)

        service_class = getattr(service_module, service_classname)

    except (KeyError, ImportError, AttributeError) as e:
        print >>sys.stderr, "Error: unable to load service '%s': %s" % (service_name, e)
        return None

    try:
        return service_class(config)
    except Exception as e:
        print >>sys.stderr, "Error: unable to instantiate service '%s':\n%s" % (service_name, traceback.format_exc(e))
    return None


def _get_service_objects_by_name(service_objects, service_names):
    if service_names is None:
        print >>sys.stderr, 'Warning: service_names is None!'
        return {}
    def service_name_to_service_object(service_name):
        if service_name not in service_objects:
            print >>sys.stderr, "Error: unable to find service class for service '%s'. (Is %s_SERVICE defined?)" % (service_name, service_name.upper())
            return None
        return service_objects[service_name]
    return map(service_name_to_service_object, service_names)


def _run_verb_on_services(services, verb, run_in_parallel, args=None, kwargs=None, timeout=None):
    ''' Given a list of objects and the name of a function, run obj.function()
        Function should return either an int or a dictionary with a key 'state'.
        Returns two values: first value is an int, 0 if all calls succeeded, 1 if any call returned non-zero;
                            second value is an array of all the returns from each service's verb call
    '''

    if len(services) == 0:
        print >>sys.stderr, 'Warning: no services supplied, %s command will have no affect.' % str(verb)
        return 0, None

    if verb is not 'trigger_status':
        log_to_syslog('calling services.%s on following services: %s' % (verb, ', '.join(map(lambda a: a.__class__.__name__.replace("Service",""),services))))

    if len(services) == 1:
        run_in_parallel = False

    return_values = None

    if run_in_parallel:
        def _keyboard_interrupt_handler(signum, frame):
            print >>sys.stderr, "Warning: ctrl-c ignored during %s" % verb
            return
        old_sigint_handler = signal.signal(signal.SIGINT, _keyboard_interrupt_handler)

        try:
            pool = None
            try:
                pool = Pool(len(services))
            except OSError: # Trap this: [Errno 12] Cannot allocate memory
                print >>sys.stderr, "Can't create service pool; out of memory?"
                return 1, None

            try:
                return_values = pool.map(_HelperRunsVerbOnAService(verb, args, kwargs, timeout), services)
            except Exception as e:
                print >>sys.stderr, "Error: %s command got exception '%s'" % (verb, str(e))
                # print >>sys.stderr, traceback.format_exc(e) Don't bother doing this, it's a strack trace from the wrong pool process...

            if return_values is None:
                return 1, None

        finally:
            signal.signal(signal.SIGINT, old_sigint_handler)


    else:
        return_values = map(_HelperRunsVerbOnAService(verb, args, kwargs, timeout), services)

    # The order of values in return_values matches the order of values in services array.
    # For convenience, create a dict that's key -> value based and return that instead.
    return_dict = {}
    for i in range(len(services)):
        return_dict[ services[i].__class__.__name__ ] = return_values[i]

    # Do a big "OR" on the return-values to see if any service returned non-zero:
    services_with_errors = ()
    for name in return_dict:
        val = return_dict[name]
        if isinstance(val, int):
            if val != 0:
                services_with_errors += (name,)
        elif isinstance(val, dict):
            if val.has_key('state'):
                if val['state'] != angel.constants.STATE_RUNNING_OK:
                    services_with_errors += (name,)
            else:
               print >>sys.stderr, "Warning: service %s returned a dict without a 'state' key while running %s()" % (name, verb)
               services_with_errors += (name,)

        else:  # if it's not a dict or int, something failed.
            print >>sys.stderr, "Warning: service %s returned nothing while running %s()" % (name, verb)
            services_with_errors += (name,)

    if 0 == len(services_with_errors):
        return 0, return_dict

    if ('trigger_stop' == verb):
        print >>sys.stderr, "Warning: the following services may not have stopped correctly: %s" % (' '.join(services_with_errors))
        # Not sure this is true any longer, testing:  return 0, return_dict # "Stop" must always succeed, otherwise package upgrades will fail.

    return 1, return_dict




def service_start(config, timeout=None):
    ''' Start services.
        Return 0 on success, -1 if any service failes to start; -2 if services fail to show OK status within timeout seconds.
    '''

    # If services are already started, we'll check conf for any additional services that might be new and need starting;
    # so okay to continue.
    if not are_services_running(config):
        set_service_state(config, angel.constants.STATE_STARTING)

    # It's possible to start a service manually, using the "tool" command; so ignore ones that are already running:
    already_running_services = get_running_service_names(config)
    enabled_services = config['.NODE_ENABLED_SERVICES']
    services_to_start = [n for n in enabled_services if n not in already_running_services]

    if config['.NODE_ENABLED_SERVICES'] == []:
        print >>sys.stderr, 'Warning: no services are enabled.'

    if len(services_to_start) == 0:
        return 0

    ret_val = _run_verb_on_services(_get_service_objects_by_name(_get_service_objects(config), services_to_start), 'trigger_start', True)[0]

    set_service_state(config, angel.constants.STATE_RUNNING_OK)

    if ret_val != 0:
        return -1
    if timeout is not None:
        if not are_services_status_ok(config, wait_for_ok=True, timeout=timeout):
            print >>sys.stderr, "Error: not all services started within %s seconds" % (timeout)
            return -2
    return 0


def service_stop(config, hard_stop=False):
    ''' Stop services (if running); return 0 on success, non-zero otherwise. '''

    if hard_stop:
        # hard_kill_all will terminate all processes listed in lockfiles, plus any descendant processes.
        # We'll call the traditional stop() logic after this, which will do any finalization that's necessary.
        hard_kill_all(get_supervisor_lockdir(config), yes_i_understand_this_is_potentially_dangerous=True)

    running_service_names = get_running_service_names(config)
    if not are_services_running(config):
        if 0 == len(running_service_names):
            print >>sys.stderr, "Ignoring stop request; services aren't running."
            return 0
        print >>sys.stderr, "Warning: services are stopped, but some services (%s) are running, stopping them now." % ', '.join(running_service_names)
    else:
        set_service_state(config, angel.constants.STATE_STOPPING)
    if not len(running_service_names):
        print >>sys.stderr, "Warning: stopping services, but no services were running."
        ret_val = 0
    else:
        ret_val = _run_verb_on_services(_get_service_objects_by_name(_get_service_objects(config), running_service_names), 'trigger_stop', True)[0] # Note: 'True' means stop in parallel
    set_service_state(config, angel.constants.STATE_STOPPED)
    # List any processes that are still running, as a visibility safety-check -- should be empty:
    if os.getuid() != pwd.getpwnam(config['RUN_AS_USER']).pw_uid:
        os.system('ps -o pid,args -U "%s" | tail -n +2 | grep -v collectd' % (config['RUN_AS_USER']))
    clear_tmp_dir(config)
    return ret_val


def service_restart(config, timeout=None):
    ''' Restart services; return 0 on success; non-zero otherwise. '''
    ret_val = service_stop(config)
    if ret_val != 0:
        print >>sys.stderr, "Warning: at least one service reported an error during stop."
    if are_services_running(config):
        service_stop(config)                   # Will stop services listed in running_services
    ret_val = service_start(config)            # Will start services listed in our config
    if ret_val != 0:
        return ret_val
    if timeout is not None:
        if not are_services_status_ok(config, wait_for_ok=True, timeout=timeout):
            print >>sys.stderr, "Error: not all services started within %s seconds" % (timeout)
            return 1
    return 0


def service_repair(config):
    ''' Calls repair() on all valid running services, which will in turn call service_repair() on services with status other than ok/warn.
        We also call start on services that should be running and stop on services that shoudln't be running.
        Does nothing when services are stopped.
        Returns 0 on success, non-zero on error. '''
    if not are_services_running(config):
        return 0
    # Check what services might be added (start them), deleted (stop them), or already running (reload/repair them).
    conf_services = config['.NODE_ENABLED_SERVICES']
    running_services = get_running_service_names(config)
    running_and_in_conf_services = list(set(running_services).intersection(set(conf_services)))
    running_but_not_in_conf_services = list(set(running_services).difference(set(conf_services)))
    in_conf_but_not_running_services = list(set(conf_services).difference(set(running_services)))
    if 'devops' in in_conf_but_not_running_services: in_conf_but_not_running_services.remove('devops') # devops doesn't actually run, inelegant solution for now.
    errors_seen = 0
    run_in_parallel = True
    service_classes = _get_service_objects(config)
    if len(running_but_not_in_conf_services):
        print >>sys.stderr, "Repair: stopping services: %s" % ', '.join(running_but_not_in_conf_services)
        if 0 != _run_verb_on_services(_get_service_objects_by_name(service_classes, running_but_not_in_conf_services), 'trigger_stop', run_in_parallel)[0]:
            errors_seen += 1
    if len(in_conf_but_not_running_services):
        print >>sys.stderr, "Repair: starting services: %s" % ', '.join(in_conf_but_not_running_services)
        if 0 != _run_verb_on_services(_get_service_objects_by_name(service_classes, in_conf_but_not_running_services), 'trigger_start', run_in_parallel)[0]:
            errors_seen += 1
    if len(running_and_in_conf_services):
        if 0 != _run_verb_on_services(_get_service_objects_by_name(service_classes, running_and_in_conf_services), 'trigger_repair', run_in_parallel)[0]:
            errors_seen += 1
    return errors_seen


def service_status(config, services_to_check=None, format=None, timeout=13, run_in_parallel=True):
    ''' Check running or enabled services; or, if services_to_check is not None, the listed services. '''
    if services_to_check is None:
        # When checking all services, include devops service so we include system-level status and warnings.
        # This should get migrated out into some place cleaner.
        services_to_check = ['devops',]
        if are_services_running(config):
            services_to_check += _get_enabled_or_running_service_names(config)
        else:
            # We can be "stopped" but have individual services manually started on us:
            services_to_check += get_running_service_names(config)
    services_objs_to_check = _get_service_objects_by_name(_get_service_objects(config), services_to_check)
    return _run_verb_on_services(services_objs_to_check, 'trigger_status', run_in_parallel, timeout=timeout)


def service_reload(config, reload_code=True, reload_conf=True, flush_caches_requested=False):
    ''' Trigger service_reload() on all running services.
        reload_code: true if the application code has been changed
        reload_conf: true if the conf for the app has been changed
        flush_caches_requested: true if data for the system has been changed, i.e. DB reset, such that services that cache data might want to reset their caches
    '''
    # To-do: reload is called after an upgrade; an upgrade might have added new services to us; or disabled a service.
    # reload currently doesn't start or stop them correctly.
    # We probably need to track if services were started automatically or via tool command; and then based on that
    # auto-stop the auto-started ones; and auto-start new ones.
    service_classes = _get_service_objects(config)
    running_services = get_running_service_names(config)
    if len(running_services) == 0:
        if are_services_running(config):
            print >>sys.stderr, "Warning: no services to reload."
        else:
            print >>sys.stderr, "Warning: services are stopped; nothing to reload."
        return 0
    run_in_parallel = True
    _run_verb_on_services(_get_service_objects_by_name(service_classes, running_services), 'trigger_reload', run_in_parallel, args=(reload_code, reload_conf, flush_caches_requested))[0]


def service_rotate_logs(config):
    return _service_run_verb_on_all_services(config, 'rotateLogs')


def service_decommission_precheck(config):
    return _service_run_verb_on_all_services(config, 'decommission_precheck')


def service_decommission(config):
    return _service_run_verb_on_all_services(config, 'decommission')


def _service_run_verb_on_all_services(config, verb, run_in_parallel=True):
    all_services = get_service_names(config)
    service_classes = _get_service_objects(config)
    return _run_verb_on_services(_get_service_objects_by_name(service_classes, all_services), verb, run_in_parallel)[0]
