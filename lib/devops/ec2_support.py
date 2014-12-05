
import re
import sys
import time

try:
    from boto import ec2
except:
    pass

from devops.process_helpers import get_command_output
from devops.simple_cache import simple_cache_get


def ec2_is_usable():
    ''' Return true is we're running on an ec2 system, false otherwise. '''
    if 'boto' not in sys.modules: return False
    if ec2_get_this_nodes_instance_id() is not None: return True
    return False


def ec2_get_attribute_via_http(attribute_name):
    ''' Given an attribute name, such as 'meta-data/public-ipv4', 'meta-data/placement/availability-zone', etc, return it; or None on error. '''
    return simple_cache_get('ec2-%s' % attribute_name, get_function=_ec2_get_attribute_via_http_helper, get_function_args=(attribute_name,), get_function_ttl_in_seconds=3600)


def _ec2_get_attribute_via_http_helper(attribute_name):
    out, err, exitcode = get_command_output('curl', ('-s', '--connect-timeout', '2', 'http://169.254.169.254/latest/%s' % attribute_name))
    if out is None or exitcode != 0:
        return None
    if attribute_name[-4:] == 'ipv4':
        # ec2 nodes can have no public ipv4 in rare cases, quick-check on length:
        if len(out) < 6 or len(out) > 16:
            return None
    return out


def ec2_get_this_nodes_instance_id():
    ''' Return the instance if for the current node, or None on error. '''
    data, errors, exitcode = get_command_output('curl --connect-timeout 3 --fail http://instance-data/latest/meta-data/instance-id')
    if exitcode != 0: return None
    return data


def ec2_get_this_nodes_user_data():
    ''' Return the ec2 node's user-data field, or None if not available. '''
    value = ec2_get_attribute_via_http('user-data')
    if value is None:
        value = ""
    return value


def ec2_get_this_nodes_zone():
    ''' Returns the region of this ec2 node's instance. '''
    return ec2_get_attribute_via_http('meta-data/placement/availability-zone')


def ec2_get_this_nodes_region():
    zone = ec2_get_this_nodes_zone()
    if zone is None: return None
    if not re.match("[a-z]",zone[-1]):
        print >>sys.stderr, "Error: devops.ec2_get_this_nodes_region expected a lower-case letter, but got %s instead." % zone[-1]
        return None
    return zone[:-1] # This will break if amazon ever changes their zone naming scheme. As of May 2012, there doesn't seem to be a cheap way to get zone->region...


_ec2_get_all_instances_cache = None
_ec2_get_all_instances_cache_time = None
def ec2_get_all_instances(aws_access_key_id, aws_secret_access_key):
    ''' Fetch info about all our instances. This call caches results for up to 10 seconds to speed up processing.
        Returns a dict of instances, where the keys are instance IDs and the values are boto ec2 objects.
    '''
    global _ec2_get_all_instances_cache, _ec2_get_all_instances_cache_time
    if _ec2_get_all_instances_cache_time is not None:
        if time.time() - _ec2_get_all_instances_cache_time > 10:
            _ec2_get_all_instances_cache_time = _ec2_get_all_instances_cache = None
    if _ec2_get_all_instances_cache is not None:
        return _ec2_get_all_instances_cache
    c = _ec2_get_connection(aws_access_key_id, aws_secret_access_key)
    if c is None: return None
    reservations = c.get_all_instances()
    instances = [i for r in reservations for i in r.instances]
    _ec2_get_all_instances_cache = {}
    _ec2_get_all_instances_cache_time = time.time()
    for i in instances:
        _ec2_get_all_instances_cache[i.id] = i
    return _ec2_get_all_instances_cache


def ec2_get_tags_for_instance(aws_access_key_id, aws_secret_access_key, instance_id):
    ''' Returns the tags for the given instance_id, or None on error. '''
    instances = ec2_get_all_instances(aws_access_key_id, aws_secret_access_key)
    if instances is None:
        print >>sys.stderr, "Error: failed to get info on ec2 instances."
        return None
    if instance_id not in instances:
        print >>sys.stderr, "Error: no info found for ec2 instance id '%s'." % instance_id
        return None
    try:
        return instances[instance_id].tags
    except:
        print >>sys.stderr, "Error: ec2_get_tags_for_instance not implemented (needs newer version of boto)."
    return None


def _ec2_get_connection(aws_access_key_id, aws_secret_access_key):
    if aws_access_key_id is None or aws_secret_access_key is None:
        print >>sys.stderr, "Error: devops can't find EC2 access keys; check that devops access key id and secret are set."
        return None
    try:
        c = ec2.connect_to_region(ec2_get_this_nodes_region(), aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key)
        if c is None:
            print >>sys.stderr, "Error: devops can't create EC2 boto connection (check that access key %s has permission?)" % aws_access_key_id
            return None
    except Exception as e:
        print >>sys.stderr, "Error: devops can't create EC2 boto connection (%s)" % e
        return None
    return c
