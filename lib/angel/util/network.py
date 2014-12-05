import socket
import sys

def is_hostname_reverse_resolving_correctly():
    '''Check if the primary IP addr for our host reverse-resolves correctly. Also returns true
       if no reverse resolve defined. This is useful for server processes like Java that sometimes
       use the '''
    my_hostname = socket.gethostname()
    my_ipaddr = socket.gethostbyname(my_hostname)
    try:
       (reverse_hostname, reverse_aliaslist, reverse_ipaddrlist) = socket.gethostbyaddr(my_ipaddr)
    except socket.herror:
       # Then there's no reverse-DNS, normal on a DHCP network.
       return True
    try:
        reverse_hostname_to_ipaddr = socket.gethostbyname(reverse_hostname)
    except:
        print >>sys.stderr, "Warning: local hostname %s running on %s, but %s reverse-resolves to invalid host %s." % \
            (my_hostname, my_ipaddr, my_ipaddr, reverse_hostname)
        return False
    return True