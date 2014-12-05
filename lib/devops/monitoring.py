# Monitoring provides basic "is it up?" insight, along with performance data about how an installation is running.
# Note: Do NOT return a 'error' state when a 'warning' state would do. 
# The system is coded to block on errors during starts / restarts. So, if the number of 
# messages in a queue is astronomically high, but the system is still running, that is a warning.
# If an error is returned in this state, then on an upgrade to fix whatever the issue is will block and fail.

import copy
import datetime
import re
import socket
import string
import sys
import time
import traceback

import angel.util.terminal
import angel.util.network
import angel.stats.mem_stats
import angel.settings
import angel.constants

from devops.stats import *
from devops.unix_helpers import set_proc_title



def run_status_check(angel_obj, do_all_checks=False, do_state_checks=False, do_service_checks=False, check_only_these_services=None, format=None, interval=None, timeout=None):

    ''' Performs various status checks on the running system.
          do_all_checks: flip this on to make sure all checks are run, so that in the future as we add additional check flags, they'll default on.
          do_state_checks: check that the running services match what should be configured
          do_service_checks: call status() on each running service, gathering health and performance data
             check_only_these_services: if defined, and do_service_checks is true, only inspect the named services

        * Note that checks that this function runs are expected to complete quickly and run as efficiently as possible;
        * this function is run in a continuous loop by collectd and polled by nagios on every node in production.
        * Please take care when adding any additional logic that it is as efficient as possible!

        format:
            "" / None     -- default action is to print human-readable status info
            "collectd"    -- run in continuous mode for collectd with given interval (defaults to 10)
            "nagios"      -- output nagios-formatted output and return a valid nagios exit code
            "errors-only" -- display only error info; return non-zero if errors or unknown state
            "silent"      -- don't output anything; just return an exit code

    '''

    if do_all_checks:
        do_state_checks = do_service_checks = True
    if interval is None:
        interval = 10  # Used only in collectd currently
    if format == '':
        format = None
    if timeout is None:
        if format is None:
            timeout = 10  # Most likely a command-line user
        else:
            timeout = 14  # Nagios nrpe is set to 15 seconds

    if format == 'collectd':
        try:
            run_collectd_monitor(angel_obj, check_only_these_services, interval)  # Will only return once services are stopped
            if angel_obj.are_services_running():
                print >>sys.stderr, "Error: run_collectd_monitor() unexpectedly returned!"
                sys.exit(1)
            sys.exit(0)
        except KeyboardInterrupt:
            sys.exit(0)
        except Exception as e:
            print >>sys.stderr, "Error: run_collectd_monitor thew an exception(%s)." % e
            sys.exit(1)


    # For all other formats, we'll query status and generate output in the requested format.
    # This function could use some clean-up / refactoring, but conceptually it's simple:
    # 1) set up some common variables; 2) call status_check on all services; 3) generate the output.


    # To-do: there's some odd rare network condition that causes a ~30 second delay in the following 3 lines
    # even when services are stopped -- presumably hostname lookup stuff when DNS is unresolvable?
    # Wasn't able to trace it further than this before networking resumed; so leaving this note here for now.
    services_are_running = angel_obj.are_services_running()
    running_services = sorted(angel_obj.get_running_service_names())
    enabled_services = sorted(angel_obj.get_enabled_services())

    running_unexpectedly = list(set(running_services) - set(enabled_services))
    if not services_are_running:
        running_unexpectedly = running_services
    not_running_but_should_be = list(set(enabled_services) - set(running_services))
    if 'devops' in not_running_but_should_be:
        not_running_but_should_be.remove('devops')

    left_column_width = 10
    if len(running_services):
        # Find the length of the longest service name:
        left_column_width = max(left_column_width, 1 + max(map(len, running_services)))

    # Default format (usually command line user) prints some info before checking each service status:
    if format is None and do_state_checks:
        _print_status_preamble(angel_obj, left_column_width)
        if len(running_services) and do_service_checks:
            print "-" * angel.util.terminal.terminal_width()


    # Gather data for each service by calling their status() functions:
    time_exceeded = False
    stat_structs = {}
    if do_service_checks:
        start_time = time.time()
        unused_ret_val, stat_structs = angel_obj.service_status(services_to_check=check_only_these_services, timeout=timeout)
        end_time = time.time()
        check_time = end_time - start_time
        if check_time > timeout:
            time_exceeded = True
        if stat_structs is None:
            print >>sys.stderr, "Error: service status struct invalid"
            return angel.constants.STATE_UNKNOWN

    # Run through the data for each status, checking it:
    service_info = {}
    status_seen_by_type = {}
    status_data = {}
    state_message = ''
    if do_state_checks:
        state_message = "%s %s" % (angel_obj.get_project_code_branch(), angel_obj.get_project_code_version())
        if format == 'nagios':
            if angel_obj.is_multinode_install() or True:
                public_ip = angel_obj.get_public_ip_addr()
                private_ip = angel_obj.get_private_ip_addr()
                if private_ip != public_ip:
                    state_message += " on " + public_ip

    def _merge_status_data(key_prefix, new_status_data):
        for k in new_status_data:
            new_key = "%s_%s" % (key_prefix, k)
            if new_key in status_data:
                print >>sys.stderr, "Warning: %s already in status_data?" % new_key
            status_data[new_key] = new_status_data[k]

    # Run through the results for each service, building up our results set:
    for key in sorted(stat_structs):

        if stat_structs[key] is None or not isinstance(stat_structs[key], dict):
            # Then the given service failed to return anything from status() -- stub in an entry here:
            stat_structs[key] = {}
            stat_structs[key]['state'] = angel.constants.STATE_UNKNOWN
            stat_structs[key]['message'] = 'Status check failed'
            if time_exceeded:
                stat_structs[key]['message'] = 'Status check failed or timed out'

        try:
            # Generate a lower-cased name of the service, without the word "service" in it:
            this_service_name = '-'.join(re.findall('[A-Z][^A-Z]*', string.replace(key, 'Service', ''))).lower()
            service_info[this_service_name] = {}

            this_state = stat_structs[key]['state']
            if this_state is None:
                print >>sys.stderr, "Error: service %s failed to return a state code" % this_service_name
                this_state = angel.constants.STATE_UNKNOWN
            service_info[this_service_name]['state'] = this_state
            status_seen_by_type[this_state] = True

            this_message = 'Unknown'
            if 'message' in stat_structs[key] and stat_structs[key]['message'] is not None:
                this_message = stat_structs[key]['message']

            if this_state != angel.constants.STATE_RUNNING_OK or do_state_checks is False:
                if len(state_message):
                    state_message += ", "
                if not (check_only_these_services is not None and 1 == len(check_only_these_services)):
                    # If we're only checking one service, don't preface the status message with the service name.
                    state_message += "%s: " % this_service_name
                state_message += this_message.split("\n")[0]

            try:
                state_name = angel.constants.STATE_CODE_TO_TEXT[this_state]
            except:
                state_name = 'UNKNOWN(%s)' % this_state
            format_str = "{:>%s}:{:>9}  {}" % left_column_width
            service_info[this_service_name]['message'] = format_str.format(this_service_name, state_name, this_message.split("\n")[0])
            service_info[this_service_name]['message_raw'] = this_message.split("\n")[0]
            if 'data' in stat_structs[key]:
                _merge_status_data(this_service_name.lower(), stat_structs[key]['data'])

        except:
            print >>sys.stderr, "Error in status check %s: %s\n%s" % (key, sys.exc_info()[0], traceback.format_exc(sys.exc_info()[2]))
            state_message += " error in %s status data" % (str(key))
            status_seen_by_type[angel.constants.STATE_UNKNOWN] = True


    # Reduce multiple status_codes down to one value for our exit_code. This isn't elegant, but it seems to be the cleanest way of managing this.
    # Order of importance, most important to least important, in general:
    #       Decommissioned > Unknown > Error > Stopped > Starting|Stopping > Warn > Okay
    # - If we're "ok" but the node is marked as in maintenance mode, we flip the level up one to warning.
    # - If a service is in starting or stopping state, that masks any Warn level stuff.
    # - If the single status code is stopped, but services are supposed to be running, then that's a real error.

    extra_state_message = ''
    if services_are_running:
        if do_state_checks:
            extra_state_message += " Running %s services" % len(running_services)
            exit_code = angel.constants.STATE_RUNNING_OK
        else:
            exit_code = angel.constants.STATE_UNKNOWN
    else:
        exit_code = angel.constants.STATE_STOPPED

    enabled_services_str = copy.copy(enabled_services)
    try:
        enabled_services_str.remove('devops')
    except:
        pass
    enabled_services_str = ', '.join(enabled_services_str)

    if angel_obj.is_decommissioned():
        exit_code = angel.constants.STATE_DECOMMISSIONED
        extra_state_message = ' DECOMMISSIONED'
    elif angel.constants.STATE_UNKNOWN in status_seen_by_type:
        exit_code = angel.constants.STATE_UNKNOWN
    elif angel.constants.STATE_ERROR in status_seen_by_type:
        exit_code = angel.constants.STATE_ERROR
    elif angel.constants.STATE_STOPPED in status_seen_by_type:
        exit_code = angel.constants.STATE_STOPPED
    elif angel.constants.STATE_STARTING in status_seen_by_type:
        exit_code = angel.constants.STATE_STARTING
    elif angel.constants.STATE_STOPPING in status_seen_by_type:
        exit_code = angel.constants.STATE_STOPPING
    elif angel.constants.STATE_WARN in status_seen_by_type:
        exit_code = angel.constants.STATE_WARN
    elif angel.constants.STATE_RUNNING_OK in status_seen_by_type:
        exit_code = angel.constants.STATE_RUNNING_OK
        if services_are_running:
            extra_state_message = ' ok: running %s' % enabled_services_str
    else:
        if do_service_checks:
            extra_state_message = ' unknown state for services %s' % enabled_services_str

    if do_state_checks:
        if services_are_running:
            if exit_code == angel.constants.STATE_STOPPED:
                # If all the services are reporting STOPPED state, but we're supposed to be running, that's an error:
                exit_code = angel.constants.STATE_ERROR

        if angel_obj.is_in_maintenance_mode():
            extra_state_message += ' (in maintenance mode)'
            if exit_code == angel.constants.STATE_RUNNING_OK:
                exit_code = angel.constants.STATE_WARN

        if not services_are_running:
            if len(running_services) and False:
                extra_state_message += ' (stopped; running %s; normally runs %s)' % (', '.join(running_services), enabled_services_str)
            else:
                extra_state_message += ' (stopped; normally runs %s)' % enabled_services_str
            if exit_code == angel.constants.STATE_RUNNING_OK or exit_code == angel.constants.STATE_WARN:
                exit_code = angel.constants.STATE_STOPPED

        if len(running_unexpectedly):
            extra_state_message += ' (running unexpected services: %s)' % ', '.join(running_unexpectedly)
            if exit_code == angel.constants.STATE_RUNNING_OK:
                exit_code = angel.constants.STATE_WARN

        if services_are_running:
            if len(not_running_but_should_be):
                extra_state_message += ' (services missing: %s)' % ', '.join(not_running_but_should_be)
                exit_code = angel.constants.STATE_ERROR

        state_message += extra_state_message.replace(') (', '; ') # in case we have multiple (foo) (bar) messages


    # We now have a state_message and exit code -- transform it according to the requested output format:

    # Default output:
    if format == '' or format is None:
        if not services_are_running and 'devops' in service_info:
            del service_info['devops']
        # It's possible to have a running service and a stopped system, e.g. during maintenance work.
        if len(service_info):
            for entry in sorted(service_info):
                color_start = ''
                color_end = ''
                if angel.util.terminal.terminal_stdout_supports_color():
                    if service_info[entry]['state'] in (angel.constants.STATE_WARN,angel.constants.STATE_STOPPED):
                        color_start = '\033[0;31m'
                        color_end = '\033[0m'
                    if service_info[entry]['state'] in (angel.constants.STATE_ERROR, angel.constants.STATE_UNKNOWN):
                        color_start = '\033[1;31m'
                        color_end = '\033[0m'
                message_to_print = service_info[entry]['message']
                if angel.util.terminal.terminal_width_is_true_size():
                    message_to_print = service_info[entry]['message'][:angel.util.terminal.terminal_width()]
                print color_start + message_to_print + color_end
            if do_service_checks and do_state_checks:
                print "-" * angel.util.terminal.terminal_width()

        general_status_state = 'UNKNOWN EXIT CODE (%s)' % exit_code
        try:
            general_status_state = angel.constants.STATE_CODE_TO_TEXT[exit_code]
        except:
            pass

        if do_state_checks:
            print ('{:>%s}: {}' % left_column_width).format("State", extra_state_message.lstrip())

            status_notes = ''
            if len(running_unexpectedly):
                status_notes += '; running unexpected services (%s)' % ', '.join(running_unexpectedly)
            status_info = "%s%s as of %s" % (general_status_state,
                                             status_notes,
                                             datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))
            print ('{:>%s}: {}' % left_column_width).format("Status", status_info)

        # In default format, we return 0 exit code on Ok and Warn states; non-zero otherwise:
        if exit_code == angel.constants.STATE_RUNNING_OK or exit_code == angel.constants.STATE_WARN:
            return 0
        return 1

    # Silent -- exit code only:
    if format == 'silent':
        if exit_code == angel.constants.STATE_RUNNING_OK or exit_code == angel.constants.STATE_WARN:
            return 0
        if exit_code == 0:
            return -1
        return exit_code

    # Errors-only -- useful for running across a large number of nodes:
    if format == 'errors-only':
        # If we're in an error or unknown state, display short message with error info and exit 1; otherwise silent and 0 exit code:
        in_error = False
        if exit_code == angel.constants.STATE_ERROR or exit_code == angel.constants.STATE_UNKNOWN or exit_code == angel.constants.STATE_DECOMMISSIONED: in_error = True
        if services_are_running and exit_code == angel.constants.STATE_STOPPED: in_error = True
        if in_error:
            print "%s: %s" % (angel_obj.get_node_hostname(), state_message)
            return 1
        return 0

    # Nagios formatted output:
    if format == 'nagios':
        # Nagios doesn't use decommissioned, starting, stopping, or stopped states, so we have to remap those:
        nagios_exit_code = exit_code
        if exit_code == angel.constants.STATE_DECOMMISSIONED: nagios_exit_code = angel.constants.STATE_ERROR
        if exit_code == angel.constants.STATE_STARTING: nagios_exit_code = angel.constants.STATE_WARN
        if exit_code == angel.constants.STATE_STOPPING: nagios_exit_code = angel.constants.STATE_WARN
        if exit_code == angel.constants.STATE_STOPPED:
            if not services_are_running:
                nagios_exit_code = angel.constants.STATE_WARN
            else:
                nagios_exit_code = angel.constants.STATE_ERROR

        # Create nagios format output:
        nagios_data_str = ''
        for key in sorted(status_data):
            d = status_data[key]
            if not 'value' in d:
                print >>sys.stderr, "Error: status data key %s has no value." % key
                continue
            nagios_data_str += " %s=%s" % (key, d['value'])

            if 'unit' in d:
                if d['unit'] in angel.constants.STAT_TYPES_NAGIOS:
                    nagios_data_str += "%s" % (angel.constants.STAT_TYPES_NAGIOS[ d['unit'] ].replace('~','') ) # See note in stats about '~' chars
                else:
                    print >>sys.stderr, "Error: unknown stat unit type '%s'" % d['unit']

            if not 'warn' in d and not 'error' in d and not 'min' in d and not 'max' in d:
                continue

            nagios_data_str += ";"
            if 'warn' in d:
                nagios_data_str += "%s" % d['warn']

            if not 'error' in d and not 'min' in d and not 'max' in d:
                continue

            nagios_data_str += ";"
            if 'error' in d:
                nagios_data_str += "%s" % d['error']

            if not 'min' in d and not 'max' in d:
                continue

            nagios_data_str += ";"
            if 'min' in d:
                nagios_data_str += "%s" % d['min']

            if not 'max' in d:
                continue
            nagios_data_str += ";%s" % d['max']

        # Print state message without hints:
        # (e.g. get rid of [...] comments in string like: process not running [try 'tool foo start'])
        print re.sub('\[[^\]]*\]', '', state_message)
        if len(nagios_data_str):
            print '|' +  nagios_data_str.lstrip(),
        print ""
        return nagios_exit_code


    # Unknown format (this should never be reached, unless an invalid format is specified):
    print >>sys.stderr, "Error: unknown status format '%s'" % format
    return angel.constants.STATE_UNKNOWN


