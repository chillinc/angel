
import os
import sys

def disk_stats_get_usage():
    partitions = disk_stats_get_partitions()
    ret_data = {}
    for partition in partitions:
        i = disk_stats_get_usage_for_path(partition)
        if i is not None:
            ret_data[partition] = i
    return ret_data


def disk_stats_get_usage_for_path(path):
    try:
        # statvfs will return the partition for the given path, even if it's a subdir of the mount point:
        path = os.path.expanduser(path)
        while len(path) > 2 and not os.path.exists(path):
            path = os.path.dirname(path)
        disk_info = os.statvfs(os.path.expanduser(path))
        capacity_mb = disk_info.f_bsize * disk_info.f_blocks / (1024*1024)
        free_mb = disk_info.f_bsize * disk_info.f_bavail / (1024*1024)
        ret_val = {}
        ret_val['free_inodes'] = disk_info.f_ffree
        ret_val['free_mb'] = free_mb
        ret_val['used_mb'] = capacity_mb - free_mb
        ret_val['size_mb'] = capacity_mb
        ret_val['used_percent'] = (1000 * (capacity_mb - free_mb) / capacity_mb) / 10.0 # Fast hack to get 1 decimal place
        return ret_val
    except Exception as e:
        print >>sys.stderr, "Error: can't get disk stats for path '%s': %s" % (path, e)
    return None


def disk_stats_get_partitions():
    # To-do: cache this in a global with a timeout and only re-build periodically
    # Also, this returns paths that can contain escape characters; we're not handling this very well.
    # It's "good enough" for our uses for now, but should be revised.
    partitions = []
    if os.path.isfile('/etc/mtab'):
        mtab = open('/etc/mtab')
        while True:
            line = mtab.readline()
            if len(line) == 0: break
            if line[0] == '/':
                device, mountpoint, dummy = line.split(' ', 2)
                partitions += [mountpoint]
    else:
        partitions = ['/']
    return partitions

