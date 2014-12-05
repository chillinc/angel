
import glob
import os
import sys
from devops.ec2_support import *
from devops.logging import log_to_syslog
from devops.process_helpers import run_command
from devops.settings_helpers import key_value_string_to_dict


def system_conf_set(config, key, value):
    ''' Update the conf files under CONF_DIR to set key to given value. Return a positive value on changes; 0 on no changes; negative on error. '''
    if value is None:
        print >>sys.stderr, "Error: can't set conf var %s to None; use unset instead." % key # So code errors don't accidentally delete a key
    return _system_conf_set(config, key, value)


def system_conf_unset(config, key):
    ''' Delete any conf setting for given key that is defined under CONF_DIR. Return a positive value on changes; 0 on no changes (or if it doesn't exist), negative on error. '''
    return _system_conf_set(config, key, None)


def _system_conf_set(config, key, value):
    ''' Helper function for system_conf_set / system_conf_unset. '''
    if not os.path.isdir(config['CONF_DIR']):
        print >>sys.stderr, "Warning: conf dir '%s' missing; creating it now." % config['CONF_DIR']
        os.makedirs(config['CONF_DIR'])

    if key not in config and value is not None:
        # Allow unknown settings to be set in case we're pre-defining a key before an upgrade or using a programatically-referenced setting.
        print >>sys.stderr, "Warning: no default for setting %s; setting it anyway." % key 

    def _get_new_conf_line():
        with_quotes = True
        if key in config and not isinstance(config[key], basestring):
            with_quotes = False
            if config[key] is None:
                # Clunky, but necessary: if the default is None, then treat this as a string.
                with_quotes = True
        if with_quotes:
            return '%s="%s"\n' % (key, value.replace('\\', '\\\\').replace('"', '\\"'))
        else:
            return '%s=%s\n' % (key, value)


    has_key_been_seen = False
    for filename in glob.glob('%s/*.conf' % config['CONF_DIR']):
        new_data = ''
        file_needs_updating = False
        lines = open(filename).readlines()
        for line in lines:
            # Check against a version that removes leading '# ', so that
            # when we "set, unset, set", we reuse the line that was previously commented out:
            comment_cleared_line = line
            if comment_cleared_line[0:2] == '# ':
                comment_cleared_line = comment_cleared_line[2:]
            if comment_cleared_line[0] == '#':
                comment_cleared_line = comment_cleared_line[1:]
            if comment_cleared_line.startswith(key) and (comment_cleared_line[len(key)] == '=' or comment_cleared_line[len(key)] == ' '):
                file_needs_updating = True
                if value is None or has_key_been_seen:
                    new_data += '# %s' % comment_cleared_line
                else:
                    new_data += _get_new_conf_line()
                has_key_been_seen = True
            else:
                new_data += line

        if not file_needs_updating:
            continue  # continue, not break, so that we process all files to delete out any duplicate keys that a user may have accidentally set manually.
        try:
            open('%s.tmp' % filename, 'w').write(new_data)
            os.rename('%s.tmp' % filename, filename)
        except Exception as e:
            print >>sys.stderr, "Error: unable to update setting %s in %s (%s)." % (filename, key, e)
            return -2

    # If we didn't find the key and the new value is None, there's nothing to do:
    if not has_key_been_seen and value is None:
        print >>sys.stderr, "Warning: setting %s isn't defined in conf dir." % key
        return 0

    # Append the new key=value line to a settings file:
    if not has_key_been_seen:
        try:
            settings_file = os.path.join(config['CONF_DIR'], '%s_settings.conf' % key.split('_')[0].lower())
            current_file_needs_newline_before_value = False
            if os.path.exists(settings_file):
                data = open(settings_file).read()
                if len(data) and data[-1] != '\n':
                    current_file_needs_newline_before_value = True
            new_data = ''
            if current_file_needs_newline_before_value:
                new_data = "\n"
            new_data += _get_new_conf_line()
            open(settings_file, 'a').write(new_data)

        except Exception as e:
            print >>sys.stderr, "Error: unable to add setting %s (%s)." % (key, e)
            return -4

    # Log to syslog when the setting was updated:
    # (Don't show value when key name has 'key' or 'secret' in it to avoid security issues with central logging; not fool-proof but a "better than nothing" approach.)
    if 'key' in key.lower() or 'secret' in key.lower():
        log_to_syslog("Settings: set config key %s" % (key))
    else:
        log_to_syslog("Settings: set config key %s to '%s'" % (key, value))

    return 0


