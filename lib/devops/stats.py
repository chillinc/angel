
# Defines functions for collecting stats by hsn for monitoring and for exporting to collectd and nagios.

# Example:
#    stat_struct = stats_create_struct(message="Short human message", state=angel.constants.STATE_RUNNING_OK)  -- message and state are optional
#    stats_update_struct(stat_struct, message="Some new message", state=angel.constants.STATE_STOPPING)    -- ditto, only pass what you want to update
#    stats_add_data_record(data_dict, name, value, unit=None, warn=None, error=None, min=None, max=None, stat_group=None, stat_name=None) -- all key=value params are optional


import sys
import angel.settings


def stats_create_struct(message='', state=None, data=None):
    ''' Return a dict with required keys set. Message should be a short human-readable message; state should be one of the angel.constants.STATE_STOPPING values.'''
    if data is None: data = {}
    return {"message": message, "state": state, "data": data}


def stats_merge_structs(stat_struct, stat_struct_to_import):
    ''' Update the stat_struct with state and data from the second struct.'''
    stats_update_struct(stat_struct, message=stat_struct_to_import['message'], state=stat_struct_to_import['state'])
    stat_struct['data'] = dict(stat_struct_to_import['data'], **stat_struct['data'])    


def stats_update_struct(stat_struct, message=None, state=None, replace_instead_of_append=False):
    ''' Update the stat_struct with the given data -- this is an append operation unless otherwise specified, so if state is already ERROR and you try to set it to OK, it will remain at ERROR. '''
    if replace_instead_of_append:
        if message is not None:
            stat_struct['message'] = message
        if state is not None:
            stat_struct['state'] = state
        return

    if message is not None and len(message):
        if 'message' in stat_struct and stat_struct['message'] is not None and len(stat_struct['message']):
            stat_struct['message'] = str(stat_struct['message']).rstrip() + '; ' + message
        else:
            stat_struct['message'] = message

    if state is None:
        return
    if stat_struct['state'] is None:
        stat_struct['state'] = state
        return
    if state == stat_struct['state']:
        return

    # If new state is STARTING / STOPPING, then that takes precedence:
    if state == angel.constants.STATE_STOPPING or state == angel.constants.STATE_STOPPING:
        stat_struct['state'] = state
        return

    # If new state is ERROR, trump everything but starting/stopping:
    if state == angel.constants.STATE_ERROR:
        stat_struct['state'] = state
        return

    # If new state is Unknown...
    if state == angel.constants.STATE_UNKNOWN:
        # ...and we haven't seen an error, then trump everything:
        if stat_struct['state'] != angel.constants.STATE_ERROR:
            stat_struct['state'] = state
        return

    # If new state is just warning, and we were at OK before, then go to WARN, otherwise stay in prior state:
    if state == angel.constants.STATE_WARN:
        if stat_struct['state'] == angel.constants.STATE_RUNNING_OK:
            stat_struct['state'] = state
        return

    # If new state is OK, then anything we had before takes precedence:
    if state == angel.constants.STATE_RUNNING_OK:
        return

    # At this point, new state can only be stopped.
    if state == angel.constants.STATE_STOPPED:
        if stat_struct['state'] == angel.constants.STATE_UNKNOWN:
            return

        print >>sys.stderr, "Not sure how to map STOPPED state onto state code %s; ignoring." % (stat_struct['state'])
        return

    print >>sys.stderr, "Unknown state code %s; ignoring." % (state)


def stats_add_data_record(stat_struct, name, value, unit=None, warn=None, error=None, min=None, max=None, stat_group=None, stat_name=None):
    ''' Given a dict, add a record with the given variables to the dict in the proper format. '''
    stat_struct['data'][name] = stats_create_data_record(name, value, unit=unit, warn=warn, error=error, min=min, max=max, stat_group=stat_group, stat_name=stat_name)


def stats_delete_data_record(stat_struct, name):
    if name in stat_struct['data']:
        del stat_struct['data'][name]


