
import sys

def key_value_string_to_dict(data_string, key_value_separator='='):
    ''' Given a string like:
            key=value\nkey2=value2
        Return a dict with data[key] = values.
        - Ignores all lines that start with # or are empty.
        - Returns None on any parse errors.
        - Strips leading and trailing white-space on a line
    '''
    if not isinstance(data_string, str):
        print >>sys.stderr, "Warning: key_value_string_to_dict called with a non-string value of data_string: %s" % data_string
        return None
    try:
        data_lines = map(lambda x: x.lstrip(), data_string.strip().split('\n'))
        data_lines_wo_comments = filter(lambda x: not (len(x) == 0 or x[0] == '#'), data_lines)
        invalid_lines = filter(lambda x: x.find(key_value_separator) < 0 and len(x.strip()), data_lines_wo_comments)
        if len(invalid_lines):
            print >>sys.stderr, "Invalid lines found while parsing string:\n%s" % '\n'.join(invalid_lines)
            return None
        return dict([ [y.strip() for y in x.split(key_value_separator,1)] for x in filter(lambda x: x.find(key_value_separator) > 0, data_lines_wo_comments)])
    except Exception as e:
        print >>sys.stderr, "Parse error in key_value_string_to_dict: %s" % e
        import traceback
        traceback.format_exc(sys.exc_info()[2])
        return None