def system_autoconf(config, force=False):
    ''' Automatically configure this node based on ec2 tags or other "outside" information.
        Return >0 if changes made; 0 if no changes; and negative value if errors. '''

    # We only support ec2 auto-conf for now...
    if ec2_is_usable():
        return _system_autoconf_ec2(config, force)
    else:
        print >>sys.stderr, "Error: autoconf not supported on this node."
        return -1


def _system_autoconf_ec2(config, force):
    ''' Look at ec2 node's tags to conf the system:
            DEVOPS_AUTOCONF_HOSTNAME      Hostname of this node
            DEVOPS_AUTOCONF_STACK         Group that the given ec2 node is part of; expected to be the public http hostname.
                                          The hosts list is generated by finding all nodes that have the same value as this node, 
                                          then mapping the services listed to each for the IP listed for each.
            DEVOPS_AUTOCONF_SERVICES      List of services that the given ec2 node should run; if set to "all" then this node will run all services 
            DEVOPS_AUTOCONF_HOST_ENABLED  Must be set to "yes" to be included in the HOSTS list
        Returns >0 if changes made; 0 if no changes; and negative value if errors.
    '''

    change_count = 0

    # Pull tags from ec2 instances, using ec2 keys from USER_DATA or from our config.
    my_instance_id = ec2_get_this_nodes_instance_id()
    if my_instance_id is None:
        return -1
    my_tags = ec2_get_tags_for_instance(config['AWS_DEVOPS_ACCESS_KEY_ID'], config['AWS_DEVOPS_ACCESS_KEY_SECRET'], my_instance_id)
    if my_tags is None:
        return -1
 
    # Set stack hostname based on the value from DEVOPS_AUTOCONF_STACK -- this is how we know which stack_settings to load; required for prod.
    if 'PUBLIC_WEB_HOSTNAME' in my_tags:
        system_conf_set(config, 'PUBLIC_WEB_HOSTNAME', my_tags['PUBLIC_WEB_HOSTNAME'])
    elif 'DEVOPS_AUTOCONF_STACK' in my_tags:
        system_conf_set(config, 'PUBLIC_WEB_HOSTNAME', my_tags['DEVOPS_AUTOCONF_STACK'])

    if 'PUBLIC_API_HOSTNAME' in my_tags:
        system_conf_set(config, 'PUBLIC_API_HOSTNAME', my_tags['PUBLIC_API_HOSTNAME'])
    elif 'DEVOPS_AUTOCONF_STACK' in my_tags:
        system_conf_set(config, 'PUBLIC_API_HOSTNAME', my_tags['DEVOPS_AUTOCONF_STACK'])

    # Set node hostname based on the value from DEVOPS_AUTOCONF_HOSTNAME:
    if 'DEVOPS_AUTOCONF_HOSTNAME' in my_tags:
        hostname = my_tags['DEVOPS_AUTOCONF_HOSTNAME']

        if os.path.isfile('/etc/hostname'):
            try:
                etc_hostname = open('/etc/hostname').read().rstrip()
                if etc_hostname != hostname:
                    open('/etc/hostname', 'w').write("%s\n" % hostname)
                    change_count += 1
            except Exception as e:
                print >>sys.stderr, "Error: autoconf failed to update /etc/hostname: %s" % e

        out, err, code = get_command_output('hostname')
        if out.rstrip() != hostname:
            if 0 != run_command('hostname', args=(hostname,), print_error_info=True):
                print >>sys.stderr, "Error: autoconf failed to set hostname"
                return -1
            change_count += 1

        etc_hosts_entry_exists = False
        hosts_fh = open('/etc/hosts')
        for line in hosts_fh.readlines():
            if line.rstrip() == '127.0.0.1 %s' % hostname:
                etc_hosts_entry_exists = True
                break
        if not etc_hosts_entry_exists:
            try:
                open('/etc/hosts', 'a').write('127.0.0.1 %s\n' % hostname)
                change_count += 1
            except Exception as e:
                print >>sys.stderr, "Error: autoconf failed to update /etc/hosts: %s" % e

    return change_count