def run_collectd_monitor(angel_obj, services_to_check, interval):

    hostname = socket.gethostname()
    if interval < 5:
        print >>sys.stderr, "Collectd interval too short; setting to 5 seconds."
        interval = 5
    if interval > 60*5:
        print >>sys.stderr, "Warning: very long collectd interval"

    while True:

        # Check that we're not leaking memory:
        mem_usage = angel.stats.mem_stats.get_mem_usage()
        if mem_usage['rss'] > 1024*1024*256:
            print >>sys.stderr, "Warning: collectd memory usage is high! %s bytes rss?" % mem_usage['rss']

        sample_start_time = time.time()

        set_proc_title('collectd: getting status')
        overall_status_code, stat_structs = angel_obj.service_status(services_to_check=services_to_check, run_in_parallel=False, timeout=interval)  # run serially so we don't spike resource usage
        set_proc_title('collectd: running')
        running_service_count = len(stat_structs)

        if 0 == running_service_count:
            # This happens when we're stopped. In this case, we'll return, which will cause us to exit.
            # Collectd will then re-start the script on the next interval.
            # We do this to guarantee that any config changes are picked up by collectd, otherwise we might
            # not pick up changes in IP addresses or services we should be monitoring.
            return 1

        for key in sorted(stat_structs):
            try:
                this_service_name = '-'.join(re.findall('[A-Z][^A-Z]*', string.replace(key, 'Service', ''))).lower()
                if stat_structs[key] is None:
                    print >>sys.stderr, "Error: service %s failed to return a stat struct" % this_service_name
                    continue

                if 'data' not in stat_structs[key]:
                    continue  # This is ok -- just means this service doesn't have any performance data metrics
                this_data = stat_structs[key]['data']
                for data_point in this_data:
                    if 'value' not in this_data[data_point]:
                        print >>sys.stderr, "Error in status check %s: key %s has no value" % (this_service_name, data_point)
                        continue
                    collectd_group = this_service_name
                    collectd_name = data_point
                    if 'stat_group' in this_data[data_point]:
                        stat_group_name = this_data[data_point]['stat_group'].lower().replace(' ', '_').replace('-', '_')
                        if this_service_name != stat_group_name:
                            collectd_group = "%s-%s" % (this_service_name, stat_group_name)
                    if 'stat_name' in this_data[data_point]:
                        collectd_name = this_data[data_point]['stat_name']
                    collectd_name = collectd_name.replace(' ', '_').replace('-', '_')
                    if 'unit' in this_data[data_point]:
                        stat_unit = angel.constants.STAT_TYPES_COLLECTD[ this_data[data_point]['unit'] ]
                        if stat_unit != collectd_name:
                            collectd_name = "%s-%s" % (stat_unit, collectd_name)
                    print 'PUTVAL "%s/%s/%s" interval=%s %d:%s' % (hostname[0:62], collectd_group[0:62], collectd_name[0:62], interval, sample_start_time, this_data[data_point]['value'])

            except:
                print >>sys.stderr, "Error in status check %s: %s\n%s" % (key, sys.exc_info()[0], traceback.format_exc(sys.exc_info()[2]))

        sample_end_time = time.time()
        sample_run_time = sample_end_time - sample_start_time
        try:
            sleep_time = interval - sample_run_time
            if sleep_time < 2:
                print >>sys.stderr, "warning: collectd interval=%s; run time=%s; sleep time=%s; sleeping for 2 seconds instead" % (interval, sample_run_time, sleep_time)
                sleep_time = 2
            set_proc_title('collectd: sleeping for %.2f' % sleep_time)
            time.sleep(sleep_time)
        except:
            print >>sys.stderr, "collectd sleep interrupted"
            return 1




