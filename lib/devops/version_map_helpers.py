
import os,sys
from fcntl import flock, LOCK_EX, LOCK_UN

def add_entries_to_static_version_mapping(static_map_filepath, entries):
    parent_dir = os.path.dirname(os.path.abspath(static_map_filepath))
    try:
        if not os.path.isdir(parent_dir): os.makedirs(parent_dir)
    except:
        # It's possible another process will have made the dir inbetween when we check its existance and call makedirs.
        # (Yes, this has happened.)
        if not os.path.isdir(parent_dir):
            print >>sys.stderr, "Error: unable to make dir '%s' in add_entries_to_static_version_mapping." % parent_dir
            return 1

    try:
        md5_map_file = open(static_map_filepath, "a")

        # Try to flock, although this seems to fail on ec2:
        flock(md5_map_file, LOCK_EX)

        # Flush to check if we're still at beginning of file (race-condition), and if so, write the preamble:
        md5_map_file.flush()
        if 0 == md5_map_file.tell():
            md5_map_file.write("hsn_static_to_cdn_map = {}\n")
            md5_map_file.flush()

        for keyname in entries:
            md5_map_file.write("hsn_static_to_cdn_map['%s'] = '%s'\n" % (keyname, entries[keyname]))
            md5_map_file.flush() # flush after every write, so in case other concurrent processes are writing and locks failed, our lines are at least intact.

        flock(md5_map_file, LOCK_UN)
        md5_map_file.close()

    except Exception as e:
        print >>sys.stderr, 'Error writing to static map "%s"' % static_map_filepath
        print >>sys.stderr, e
        return 1

    return 0