def stats_create_data_record(name, value, unit=None, warn=None, error=None, min=None, max=None, stat_group=None, stat_name=None):
    # Our data structure is a dict of key->values, where the key is the name of the data point and the value is a dict with keys:
    #     data['observation name']['value'] = <number>
    # Optional values:
    #     data['observation name']['unit'] = "percent", "number", "seconds", "bytes", "count" -- use a angel.constants.STAT_TYPE_xxx const!
    #     data['observation name']['warn'] = <number> --threshold for warnings
    #     data['observation name']['error'] = <number> --threshold for errors
    #     data['observation name']['min'] = <number> -- minimum possible value, i.e. 0 for percentage or bytes
    #     data['observation name']['max'] = <number> -- max possible value, i.e.  100 for percentage
    #     data['observation name']['stat_name'] = name for stat display ('observation name' would be used by default in collectd)
    #     data['observation name']['stat_group'] = group name for stat display (collectd, mainly)

    if value is None or len(str(value)) == 0:
        print >>sys.stderr, "Warning: stats_create_data_record(%s, %s... missing value." % (name, value)
    data = {}

    if unit is not None:
        data['unit'] = unit
        if unit not in angel.constants.STAT_TYPES_NAGIOS:
            print >>sys.stderr, "Warning: unknown unit type '%s' or missing nagios mapping for stat %s" % (unit, name)
        if unit not in angel.constants.STAT_TYPES_COLLECTD:
            print >>sys.stderr, "Warning: unknown unit type '%s' or missing collectd mapping for stat %s" % (unit, name)

    def string_to_number(s):
        if not isinstance(s, str):
            return s
        if s.find('.') < 0:
            try:
                return int(s)
            except:
                print >>sys.stderr, "Warning: unable to cast '%s' to int in devops.stats" % s
                return s
        try:
            return float(s)
        except:
            print >>sys.stderr, "Warning: unable to cast '%s' to float in devops.stats" % s
            return s

    def is_number(s):
        try:
            float(s)
            return True
        except ValueError:
            return False

    if is_number(value):
        data['value'] = string_to_number(value)
    else:
        data['value'] = value

    if warn is not None:
        data['warn'] = string_to_number(warn)
    if error is not None:
        data['error'] = string_to_number(error)
    if min is not None:
        data['min'] = string_to_number(min)
    if max is not None:
        data['max'] = string_to_number(max)
    if stat_group is not None:
        data['stat_group'] = stat_group
    if stat_name is not None:
        data['stat_name'] = stat_name
    return data


def stats_import_nagios_data(stat_struct, nagios_data_str):
    ''' Given a nagios data string, parse and add the data points into the stat_struct. '''
    # Example string:  aut_time=0.026589s;;;0.000000;10.000000 ela_time=0.000757s;;;0.000000;10.000000 gra_time=0.006633s;;;0.000000;10.000000
    data_points = nagios_data_str.split(' ')
    for data_point in data_points:
        if len(data_point) < 3:
            continue
        (this_name, this_value_parts_str) = data_point.split('=')
        this_value_parts = this_value_parts_str.split(';')

        this_value_with_unit_str = this_value_parts[0] # i.e. 0.026589s, -20, 92%, 1288kbps ...
        unit_offset = len(this_value_with_unit_str)
        while unit_offset > 1 and not this_value_with_unit_str[unit_offset-1].isdigit():
            unit_offset -= 1
        this_value = this_value_with_unit_str[0:unit_offset]
        this_unit = this_value_with_unit_str[unit_offset:]
        if len(this_unit):
            this_unit = stats_nagios_type_to_const(this_unit)
        else:
            this_unit = None

        this_warn = this_error = this_min = this_max = None
        if len(this_value_parts) > 1:
            if len(this_value_parts[1]):
                this_warn = this_value_parts[1]
        if len(this_value_parts) > 2:
            if len(this_value_parts[2]):
                this_error = this_value_parts[2]
        if len(this_value_parts) > 3:
            if len(this_value_parts[3]):   
                try:
                    this_min = float(this_value_parts[3])
                except:
                    print >>sys.stderr, "Warning: couldn't cast stat min value '%s'; passing as string." % this_value_parts[3]
                    this_min = this_value_parts[3]
        if len(this_value_parts) > 4:
            if len(this_value_parts[4]):
                try:
                    this_max = float(this_value_parts[4])
                except:
                    print >>sys.stderr, "Warning: couldn't cast stat max value '%s'; passing as string." % this_value_parts[4]
                    this_max = this_value_parts[4]
            
        stats_add_data_record(stat_struct, this_name, this_value, this_unit, this_warn, this_error, this_min, this_max)


stats_nagios_type_to_const_haystack = None
def stats_nagios_type_to_const(needle):
    ''' When parsing nagios output, we're given strings like 's', 'kb', etc. Given one of those strings, return the appropriate hsn_constant value.'''
    global stats_nagios_type_to_const_haystack
    if stats_nagios_type_to_const_haystack is None:
        stats_nagios_type_to_const_haystack = {}
        for key,value in angel.constants.STAT_TYPES_NAGIOS.items():
            stats_nagios_type_to_const_haystack[value] = key
    if needle not in stats_nagios_type_to_const_haystack:
        if needle != 'B':
            print >>sys.stderr, "Warning: nagios plugin type '%s' not defined in stats" % needle
        return needle
    return stats_nagios_type_to_const_haystack[needle]