def _print_status_preamble(angel_obj, left_column_width):
    """Print some basic info about the node -- a "header" to the status output"""

    def _print_line(label, value):
        print ('{:>%s}: {}' % left_column_width).format(label, value)

    # "    Node:
    ip_addr_info = angel_obj.get_private_ip_addr()
    if angel_obj.get_public_ip_addr():
        if angel_obj.get_public_ip_addr() != ip_addr_info:
            ip_addr_info += ' / ' + angel_obj.get_public_ip_addr()
    nodename = angel_obj.get_node_hostname()
    nodename_warning_msg = ""
    if not angel.util.network.is_hostname_reverse_resolving_correctly():
        nodename_warning_msg = "   !!! INVALID REVERSE DNS ENTRY !!! "
    _print_line("Node", "%s - %s%s" % (nodename, ip_addr_info, nodename_warning_msg))

    version_manager = angel_obj.get_version_manager()
    branch = angel_obj.get_project_code_branch()
    code_version = angel_obj.get_project_code_version()
    newest_code_version = None
    pinning_message = ''
    if version_manager:
        if version_manager.is_version_pinned():
            pinning_message = '; version is pinned (%s)' % version_manager.get_version_pinned_reason()
    if branch is None:
        branch = '_unknown-branch'
    if code_version is None:
        code_version = '_unknown-build'
    if version_manager:
        newest_code_version = version_manager.get_highest_installed_version_number(branch)
    branch_and_build = branch
    if code_version is not None:
        branch_and_build += " %s" % code_version

    version_message = ''
    if code_version and newest_code_version:
        if code_version != newest_code_version:
            version_message = '; newer version %s available' % newest_code_version
    _print_line("Version", "%s%s%s" % (branch_and_build, version_message, pinning_message))

