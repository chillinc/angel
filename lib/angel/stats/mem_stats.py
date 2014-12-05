
def get_mem_usage():
    ''' Get the memory usage of the current process in bytes. '''
    # Based on http://stackoverflow.com/questions/897941/python-equivalent-of-phps-memory-get-usage
    status = None
    result = {'peak': -1, 'rss': -1}
    try:
        status = open('/proc/self/status')
        for line in status:
            parts = line.split()
            key = parts[0][2:-1].lower()
            if key in result:
                result[key] = int(parts[1]) * 1024
    except:
        pass
    finally:
        if status is not None:
            status.close()
    return result
